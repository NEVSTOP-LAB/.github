# 组织加入 Discussion Bot 实施计划

> 纯 LLM 路由方案：Worker 统一 dispatch → LLM 三分类 → 分派逻辑

## 整体流程

```
Webhook → Worker (统一 dispatch: org_msg_router) → router.py → LLM 分类 → JOIN 逻辑 / QA 逻辑 / OTHER 引导
```

## 生效范围

- **JOIN 请求**：任意分类下的组织 Discussion 均生效
- **QA 请求**：仅 Q&A 分类下生效（非 Q&A 则引导用户到 Q&A 区）
- **仓库级 Discussion**：保持不变，由现有 `csm-discussion-bot.yml` 继续处理

## 实施步骤

### 步骤 1 — 修改 `webhook/cloudflare-worker.js`（~8 行）

将 `sendDispatch` 中的 `event_type` 从 `"org_discussion_created"` 改为 `"org_msg_router"`，并在 `client_payload` 中附带以下字段（避免 Router 再次拉取）：

```javascript
client_payload: {
  discussion_number: discussionNumber,
  comment_body: (payload?.comment?.body || "").slice(0, 500),
  comment_author: (payload?.comment?.user?.login || ""),
  category_name: (payload?.discussion?.category?.name || ""),
  source: "webhook",
}
```

> 原有验签、JWT 签发、Installation Token 换取逻辑完全不变。

### 步骤 2 — 新建 `.github/workflows/org-router.yml`（~70 行）

```yaml
name: Org Discussion Router

on:
  repository_dispatch:
    types: [org_msg_router]
  workflow_dispatch:
    inputs:
      discussion_number:
        required: true
      dry_run:
        default: 'false'

jobs:
  route:
    runs-on: ubuntu-latest
    concurrency:
      group: org-router-${{ github.event.client_payload.discussion_number || github.event.inputs.discussion_number }}
      cancel-in-progress: false
    permissions:
      contents: read
      discussions: write
    steps:
      - uses: actions/checkout@v4
        with:
          token: ${{ secrets.CSM_QA_GH_TOKEN }}
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install -r requirements-bot.txt
      - name: Route & Process
        env:
          CSM_QA_GH_TOKEN: ${{ secrets.CSM_QA_GH_TOKEN }}
          LLM_API_KEY: ${{ secrets.LLM_API_KEY }}
          GITHUB_REPOSITORY: ${{ github.repository }}
        run: |
          python scripts/router.py \
            --discussion-number ${{ github.event.client_payload.discussion_number || github.event.inputs.discussion_number }} \
            --comment-body "${{ github.event.client_payload.comment_body || '' }}" \
            --comment-author "${{ github.event.client_payload.comment_author || '' }}" \
            --category-name "${{ github.event.client_payload.category_name || '' }}" \
            ${{ github.event.inputs.dry_run == 'true' && '--dry-run' || '' }}
```

### 步骤 3 — 新建 `scripts/router.py`（~280 行）

#### 模块结构

```
router.py
├── 全局常量
│   ├── BOT_MARKER = "<!-- org-router-bot -->"
│   ├── JOIN_CONDITIONS = { star_repos: [...], contribution_days: 30 }
│   └── INTENT_CLASSIFY_PROMPT  (LLM 分类提示词)
│
├── class GQL
│   └── query(gql, variables)   最小化 GraphQL 客户端
│
├── LLM 意图分类
│   ├── classify_intent(comment_body) → "JOIN" | "QA" | "OTHER"
│   └── 失败时降级为关键词正则匹配
│
├── JOIN 逻辑（独立函数，不依赖 discussion_bot.py）
│   ├── _check_star(token, username, owner, repo)          REST API 公开检查
│   ├── _check_commits(gql, username, org, days)           GraphQL 搜索 commits
│   ├── _check_issues_prs(gql, username, org, days)        GraphQL 搜索 Issue/PR
│   ├── check_all_conditions(token, username, org)         汇总所有条件
│   ├── build_condition_report(username, all_met, results) Markdown 表格报告
│   ├── send_invitation(token, org, username)              REST API 发送邀请
│   └── post_reply(token, discussion_id, body)             GraphQL 发布评论
│
├── QA 逻辑（导入 discussion_bot.py 函数，不改原文件）
│   ├── 仅 Q&A 分类 → import GitHubGraphQL, compute_reply_plan, build_reply 等
│   ├── 初始化 CSM_QA.from_env()（延迟加载，JOIN/OTHER 路径不触发）
│   └── 生成回答并发布评论
│
├── fetch_discussion(token, owner, repo, number)  拉取 discussion 元信息
│
└── main()
    ├── 解析 CLI 参数
    ├── classify_intent() → 三分类
    ├── INTENT_JOIN  → 条件检测 + 邀请发送
    ├── INTENT_QA    → Q&A 分类? 回答 : 引导
    └── INTENT_OTHER → 友好回复
```

