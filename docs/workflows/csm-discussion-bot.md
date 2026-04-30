# CSM Q&A Discussion Bot

- **Workflow 文件**：[`.github/workflows/csm-discussion-bot.yml`](../../.github/workflows/csm-discussion-bot.yml)
- **核心脚本**：[`scripts/discussion_bot.py`](../../scripts/discussion_bot.py)
- **依赖清单**：[`requirements-bot.txt`](../../requirements-bot.txt)
- **测试**：[`tests/test_discussion_bot.py`](../../tests/test_discussion_bot.py)
- **知识源**：[`csm-wiki/wiki_source.json`](../../csm-wiki/wiki_source.json)（指向 [CSM-Wiki](https://github.com/NEVSTOP-LAB/CSM-Wiki)）

## 1. 功能概述

为 NEVSTOP-LAB **组织级 Discussions**（`/orgs/NEVSTOP-LAB/discussions`）的 Q&A 分类提供基于 RAG + LLM 的自动回复。流程概览：

```
Discussion 创建 / 评论
   │
   ├─ ① GitHub 仓库级事件 → 直接触发 workflow
   ├─ ② 组织级事件 → Cloudflare Worker（webhook/）→ repository_dispatch → workflow
   └─ ③ 手动 workflow_dispatch → 全量扫描所有未答复 / 有新追问的 discussion
   │
   ▼
RAG 检索 CSM-Wiki（ChromaDB + BGE-small-zh-v1.5 embedding）
   │
   ▼
组装 Prompt → 调用 LLM（默认 deepseek-chat）→ 发布 / 追加评论
```

## 2. 触发条件

| 事件 | 何时触发 | 说明 |
|------|----------|------|
| `discussion: [created]`               | 本仓库新 Discussion | 主要用于本仓库内调试，组织级 discussion 不会进入这条路径 |
| `discussion_comment: [created]`       | 本仓库 Discussion 新评论 | 跳过 bot 自身评论以防无限循环（在脚本中通过 sender/author type 过滤） |
| `repository_dispatch: org_discussion_created` | 组织级 webhook 中继 | `client_payload.discussion_number` 必填，Worker 已做去重 |
| `workflow_dispatch`                   | 手动触发 | 输入 `dry_run=true` 时只打印，不发布评论；用于回归测试 |

> **运行频率**：事件驱动，无 cron。手动全量扫描按需运行。

## 3. 关键 Secrets

| Secret | 作用 | 备注 |
|--------|------|------|
| `CSM_QA_GH_TOKEN` | checkout、读写组织级 Discussions | 见下方说明 |

`CSM_QA_GH_TOKEN` 必须是 **Fine-grained PAT**，至少授予：

- 目标仓库 `Contents: Read`、`Discussions: Read & Write`
- 组织级 `Discussions: Read & Write`（组织级 discussion 归属于源仓库 `<org>/.github`）

**不要**改成默认 `GITHUB_TOKEN`，后者无法访问组织级 discussion API。
| `LLM_API_KEY`     | 调用 LLM API | 默认 deepseek-chat，可在脚本/环境变量中切换 OpenAI 兼容 provider |

## 4. 缓存策略（重要）

workflow 使用 **拆分式 `actions/cache/restore` + `actions/cache/save`** 而非 `actions/cache@v4`，原因：

- `actions/cache@v4` 的 post-save 仅在 job success 时执行；bot 早期失败（配置/密钥错误）时缓存永远不会保存，下一次重跑仍 miss，形成"永远命中不到"的死循环。
- 拆开后每个 save 步骤使用 `if: always() && cache-hit != 'true' && <已生成内容>`，即便后续步骤失败，已经下载/构建好的缓存也能保留。

| 缓存 | Key | 内容 |
|------|-----|------|
| `pip` | `${{ runner.os }}-pip-${{ hashFiles('requirements-bot.txt') }}` | Python 依赖 |
| `huggingface` | `${{ runner.os }}-hf-BAAI-bge-small-zh-v1.5` | embedding 模型 |
| `vector store` | `${{ runner.os }}-vectorstore-<csm-wiki HEAD SHA>` | ChromaDB 向量库 + `csm-wiki/wiki_source.json` + `csm-wiki/remote` |

向量库 key 中的 SHA 通过在 `actions/cache` **之前**单独调用 GitHub API 解析得到（`Resolve csm-wiki latest commit SHA` 步骤）；该步骤通过仓库 `default_branch` 字段动态发现分支名（`main` / `master` 兜底），**不要硬编码**为 `main`。

## 5. 并发控制

```yaml
concurrency:
  group: csm-qa-bot-${{ github.event.discussion.number || github.event.client_payload.discussion_number || 'scan' }}
  cancel-in-progress: false
```

- 每条 discussion 独立分组：不同 discussion 可并行，同一条 discussion 串行。
- `cancel-in-progress: false`：避免运行中的回复被新事件中断造成漏回复。
- 全量扫描使用固定 group `csm-qa-bot-scan`，与单条 discussion 隔离。

## 6. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| Bot 不回复新 discussion | ① 查 Actions 是否被触发；② 若组织级且未触发，检查 Cloudflare Worker 日志（`webhook/`）和 `CSM_QA_GH_TOKEN` 是否过期；③ 用 `workflow_dispatch` + `dry_run=true` 复现 |
| 回复内容明显过时 | 强制使新 wiki 缓存失效：手动改一次 `csm-wiki/wiki_source.json`（或等下一次 CSM-Wiki commit），cache key 自动变化即触发重建 |
| 出现"无限循环回复" | 确认评论过滤逻辑生效：workflow 中 `discussion_comment` 步骤的 `if` 条件已包含 `github.event.comment.user.type != 'bot'`；同时 Worker 端会过滤防重标记 |
| LLM 报 401/403 | 轮换 `LLM_API_KEY`；如更换 provider，同步更新脚本中的 base_url |
| 缓存大小膨胀 | 在 Actions → Caches 页面手动清理旧 `vectorstore-*` key，下一次运行会按当前 SHA 重新构建 |

## 7. 本地调试

```bash
pip install -r requirements-bot.txt
export CSM_QA_GH_TOKEN=ghp_xxx
export LLM_API_KEY=sk-xxx
export GITHUB_REPOSITORY=NEVSTOP-LAB/.github
export DISCUSSION_SOURCE_REPO=NEVSTOP-LAB/.github

# 试跑某条组织级 discussion，不发布评论
python scripts/discussion_bot.py --org-discussion-number 123 --dry-run

# 全量扫描
python scripts/discussion_bot.py --scan-org --dry-run
```

测试：

```bash
pip install -r requirements-bot.txt pytest
pytest tests/
```

## 8. 关联文档

- [`webhook/README.md`](../../webhook/README.md)：实时触发链路与 Cloudflare Worker 部署
- [`docs/调研/`](../调研)：RAG / Token / 费用等设计调研
