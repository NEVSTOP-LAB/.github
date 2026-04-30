# Update Sorted Tags

- **Workflow 文件**：[`.github/workflows/update-sorted-tags.yml`](../../.github/workflows/update-sorted-tags.yml)
- **核心脚本**：[`scripts/update_sorted_tags.py`](../../scripts/update_sorted_tags.py)
- **写入目标**：[`profile/README.md`](../../profile/README.md)

## 1. 功能概述

定期遍历 NEVSTOP-LAB 组织下的所有公开仓库，按 GitHub **topic** 聚合并排序，生成"按标签分类"的仓库索引表，写入 `profile/README.md` 中相应区块（脚本通过特定标记定位区块，详见脚本内常量）。

## 2. 触发条件与频率

| 触发 | 说明 |
|------|------|
| `schedule: '0 0 * * 0'` | 每周日 00:00 UTC 自动运行（topic 信息变化频率较低，无需更频繁） |
| `workflow_dispatch`     | 手动触发，新增/调整仓库 topic 后立刻生效 |

## 3. 关键环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GITHUB_TOKEN`   | `secrets.SYNC_GITHUB_TOKEN` | 列举组织仓库 + topic |
| `ORG`            | `NEVSTOP-LAB` | 目标组织名 |
| `MIN_TAG_COUNT`  | `1` | 仅当某 topic 下仓库数 ≥ 此值时展示，调高可隐藏冷门 topic |

## 4. Secrets

| Secret | 作用 |
|--------|------|
| `SYNC_GITHUB_TOKEN` | 列举仓库 + push 提交，需 `repo` 权限（私有仓库统计也需要） |

## 5. 提交流程

脚本运行后由 workflow 内联 shell 完成提交：

1. `git add profile/README.md`
2. 若 `git diff --staged --quiet` → 无变更，正常退出。
3. 否则提交：`chore: update sorted tags`。
4. `git push` 失败时执行 `git fetch origin main && git rebase origin/main`，**最多重试 3 次**，仍失败则 job fail。

## 6. 并发控制

```yaml
concurrency:
  group: update-sorted-tags
  cancel-in-progress: true
```

## 7. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| 表格里少了某个仓库 | 该仓库需在 GitHub 上设置至少一个 topic；topic 数量需 ≥ `MIN_TAG_COUNT` |
| 想隐藏小众 topic | 调高 workflow 中的 `MIN_TAG_COUNT` 环境变量 |
| 写入位置错乱 | 脚本基于 `profile/README.md` 中的标记块替换，不要手动删除/改动这些 HTML 注释标记 |
| push 失败 (3 次) | 通常是与其他 update-* workflow 同时写 `profile/README.md` 又冲突解不开；手动重跑一次即可，concurrency 已防同名重入 |

## 8. 修改注意事项

- 此 workflow 与 [`update-vipm-downloads.yml`](./update-vipm-downloads.md) 同样写 `profile/README.md`。两者通过不同 cron 时间错开（VIPM 每天 17:00 UTC，本 workflow 每周日 00:00 UTC），冲突概率低；但如新增其他写 `profile/README.md` 的 workflow，需考虑加 `concurrency` 共用 group 或合并脚本。
- 如调整 `ORG` 以复用到其他组织，建议同步把 secret 重命名以免误用。
