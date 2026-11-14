# Update Profile Docs

- **Workflow 文件**：[`.github/workflows/update-profile-readme.yml`](../../.github/workflows/update-profile-readme.yml)
- **写入目标**：[`csm-framework.md`](../../csm-framework.md)、[`csm-modsets.md`](../../csm-modsets.md)、[`Star-History.md`](../../Star-History.md)

## 1. 功能概述

一次运行按固定顺序执行四个脚本，把 [CSM Framework 详解](../../csm-framework.md) 与 `Star-History.md` 里的自动区块全部刷新，最后一次性提交推送。组织主页 `profile/README.md` 是手写内容，不由本 workflow 修改。

| 顺序 | 脚本 | 写入文件 | 内容 |
|------|------|----------|------|
| 1 | [`update_vipm_downloads.py`](../../scripts/update_vipm_downloads.py) | `csm-framework.md` | `VIPM_DOWNLOADS` 区块：当月安装量图表 + 历史快照表 |
| 2 | [`update_sorted_tags.py`](../../scripts/update_sorted_tags.py) | `csm-framework.md` | `SORTED_TAGS` 区块：按 topic 聚合的仓库数量 |
| 3 | [`update_csm_modsets.py`](../../scripts/update_csm_modsets.py) | `csm-framework.md`、`csm-modsets.md` | `CSM_MODSETS` 区块：CSM 可复用模块列表 |
| 4 | [`update_star_history.py`](../../scripts/update_star_history.py) | `Star-History.md` | 累计 star 增长曲线与 Top N 排行 |

写入位置由 `csm-framework.md` 里的 `<!-- *_START/END -->` 标记块定位，不要手工删除或改动这些注释。

> [!NOTE]
> `VIPM_DOWNLOADS` 区块由脚本整体重建：
>
> - 图表只画**当月**各包的累计安装量，月份写在标题里；`bar` 的值与 `x-axis` 类目一一对应，月份不再作为数据值混进数组。
> - 历史快照在 `<details>` 折叠表里，默认显示最近 24 个月，可用 `--visible-months` 调整。
> - 超过 24 个月的快照保留在文件中的 HTML 注释里，数据不删除；脚本会连同注释里的行一起解析。
> - 同月重复运行只覆盖当月那一行，跨月才新增。

## 2. 触发条件与频率

| 触发 | 说明 |
|------|------|
| `schedule: '0 16 * * *'` | 每天北京时间 00:00（UTC 16:00） |
| `workflow_dispatch` | 手动触发；发布新 VIPM 包或调整仓库 topic 后立即刷新 |

## 3. 关键环境变量

| 变量 | 默认值 | 作用 |
|------|--------|------|
| `GITHUB_TOKEN` | `secrets.SYNC_GITHUB_TOKEN` | 列举组织仓库与 topic、拉 stargazers、push 提交 |
| `ORG` | `NEVSTOP-LAB` | 目标组织名 |
| `MIN_TAG_COUNT` | `1` | 某 topic 下仓库数 ≥ 此值才展示，调高可隐藏冷门 topic |
| `EXCLUDE_USERS` | `nevstop,yao0928,KivenJia` | star 排行排除的账号，逗号分隔且**不要加空格** |
| `PRIVATE_VISIBLE_CHARS` | `10` | 私有仓库名脱敏展示的前 N 个字符，`0` 为完全隐藏 |
| `TOP_N` | `10` | star 排行显示前 N 个仓库 |

VIPM 步骤只访问 VIPM 站点，不需要 `GITHUB_TOKEN`；包列表与 README 锚点内嵌在脚本里，新增包需要改脚本。

## 4. Secrets

| Secret | 作用 |
|--------|------|
| `SYNC_GITHUB_TOKEN` | checkout、push、GitHub API；需 `repo`（私有仓库统计也要）与 `read:org` |

## 5. 提交流程

1. `git add csm-framework.md csm-modsets.md Star-History.md`
2. `git diff --staged --quiet` 时直接退出，不产生空提交。
3. 有变更时提交 `chore: update profile docs`。
4. `git push` 失败时执行 `git fetch origin main && git rebase origin/main`，**最多重试 3 次**，仍失败则 job fail。

> [!WARNING]
> 提交步骤只 `git add` 这三个真实会变的文件。把不存在的路径写进 `git add` 会让整条命令失败并被 `|| true` 吞掉，表现为「workflow 每天 success 但区块冻结数周」。新增写入目标时，把该文件同时加进 `git add`。

## 6. 并发控制

```yaml
concurrency:
  group: update-profile-readme
  cancel-in-progress: true
```

四个脚本在同一 job 内顺序执行，不会互相覆盖；与人工编辑的冲突由 rebase 重试兜底。

## 7. 常见维护场景

| 场景 | 排查/操作 |
|------|-----------|
| 区块长期不更新 | 先看 run 是否产生了提交；success 但无提交通常是脚本没找到标记块或抓取失败 |
| 表里少了某个仓库或 topic | 仓库需设置 topic，且该 topic 下仓库数 ≥ `MIN_TAG_COUNT` |
| 想隐藏小众 topic | 调高 `MIN_TAG_COUNT` |
| 星标排行不准 / 想增加排除人员 | 改 `EXCLUDE_USERS`，逗号分隔且不要加空格 |
| 私有仓库名露出 | 调小 `PRIVATE_VISIBLE_CHARS` |
| 新发布的 VIPM 包没有下载数 | 在脚本中追加包条目，并在 `csm-framework.md` 加对应锚点 |
| VIPM 数字不动或为 0 | VIPM 页面改版导致解析失败，查看脚本日志最后输出并更新解析逻辑 |
| Mermaid 图渲染异常 | VIPM 区块由脚本整体重建，不要手改图表代码；`Star-History.md` 可直接删除后重跑重建 |
| API 限流 | 换配额更高的 PAT；GitHub App token 单小时配额更高 |
| push 冲突 3 次 | 与人工提交改了同一文件，手动 rebase 后重跑一次 |

## 8. 修改注意事项

- 改 cron 时保留北京时间注释，避免后续维护者按 UTC 误判。
- Python 版本钉在 `3.12`，脚本升级到新语法时同步改 workflow。
- 新增脚本要放在 commit 步骤之前，并把写入文件加进 `git add`。
- 脚本写入依赖 `<!-- *_START/END -->` 标记块，删掉标记就没有定位点。
