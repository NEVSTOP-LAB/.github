# Org Membership Cleanup

- **Workflow 文件**：[`.github/workflows/org-membership-cleanup.yml`](../../.github/workflows/org-membership-cleanup.yml)
- **脚本**：[`scripts/org_membership_cleanup.py`](../../scripts/org_membership_cleanup.py)

## 1. 功能概述

每日自动检查 NEVSTOP-LAB 组织成员的活跃度，对过去 **14 天**内无任何公开贡献的成员执行**逐级降级**，直到移出组织。

核心规则：

| 规则 | 说明 |
|------|------|
| **锚点豁免** | `CSM-Developer` 团队永久豁免，不受任何检查影响 |
| **层级自动发现** | 以 `csm-developer` 为锚点，通过 GitHub API 的 `parent` 字段向上追溯完整团队层级链（如 `csm-community → csm-module-author → csm-developer`），**不硬编码团队名称** |
| **逐级降级** | 无贡献者每次降一级：`CSM-Module-Author` → `CSM-Community` → 移出组织 |
| **滑动窗口** | 每个用户的 14 天窗口从"上次贡献时间"起算。若在检查窗口内发现贡献，`last_check` 更新为最近贡献时间（而非检查当天），下次检查从该时间起算 |
| **首次处理** | 未记录的新用户立即触发检查 |

## 2. 贡献判定标准

通过 GitHub Search API 综合查询以下三类公开活动（任一命中即视为"有贡献"）：

| 类别 | 搜索范围 | API |
|------|----------|-----|
| Issues / PRs（作者） | `org:NEVSTOP-LAB author:{user}` | `GET /search/issues` |
| Issues / PRs（被指派） | `org:NEVSTOP-LAB assignee:{user}` | `GET /search/issues` |
| Commits | `org:NEVSTOP-LAB author:{user}` | `GET /search/commits` |

> 三类查询均按时间倒序取最近 5 条；取所有命中结果中最新的时间作为"最近贡献时间"。

## 3. 触发条件与频率

| 触发 | 说明 |
|------|------|
| `schedule: '0 1 * * *'` | UTC 1:00 = **北京时间每天 09:00** 自动运行 |
| `workflow_dispatch` | 在 Actions 页面手动触发，支持 `--dry-run` 仅输出日志不实际操作 |

## 4. 降级操作细节

| 当前级别 | 操作 | 说明 |
|----------|------|------|
| `CSM-Module-Author` | 从该团队移除 | 用户自然留在父团队 `CSM-Community` |
| `CSM-Community` | 先移除 `CSM-Community` 团队 → 检查是否在其他团队中 → 有其他团队则保留组织身份，无则移出组织 | 避免误移除仍活跃于其他项目的成员 |
| `CSM-Developer` | 不做任何操作（锚点豁免） | — |

降级后 `last_check` 重置为当前时间，14 天后再次进入检查窗口。

## 5. 状态文件

检查状态持久化在 [`data/member_check_state.json`](../../data/member_check_state.json)：

```json
{
  "_comment": "Per-user state for org-membership-cleanup workflow. DO NOT edit manually.",
  "_schema": {
    "users": {
      "<github-username>": {
        "last_check": "ISO-8601 datetime",
        "team": "current CSM team slug or 'removed'"
      }
    }
  },
  "users": {
    "alice": {
      "last_check": "2025-06-20T08:30:00+00:00",
      "team": "csm-module-author"
    },
    "bob": {
      "last_check": "2025-06-24T01:00:00+00:00",
      "team": "csm-community"
    }
  }
}
```

- `_schema`：自描述字段，记录 `users` 中各字段的含义与格式（仅文档作用，不影响运行时逻辑）
- `last_check`：ISO-8601 UTC 时间，距上次检查或最近贡献的时间
- `team`：用户当前所在 CSM 团队 slug（运行时以 API 查询为准，此处仅作记录）
- `"removed"` 表示该用户已从组织移除

Workflow 每次运行后将状态文件 commit 回仓库（无变更则不提交）。

## 6. Secrets

| Secret | 作用 | 备注 |
|--------|------|------|
| `SYNC_GITHUB_TOKEN` | checkout、所有 GitHub API 调用、push 状态文件 | 需 `admin:org`（classic PAT）或 Organization Members: Read + Write（fine-grained PAT），同时具备 `repo` scope 用于 push |

> 与 discussion bot 不同，本 workflow 直接使用 `SYNC_GITHUB_TOKEN` 鉴权，不经过 GitHub App JWT 流程。

## 7. 并发控制

```yaml
concurrency:
  group: org-membership-cleanup
  cancel-in-progress: true
```

同一时间最多一个实例运行；手动触发时会取消正在运行的旧实例。

## 8. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| 新增 CSM 子团队 | 若新团队在 `csm-developer` 的 parent 链上，脚本会自动发现；若新团队在其他层级，需确认其 `parent` 设置正确 |
| 锚点团队改名 | 修改脚本顶部 `ANCHOR_TEAM` 常量，提交后即时生效 |
| 需要调整检查周期 | 修改脚本顶部 `CHECK_INTERVAL_DAYS` 常量 |
| 成员被误降级 | 手动将其加回原团队；下次检查时会自动更新状态文件中的 `team` 字段 |
| Token 过期 | 轮换 `SYNC_GITHUB_TOKEN`，确认新 token 具备 `admin:org` + `repo` 权限 |
| 调试降级逻辑 | 在 Actions 页面用 `workflow_dispatch` 触发并选择 `dry_run: true`，仅输出日志 |

## 9. 修改注意事项

- 团队层级链通过 API 动态发现，**不要硬编码团队名称或层级数量**。
- cron 改为 UTC 时间，注释中标明对应北京时间。
- 修改降级逻辑后建议先用 `--dry-run` 手动触发验证输出。
- 状态文件（`data/member_check_state.json`）由 workflow 自动维护，**不要手工编辑**。
