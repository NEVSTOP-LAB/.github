# Update Star History

- **Workflow 文件**：[`.github/workflows/update-star-history.yml`](../../.github/workflows/update-star-history.yml)
- **核心脚本**：[`scripts/update_star_history.py`](../../scripts/update_star_history.py)
- **写入目标**：[`Star-History.md`](../../Star-History.md)

## 1. 功能概述

聚合 NEVSTOP-LAB 组织所有仓库的 stargazer 数据，生成：

- **累计 star 增长曲线**（Mermaid `xychart-beta`）
- **Top N 最受欢迎仓库榜单**

并以更新时间戳一并写入 `Star-History.md`。文件由 workflow 自动维护，不要手工编辑数据部分。

## 2. 触发条件与频率

| 触发 | 说明 |
|------|------|
| `schedule: '0 */8 * * *'` | 每 8 小时一次：UTC 00:00 / 08:00 / 16:00 |
| `workflow_dispatch`       | 手动触发 |

频率较高是为了让首页的 star 数据保持"近一日"新鲜度；GitHub API 的速率配额对 PAT 而言完全够用。

## 3. 关键环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GITHUB_TOKEN`         | `secrets.SYNC_GITHUB_TOKEN` | 拉取 stargazers / repo 列表 |
| `EXCLUDE_USERS`        | `nevstop,yao0928,KivenJia` | 逗号分隔；从统计中排除组织成员/管理员，使排行体现真实社区热度 |
| `PRIVATE_VISIBLE_CHARS`| `10` | 私有仓库名脱敏：仅展示前 N 字符 |
| `TOP_N`                | `10` | 排行榜显示前 N 个仓库 |

调整这几个变量是日常运维的主要"配置入口"，无需改动脚本。

## 4. Secrets

| Secret | 作用 |
|--------|------|
| `SYNC_GITHUB_TOKEN` | API 访问 + push 提交，需 `repo` + `read:org` |

## 5. 提交流程

与 `update-sorted-tags` 一致：

1. `git add Star-History.md`
2. 无变更直接退出。
3. 提交信息：`chore: update Star History`。
4. push 失败 → `git fetch origin main && git rebase origin/main`，**重试 3 次**。

## 6. 并发控制

```yaml
concurrency:
  group: update-star-history
  cancel-in-progress: true
```

## 7. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| 排行榜不准 / 想增加排除人员 | 修改 `EXCLUDE_USERS`，逗号分隔且**不要加空格**（脚本按 `,` 直接切分，多余空格会被当成用户名的一部分而匹配不到）。例：`nevstop,yao0928,KivenJia` ✅；`nevstop, yao0928` ❌ |
| 想展示更多仓库 | 增大 `TOP_N`；同时考虑 README 渲染长度 |
| 私有仓库泄露名称 | 减小 `PRIVATE_VISIBLE_CHARS`（最低可设为 `0` 完全隐藏） |
| Mermaid 图渲染异常 | 多半是数据点超出 `y-axis` 上限，脚本自动按当前最大值取整；如手工编辑过 `Star-History.md`，删除该文件后重跑可重置 |
| API 限流 | 切换 `SYNC_GITHUB_TOKEN` 为权限相同但更高配额的 PAT；GitHub App token 单小时配额更高，可考虑迁移 |
| push 冲突 (3 次) | 与人工同时编辑 `Star-History.md` 才会出现，不应发生；如出现，手动 rebase 后重跑 |

## 8. 修改注意事项

- 修改图表样式时，请保持 Mermaid 语法在 GitHub renderer 可渲染范围内（Mermaid 版本会随 GitHub 更新）。
- 时间戳使用 `UTC+8`（北京时间），与组织主用户群体匹配；如改时区，脚本与 README 标识需同步更新。
