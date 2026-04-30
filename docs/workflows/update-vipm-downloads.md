# Update VIPM Downloads

- **Workflow 文件**：[`.github/workflows/update-vipm-downloads.yml`](../../.github/workflows/update-vipm-downloads.yml)
- **核心脚本**：[`scripts/update_vipm_downloads.py`](../../scripts/update_vipm_downloads.py)
- **写入目标**：[`profile/README.md`](../../profile/README.md)

## 1. 功能概述

抓取 NEVSTOP-LAB 在 [VIPM (VI Package Manager)](https://www.vipm.io/) 上发布的 LabVIEW 包的累计下载数，并将这些数字回填到 `profile/README.md` 中对应徽章/表格区域。

> VIPM 不提供官方下载量徽章服务，需要脚本通过其网页/接口抓取并回写。

## 2. 触发条件与频率

| 触发 | 说明 |
|------|------|
| `schedule: '0 17 * * *'` | UTC 17:00 = **北京时间每天 01:00** 自动同步 |
| `workflow_dispatch`      | 手动触发（发布新包后想立刻刷新可使用） |

## 3. 关键参数

脚本参数仅一项：写入目标文件 `profile/README.md`。包列表与定位锚点全部内嵌在脚本内（按 VIPM 包 URL/名称匹配 README 中标记），新增包时需修改脚本。

## 4. Secrets

| Secret | 作用 | 备注 |
|--------|------|------|
| `SYNC_GITHUB_TOKEN` | checkout + push | 抓取 VIPM 数据本身不需要鉴权 |

> 注意：与其他 `update-*` workflow 不同，本 workflow 在 `Update VIPM download counts` 步骤**未**显式 export `GITHUB_TOKEN`，因为脚本只访问 VIPM；如未来脚本需访问 GitHub API，请补充 env。

## 5. 提交流程

同 `update-sorted-tags` / `update-star-history`：

1. `git add profile/README.md`
2. 无变更则退出。
3. 提交信息：`chore: update VIPM download counts`。
4. push 失败 → `git fetch origin main && git rebase origin/main`，**重试 3 次**。

## 6. 并发控制

```yaml
concurrency:
  group: update-vipm-downloads
  cancel-in-progress: true
```

注意本 workflow 与 [`update-sorted-tags`](./update-sorted-tags.md) 都会写 `profile/README.md`。两者通过 cron 时间错开（VIPM 每天 17:00 UTC，sorted tags 每周日 00:00 UTC），但 `concurrency.group` **不同**，理论上仍可能与人工触发重叠；3 次 rebase 重试是兜底保护。

## 7. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| 新发布的包未出现下载数 | 在 `scripts/update_vipm_downloads.py` 中追加该包条目，并在 `profile/README.md` 加上对应锚点 |
| 数字明显不变化/为 0 | VIPM 网页改版导致抓取失败；查看脚本日志最后输出，更新解析逻辑 |
| 抓取被 VIPM 限流 | 适当降低 cron 频率或在脚本中加入 sleep；当前每天一次通常无风险 |
| Python 版本相关报错 | workflow 钉在 `python-version: '3.10'`，如脚本升级到新语法记得同步更新此版本号 |

## 8. 修改注意事项

- 改 cron 时请保留北京时间注释，避免后续维护者按 UTC 误判。
- 脚本对 README 的写入是基于标记块的局部替换；不要删除 README 中的 HTML 注释锚点，否则下一次运行将无法定位写入位置。