#### LLM 分类 Prompt（内置在脚本中）

```
你是一个 GitHub 讨论区路由助手。请判断以下评论属于哪一类意图，只回复一个标签：

标签说明：
- JOIN：用户申请加入组织、想成为成员
- QA：用户提出技术问题或框架使用问题（可能包含 join/加入 等技术术语）
- OTHER：其他与上述无关的评论

用户评论：
'''
{comment_body}
'''

只回复标签名（JOIN / QA / OTHER），不要任何解释。
```

- 使用 DeepSeek API（已有 `LLM_API_KEY`）
- `temperature=0`，`max_tokens=10`
- LLM 调用失败 → 降级正则 `/join|加入|申请|apply/` → JOIN，否则 QA

#### 条件检测详情

| 条件 | API | 说明 |
|------|-----|------|
| Star 指定仓库 | `GET /users/{user}/starred/{owner}/{repo}` | 204 = 已 Star，404 = 未 Star |
| 近期 commit | GraphQL `search(type: COMMIT, query: "author:{user} org:{org} committer-date:>={date}")` | 搜索近 30 天 commit |
| 近期 Issue/PR | GraphQL `search(type: ISSUE, query: "author:{user} org:{org} created:>={date}")` | 搜索近 30 天 Issue/PR |

#### 报告格式

```
## 📋 @{username} 的加入申请审核结果

| 条件 | 状态 | 详情 |
|------|:----:|------|
| ⭐ Star {owner}/{repo} | ✅ | 已 Star |
| 📝 近 30 天 commit | ❌ | 近 30 天无 commit 记录 |
| 🐛 近 30 天 Issue/PR | ✅ | 近 30 天有 5 条（详情...） |

{通过时} 🎉 全部通过 (3/3)！邀请已发送，请查收 GitHub 邮件并点击 Accept。
{未通过} 🔴 需要全部满足，当前 2/3 项通过。满足后再次发送 /join 重试。
```

### 步骤 4 — GitHub App 配置

在现有 GitHub App（`CSM-QA-Bot-Webhook-Relay`）设置页：

1. **Organization permissions → Members** → 设为 `Read & Write`
2. 保存后**重新安装 App** 到 NEVSTOP-LAB 组织

### 步骤 5 — 灰度验证

1. **手动触发测试 JOIN**：Actions 页面 → `Org Discussion Router` → `workflow_dispatch`，输入一条 Q&A 区 Discussion 编号（该 Discussion 最后一条评论为 `/join`），dry-run 查看报告
2. **手动触发测试 QA**：同样方式，输入一条技术问题 Discussion，确认正常回答
3. **边界测试**：用 `"LabVIEW 怎么 join 数组"` 确认分类为 QA 而非 JOIN
4. **LLM 降级测试**：临时改错 `LLM_API_KEY`，确认正则降级路由正常工作
5. **正式上线**：Worker 代码部署后，自动路由生效
6. **回滚方案**：若出问题，Worker 中改回旧 `event_type` + Actions 禁用 `org-router.yml`

## 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `webhook/cloudflare-worker.js` | 修改 ~8 行 | 统一 `event_type` + 附加 payload |
| `scripts/router.py` | **新建** ~280 行 | LLM 路由 + JOIN/QA/OTHER 逻辑 |
| `.github/workflows/org-router.yml` | **新建** ~70 行 | 统一 workflow 入口 |
| `scripts/discussion_bot.py` | 不碰 | 零风险 |
| `.github/workflows/csm-discussion-bot.yml` | 不碰 | 保留仓库级触发和手动扫描 |
| `tests/test_discussion_bot.py` | 不碰 | 零风险 |

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM 分类温度 | `temperature=0` | 确定性输出，同一评论不反复变化 |
| LLM 降级 | 失败降级为正则匹配 | 保证服务不中断 |
| Worker 传入 comment_body | 附带在 payload 中 | Router 省一次 GraphQL 查询 |
| CSM_QA 初始化 | 延迟到 QA 路径才 import | JOIN/OTHER 路径秒级完成 |
| JOIN 不限分类 | 任意 Discussion 下 `/join` 均生效 | 入口灵活 |
| QA 限制 Q&A 分类 | 非 Q&A 则引导用户过去 | 防止技术回答溢出到其他分类 |

## 未来扩展

Router 架构自然支持添加新意图：

```
LLM 分类 → INTENT_REPORT_BUG  → 自动创建 Issue
         → INTENT_FEATURE_REQ → 自动创建 Discussion
         → INTENT_FAQ         → 检索 FAQ 文档回复
         ...
```

只需在分类 Prompt 和路由表中各加一行即可。
