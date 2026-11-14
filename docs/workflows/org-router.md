# Org Discussion Router

- **Workflow 文件**：[`.github/workflows/org-router.yml`](../../.github/workflows/org-router.yml)
- **核心脚本**：[`scripts/router.py`](../../scripts/router.py)
- **依赖清单**：[`requirements-bot.txt`](../../requirements-bot.txt)
- **测试**：[`tests/test_router.py`](../../tests/test_router.py)
- **知识源**：[`csm-wiki/wiki_source.json`](../../csm-wiki/wiki_source.json)（指向 CSM-Wiki）

## 1. 功能概述

组织级 Discussion 的统一入口：先做 LLM 意图分类（失败时降级为正则），再按意图分派处理。

| 意图 | 处理 |
|------|------|
| `JOIN` | 检查加入条件（关注组织 + Star 指定仓库），通过后发出组织邀请，默认加入 `csm-community` 团队 |
| `QA` | 在 Q&A 分类下用 CSM_QA（RAG）回答；其他分类则回复引导用户去 Q&A 区 |
| `OTHER` | 友好引导回复 |

## 2. 触发条件

| 事件 | 说明 |
|------|------|
| `discussion: [created]` | 组织级新建 Discussion |
| `discussion_comment: [created]` | 新评论；正文含 `<!-- csm-qa-bot -->` 标记的评论（bot 自己发的）不触发，避免循环 |
| `repository_dispatch: org_msg_router` | Cloudflare Worker 中继（[`webhook/`](../../webhook)），作为备用路径 |
| `workflow_dispatch` | 手动分类与 dry-run，可填 `discussion_number`、`comment_body`、`comment_author`、`category_name`、`dry_run` |

## 3. 依赖配置

| 类型 | 名称 | 作用 |
|------|------|------|
| Secret | `CSM_QA_GH_TOKEN` | checkout、读写 Discussions、发布回复（Fine-grained PAT） |
| Secret | `LLM_API_KEY` | 调用 LLM 做意图分类与问答 |
| Secret | `WEBHOOK_APP_ID`、`WEBHOOK_APP_PRIVATE_KEY` | 以 GitHub App 身份发出组织邀请 |
| Variable | `JOIN_FOLLOW_ORG` | 加入条件：需关注的账号，默认 `NEVSTOP-LAB` |
| Variable | `JOIN_STAR_REPOS` | 加入条件：需 Star 的仓库，默认 4 个 CSM 核心仓库 |
| Variable | `JOIN_DEFAULT_TEAM` | 邀请后加入的团队，默认 `csm-community` |

## 4. 缓存策略

使用拆分式 `actions/cache/restore` + `actions/cache/save`，仅在 `QA` 分支 restore RAG 相关缓存，`JOIN` / `OTHER` 不加载模型。

| 缓存 | Key | 内容 |
|------|-----|------|
| `venv` | `<os>-venv-py3.11-<requirements-bot.txt 哈希>` | `uv` 构建的 `.venv` |
| `huggingface` | `<os>-hf-BAAI-bge-small-zh-v1.5` | embedding 模型 |
| `vector store` | `<os>-vectorstore-<requirements 哈希>-<csm-wiki HEAD SHA>` | ChromaDB 向量库、`csm-wiki/wiki_source.json`、`csm-wiki/remote` |

## 5. 并发控制

```yaml
concurrency:
  group: org-router-${{ ... discussion_number }}
  cancel-in-progress: false
```

按 Discussion 编号分组：不同 Discussion 可并行，同一条串行；`cancel-in-progress: false` 避免打断正在发布的回复。

## 6. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| 新成员没收到邀请 | 查 `JOIN` 步骤日志：未关注组织或未 Star 指定仓库会给出提示；条件可通过 Variables 调整 |
| 问答不回复 | 确认 Discussion 分类是 Q&A；检查 `LLM_API_KEY` / `CSM_QA_GH_TOKEN` 是否过期；用 `workflow_dispatch` + `dry_run=true` 复现 |
| 出现重复回复 | 确认评论正文里的 `<!-- csm-qa-bot -->` 标记未被改动，`if` 依赖它过滤 bot 自身评论 |
| Worker 未部署 | 原生 `discussion` / `discussion_comment` 事件可直接触发，不依赖 Worker |
| 需要回滚到旧 bot 链路 | Worker 改发 `org_discussion_created`，并启用 [`csm-discussion-bot.yml`](./csm-discussion-bot.md) 的回滚通道 |
| 缓存不命中 | Key 里带 csm-wiki HEAD SHA，wiki 有新提交时向量库会重建，属预期行为 |

## 7. 本地调试

```bash
pip install -r requirements-bot.txt

# 只做分类，不发布回复
python scripts/router.py --classify-only --discussion-number 123 --comment-body "怎么加入组织？"

# 模拟某个意图的处理流程
python scripts/router.py --intent QA --comment-body "CSM 如何做超时处理？" --dry-run
```

测试：`pytest tests/test_router.py -v`

## 8. 关联文档

- [`csm-discussion-bot.md`](./csm-discussion-bot.md)：全量扫描与回滚通道
- [`webhook/README.md`](../../webhook/README.md)：Cloudflare Worker 中继与 `org_msg_router` 派发
