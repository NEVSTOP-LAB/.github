# Workflow 维护文档索引

本目录是 [`.github/workflows/`](../../.github/workflows) 下各 workflow 的**详细维护手册**。
顶层 [`README.md`](../../README.md) 仅提供概览（功能、触发、频率），日常排错、密钥轮换、修改注意事项请查阅本目录下对应文档。

## 文档列表

| Workflow 文件 | 维护文档 | 主要用途 |
|---------------|----------|----------|
| [`csm-discussion-bot.yml`](../../.github/workflows/csm-discussion-bot.yml) | [csm-discussion-bot.md](./csm-discussion-bot.md) | 组织级 Q&A 全量扫描与回滚通道；实时问答由 `org-router.yml` 接管 |
| [`org-router.yml`](../../.github/workflows/org-router.yml) | [org-router.md](./org-router.md) | 组织级 Discussion 意图分类与 JOIN / QA / OTHER 分派 |
| [`org-membership-cleanup.yml`](../../.github/workflows/org-membership-cleanup.yml) | [org-membership-cleanup.md](./org-membership-cleanup.md) | 不活跃组织成员逐级降级；当前在 Actions 中为禁用状态 |
| [`sync-to-gitee.yml`](../../.github/workflows/sync-to-gitee.yml) | [sync-to-gitee.md](./sync-to-gitee.md) | GitHub → Gitee 镜像同步 |
| [`update-profile-readme.yml`](../../.github/workflows/update-profile-readme.yml) | [update-profile-readme.md](./update-profile-readme.md) | 串行刷新 VIPM 下载量、Topics 聚合、CSM Modsets、Star History |

## 通用约定

为便于维护，几条约定在所有"自动更新"类 workflow 中保持一致：

1. **统一鉴权**：checkout 与 push 均使用 `secrets.SYNC_GITHUB_TOKEN`，避免使用默认 `GITHUB_TOKEN` 导致提交被忽略 workflow 触发器。
2. **并发互斥**：每个 workflow 都使用 `concurrency.group` 锁同名任务，`cancel-in-progress: true`（数据更新类）确保不会出现两次写入打架；discussion bot 使用 `cancel-in-progress: false` 以免漏回复。
3. **重试推送**：`update-profile-readme` workflow 在 `git push` 失败时会执行 `git fetch origin main && git rebase origin/main` 后重试，最多 3 次再判定失败，避免与并发的人工提交冲突。
4. **最小提交**：脚本写完文件后由 workflow 检测 `git diff --staged --quiet`，无变更直接退出，不产生空提交。
5. **手动触发**：所有 workflow 都保留 `workflow_dispatch` 入口，便于在 Actions 页面手动重跑或调试。

## 修改 workflow 时的检查清单

在合并对 `.github/workflows/*.yml` 或 `scripts/*.py` 的修改前，建议确认：

- [ ] cron 表达式时区已明确写在注释中（GitHub Actions cron 一律使用 UTC）。
- [ ] 新增/修改的 secret 已在 [`README.md`](../../README.md#-涉及的-secrets) 与本目录对应文档中同步。
- [ ] push 步骤仍保留 fetch+rebase 的 3 次重试循环（防止并发写入失败）。
- [ ] 若新增缓存，使用 `actions/cache/restore` + `actions/cache/save` 拆分写法并配合 `if: always()`，避免 job 提前失败时缓存永远写不回去（参见 `csm-discussion-bot.yml` 注释）。
- [ ] 改动后用 `workflow_dispatch` 至少手动跑一次成功。
