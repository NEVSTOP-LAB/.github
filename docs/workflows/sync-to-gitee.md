# Sync GitHub to Gitee

- **Workflow 文件**：[`.github/workflows/sync-to-gitee.yml`](../../.github/workflows/sync-to-gitee.yml)
- **使用的 Action**：[`NEVSTOP-LAB/GitHub-Gitee-Sync@main`](https://github.com/NEVSTOP-LAB/GitHub-Gitee-Sync)

## 1. 功能概述

将 NEVSTOP-LAB 组织在 GitHub 上的全部仓库（含 **releases** 与 **wiki**）同步到 Gitee 同名组织，作为国内访问镜像。同步方向为单向 `github2gitee`。

## 2. 触发条件与频率

| 触发 | 说明 |
|------|------|
| `schedule: '0 18 * * *'` | UTC 18:00 = **北京时间每天 02:00** 自动同步一次 |
| `workflow_dispatch`      | 在 Actions 页面手动触发（新增仓库、紧急同步时使用） |

## 3. 关键参数

| 参数 | 值 | 说明 |
|------|----|------|
| `github-owner` | `NEVSTOP-LAB` | 源组织 |
| `gitee-owner`  | `NEVSTOP-LAB` | 目标组织（Gitee 上需提前存在并授予 token 权限） |
| `account-type` | `org` | 按组织维度遍历仓库 |
| `sync-extra`   | `releases,wiki` | 在仓库本体之外额外同步 release 资产与 wiki |
| `show-private-repo-names` | `8` | 日志中私有仓库名只显示前 8 个字符，避免泄露 |
| `direction`    | `github2gitee` | 单向同步，禁止反向 |

## 4. Secrets

| Secret | 作用 | 备注 |
|--------|------|------|
| `SYNC_GITHUB_TOKEN` | 读取 GitHub 组织全部仓库（含私有） | 需 `repo` + `read:org` |
| `SYNC_GITEE_TOKEN`  | 在 Gitee 创建 / 推送仓库          | 在 Gitee → 设置 → 私人令牌生成，赋"projects/user_info/issues/notes/groups"权限 |

## 5. 并发控制

```yaml
concurrency:
  group: sync-github-to-gitee
  cancel-in-progress: true
```

同一时间最多一个同步任务在跑；手动触发时若已有运行实例，会取消旧的——这通常是期望行为，因为后触发的请求一般包含更新的状态。

## 6. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| 新仓库未同步过去 | ① 等待下一次定时；或在 Actions 页面手动触发；② 确认 Gitee 上目标组织 `SYNC_GITEE_TOKEN` 仍有创建仓库权限 |
| Gitee 上仓库被推空 / 损坏 | 在 Gitee 删除该仓库，下次运行会重新创建并完整推送 |
| 同步耗时持续上升 | 仓库数变多属正常；如某次卡在某仓库，查看日志最后一行的仓库名，单独到 Gitee 检查权限或手动删除后重试 |
| token 401/403 | 轮换 `SYNC_GITHUB_TOKEN` 或 `SYNC_GITEE_TOKEN`；token 通常 1 年到期，建议加日历提醒 |
| 想同步到其他平台 | 当前 Action 仅支持 Gitee，需要扩展时改 [`NEVSTOP-LAB/GitHub-Gitee-Sync`](https://github.com/NEVSTOP-LAB/GitHub-Gitee-Sync) 上游 |

## 7. 修改注意事项

- cron 改动后请在注释里同步说明对应北京时间，避免后续维护者被 UTC 误导。
- 如果未来加上 `branches`/`tags` 等额外过滤，注意 `sync-extra` 当前只识别 `releases,wiki`，新增字段需查 Action README。
