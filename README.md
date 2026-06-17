# NEVSTOP-LAB/.github

本仓库是 [NEVSTOP-LAB](https://github.com/NEVSTOP-LAB) 组织的 **`.github` 默认仓库**，集中维护：

- 组织主页（`profile/README.md`，即访问 <https://github.com/NEVSTOP-LAB> 时展示的内容）
- 组织级 GitHub Discussions 问答机器人（CSM Q&A Bot）及其调研文档
- 跨仓库的自动化运维 workflow（同步、统计、排行等）

## 📑 目录结构

| 路径 | 说明 |
|------|------|
| [`profile/README.md`](./profile/README.md) | 组织 Profile 页面，展示项目矩阵、社区入口、AI-Wiki 等 |
| [`.github/workflows/`](./.github/workflows) | 本仓库运行的所有 GitHub Actions workflow，详见下文 |
| [`scripts/`](./scripts) | workflow 调用的 Python 脚本（discussion bot、统计脚本等） |
| [`tests/`](./tests) | discussion bot 的 pytest 单元测试 |
| [`csm-wiki/wiki_source.json`](./csm-wiki/wiki_source.json) | CSM-Wiki 远程仓库地址，bot RAG 检索的知识源 |
| [`requirements-bot.txt`](./requirements-bot.txt) | discussion bot 的 Python 依赖清单 |
| [`webhook/`](./webhook) | Cloudflare Worker 实时触发组织级 Discussion 回答的中继代码 |
| [`docs/workflows/`](./docs/workflows) | **各 workflow 的详细维护文档**（运维、排错、密钥说明） |
| [`docs/调研/`](./docs/调研) | csm_qa RAG 问答 SDK 的技术调研报告 |

> 顶层的 `Group-*.md`、`Star-History.md` 等文件由 [`profile/README.md`](./profile/README.md) 间接引用或由自动化 workflow 维护，请勿手工编辑被自动覆盖的部分。

## 🤖 自动化 Workflow 概览

下表汇总了本仓库当前运行的 6 个 workflow，详细维护说明请点击对应文档链接。

| Workflow | 文件 | 功能简介 | 触发条件 | 运行频率 |
|----------|------|----------|----------|----------|
| [CSM Q&A Discussion Bot](./docs/workflows/csm-discussion-bot.md) | [`csm-discussion-bot.yml`](./.github/workflows/csm-discussion-bot.yml) | 基于 RAG + LLM 自动回复组织级 Discussion 中 Q&A 分类下的提问与追问 | `discussion` / `discussion_comment` 事件、`repository_dispatch`（来自 Cloudflare Worker 实时中继）、`workflow_dispatch`（手动全量扫描，可 dry-run） | 事件驱动（实时） + 手动 |
| [Sync GitHub to Gitee](./docs/workflows/sync-to-gitee.md) | [`sync-to-gitee.yml`](./.github/workflows/sync-to-gitee.yml) | 将 NEVSTOP-LAB 组织下的所有仓库（含 releases、wiki）同步到 Gitee 镜像 | 定时 + `workflow_dispatch` | 每天 02:00（北京时间） |
| [Update Sorted Tags](./docs/workflows/update-sorted-tags.md) | [`update-sorted-tags.yml`](./.github/workflows/update-sorted-tags.yml) | 拉取组织所有仓库 topic 信息，按标签聚合后写回 `profile/README.md` | 定时 + `workflow_dispatch` | 每周日 00:00 UTC |
| [Update Star History](./docs/workflows/update-star-history.md) | [`update-star-history.yml`](./.github/workflows/update-star-history.yml) | 统计组织 star 历史与 Top 仓库排行，更新 `Star-History.md`（含 Mermaid 图表） | 定时 + `workflow_dispatch` | 每 8 小时一次（00:00 / 08:00 / 16:00 UTC） |
| [Org Membership Cleanup](./docs/workflows/org-membership-cleanup.md) | [`org-membership-cleanup.yml`](./.github/workflows/org-membership-cleanup.yml) | 每日检查组织成员活跃度，14 天无贡献者逐级降级（CSM-Developer 豁免）直至移出组织 | 定时 + `workflow_dispatch` | 每天 09:00（北京时间） |
| [Update VIPM Downloads](./docs/workflows/update-vipm-downloads.md) | [`update-vipm-downloads.yml`](./.github/workflows/update-vipm-downloads.yml) | 抓取 VIPM 包下载量并刷新 `profile/README.md` 中相关徽章数据 | 定时 + `workflow_dispatch` | 每天 01:00（北京时间） |

> [!NOTE]
> **Org Membership Cleanup 背后逻辑**
>
> - **锚点豁免**：以 `CSM-Developer` 为锚点，通过 GitHub API 的 team `parent` 字段动态发现完整团队层级链（`CSM-Community → CSM-Module-Author → CSM-Developer`），不硬编码团队名称。锚点团队永久豁免清理。
> - **滑动窗口**：每个用户 14 天窗口从**上次贡献时间**（而非上次检查时间）起算。若窗口内发现贡献，`last_check` 更新为最近贡献时间，窗口自动后移；无贡献者继续下一轮判定。
> - **逐级降级**：每次降一级 —— `CSM-Module-Author` → `CSM-Community` → 移出组织。
> - **贡献判定**：综合查询过去窗口内的 commits（`committer-date`）、authored issues/PRs（`created`）、assigned 且已关闭的 issues/PRs（`closed`）。任一命中即视为活跃。
> - **安全策略**：Search API 限速（403/429）时跳过该用户本轮检查（不降级），等待下次重试；dry-run 模式不落盘状态文件，避免错误推进检查窗口。
> - 状态持久化在 `data/member_check_state.json`，每天 workflow 运行后自动 commit 回仓库。详见 [维护文档](./docs/workflows/org-membership-cleanup.md)。

> 所有“自动更新”类 workflow 在 push 失败时会自动 `fetch + rebase origin/main` 最多重试 3 次，并使用 `secrets.SYNC_GITHUB_TOKEN` 进行鉴权与提交。

## 📚 其他文档简介

| 文档 | 简介 |
|------|------|
| [`profile/README.md`](./profile/README.md) | NEVSTOP-LAB 组织主页，介绍 CSM 框架、配套插件、社区入口、AI-Wiki，以及 star/topic 统计表格 |
| [`webhook/README.md`](./webhook/README.md) | Cloudflare Worker 中继的部署与配置说明：把组织级 Discussion 的 webhook 转发为 `repository_dispatch`，让 bot 实时响应而非等待定时扫描 |
| [`docs/workflows/README.md`](./docs/workflows/README.md) | Workflow 维护文档索引，按需查阅每个 workflow 的密钥、修改清单、常见故障 |
| [`docs/调研/README.md`](./docs/调研/README.md) | csm_qa RAG SDK 整体架构与各方向调研报告（LLM 接入、向量库、Token 优化、费用评估）入口 |
| [`Star-History.md`](./Star-History.md) | 由 `update-star-history.yml` 自动维护的组织 star 增长曲线与 Top 10 仓库排行 |
| `Group-*.md`（[CSM Framework](./Group-Communicable%20State%20Machine%28CSM%29%20Framework.md) · [Libraries](./Group-Libraries.md) · [Programming Environments](./Group-Programming%20Environments.md) · [Supplement of Base Functions](./Group-Supplement%20of%20Base%20Functions.md)） | 按主题分组列出组织内仓库的索引页（CSM 框架、库、编程环境、基础功能补充等） |

## 🔐 涉及的 Secrets

| Secret | 用于 |
|--------|------|
| `SYNC_GITHUB_TOKEN` | `update-*`、`sync-to-gitee` 与 `org-membership-cleanup` workflow 的 checkout / push / GitHub API |
| `SYNC_GITEE_TOKEN`  | `sync-to-gitee` 推送到 Gitee 的鉴权 |
| `CSM_QA_GH_TOKEN`   | discussion bot 读写组织级 Q&A discussions（Fine-grained PAT） |
| `LLM_API_KEY`       | discussion bot 调用 LLM（默认 deepseek-chat）所需的 API Key |

各 secret 的具体权限要求与轮换说明请见 [`docs/workflows/`](./docs/workflows) 下对应文档。
