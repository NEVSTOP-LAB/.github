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
// discussion 事件无 comment 对象 → 取 discussion.body + sender.login
const isDiscussionEvent = eventType === "discussion";
client_payload: {
  discussion_number: discussionNumber,
  comment_body: (isDiscussionEvent
    ? (payload?.discussion?.body || "")
    : (payload?.comment?.body || "")
  ).slice(0, 800),
  comment_author: isDiscussionEvent
    ? (payload?.sender?.login || "")
    : (payload?.comment?.user?.login || ""),
  category_name: (payload?.discussion?.category?.name || ""),
  event_type: eventType,
  source: "webhook",
}
```

> 原有验签、JWT 签发、Installation Token 换取逻辑完全不变。
>
> **防重过滤**：`router.py` 将复用现有 `<!-- csm-qa-bot -->` 标记（见步骤 3），
> Worker 第 77–87 行已有的 `csm-qa-bot` 过滤逻辑无需修改即可覆盖 Router 自身发出的评论，
> 不会引入新的无限循环。

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
          JOIN_FOLLOW_ORG: ${{ vars.JOIN_FOLLOW_ORG || 'NEVSTOP-LAB' }}
          JOIN_STAR_REPOS: ${{ vars.JOIN_STAR_REPOS || 'csm-core,API String,MassData,INIVariable' }}
        run: |
          python scripts/router.py \
            --discussion-number ${{ github.event.client_payload.discussion_number || github.event.inputs.discussion_number }} \
            --comment-body "${{ github.event.client_payload.comment_body || '' }}" \
            --comment-author "${{ github.event.client_payload.comment_author || '' }}" \
            --category-name "${{ github.event.client_payload.category_name || '' }}" \
            ${{ github.event.inputs.dry_run == 'true' && '--dry-run' || '' }}
```

> **依赖说明**：`requirements-bot.txt` 已包含 `csm-llm-qa`，但仅在 QA 路径才 import，
> JOIN / OTHER 路径不触发其加载。LLM 分类调用使用 Python stdlib `urllib`，无需额外依赖。
>
> **配置说明**：`JOIN_FOLLOW_ORG` / `JOIN_STAR_REPOS` 使用 GitHub Actions 变量（`vars`）而非 secret，
> 方便随时调整无需重新部署。在仓库 **Settings → Secrets and variables → Actions → Variables**
> 中创建即可。未设置时使用默认值（`NEVSTOP-LAB` / `csm-core,API String,MassData,INIVariable`）。

### 步骤 3 — 新建 `scripts/router.py`（~280 行）

#### 模块结构

```
router.py
├── 全局常量
│   ├── BOT_MARKER = "<!-- csm-qa-bot -->"  （复用现有标记，Worker 过滤无需改动）
│   ├── JOIN_FOLLOW_ORG = os.getenv("JOIN_FOLLOW_ORG", "NEVSTOP-LAB")
│   ├── JOIN_STAR_REPOS = os.getenv("JOIN_STAR_REPOS", "csm-core,API String,MassData,INIVariable").split(",")
│   │                    （从 GitHub Actions vars 注入，逗号分隔，可随时调整无需改代码）
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
│   ├── _check_following(token, username, org)               REST API 检查是否关注组织
│   ├── _check_star(token, username, owner, repo)           REST API 公开检查
│   ├── check_all_conditions(token, username, org)          汇总所有条件
│   ├── build_condition_report(username, all_met, results)  Markdown 表格报告
│   ├── _resolve_user_id(token, username)                   GET /users/{username} → 提取数字 ID
│   ├── send_invitation(token, org, user_id)                POST /orgs/{org}/invitations
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
- LLM 调用失败 → 降级为正则匹配：
  - 匹配 `/join|加入|申请|apply/` → JOIN
  - 匹配 `/(问|？|\?|怎么|如何|是什么|报错|bug|error|请教|求助)/` → QA
  - 其余 → OTHER（避免 LLM 故障时所有评论触发完整 RAG + LLM 回答链路）

#### 条件检测详情

| 条件 | API | 说明 |
|------|-----|------|
| 关注组织 | `GET /users/{username}/following/{org}` | 204 = 已关注 NEVSTOP-LAB，404 = 未关注 |
| Star 指定仓库 | `GET /users/{user}/starred/{owner}/{repo}` | 204 = 已 Star，404 = 未 Star；需全部 Star |

> **Star 仓库清单**：通过 GitHub Actions 变量 `JOIN_STAR_REPOS` 配置，逗号分隔（默认：`csm-core,API String,MassData,INIVariable`）。
> 均为 `NEVSTOP-LAB` 组织下仓库。变更只需在仓库 Settings → Variables 中修改，无需改代码或重新部署。

> **邀请发送流程**：`send_invitation` 调用前需先通过 `_resolve_user_id(token, username)` 获取用户数字 ID（`GET /users/{username}` → `id` 字段），再以 `invitee_id` 调用 `POST /orgs/{org}/invitations`。GitHub 邀请 API 要求数字 ID 而非用户名。

#### 报告格式

```
## 📋 @{username} 的加入申请审核结果

| 条件 | 状态 | 详情 |
|------|:----:|------|
| 👀 关注 @NEVSTOP-LAB | ✅ | 已关注 |
| ⭐ Star 指定仓库 | ❌ | 缺少：API String、INIVariable |

{通过时} 🎉 全部通过 (2/2)！邀请已发送，请查收 GitHub 邮件并点击 Accept。
{未通过} 🔴 需要全部满足，当前 1/2 项通过。满足后再次发送 /join 重试。

> ⭐ 需 Star 的仓库：csm-core, API String, MassData, INIVariable
```

### 步骤 4 — 测试（新建 `tests/test_router.py`）

- 覆盖以下函数：
  - `classify_intent` 降级正则：JOIN 关键词 → JOIN，QA 关键词 → QA，其他 → OTHER
  - `check_all_conditions`：mock REST 返回，验证关注通过/未通过 + Star 全部/部分通过等组合路径
  - `build_condition_report`：验证 Markdown 表格输出格式（✅/❌ 图标、详情列、Star 仓库清单展示）
  - `_resolve_user_id`：mock REST `GET /users/{username}` 返回，验证 ID 提取
- 测试通过 `pytest tests/test_router.py` 运行
- 在 `org-router.yml` workflow 的 `Route & Process` 步骤前增加 pytest 步骤（默认启用，失败不阻塞主流程）

### 步骤 5 — GitHub App 配置 + Actions 变量

在现有 GitHub App（`CSM-QA-Bot-Webhook-Relay`）设置页：

1. **Organization permissions → Members** → 设为 `Read & Write`
2. 保存后**重新安装 App** 到 NEVSTOP-LAB 组织

在本仓库 **Settings → Secrets and variables → Actions → Variables** 中创建：

| Variable | 默认值 | 说明 |
|----------|--------|------|
| `JOIN_FOLLOW_ORG` | `NEVSTOP-LAB` | 用户需关注的组织 |
| `JOIN_STAR_REPOS` | `csm-core,API String,MassData,INIVariable` | 需 Star 的仓库名，逗号分隔 |

> 变量均为可选：未设置时使用默认值。变更后即时生效，无需改代码或重新部署。

### 步骤 6 — 灰度验证

1. **手动触发测试 JOIN**：Actions 页面 → `Org Discussion Router` → `workflow_dispatch`，输入一条 Q&A 区 Discussion 编号（该 Discussion 最后一条评论为 `/join`），dry-run 查看报告
2. **手动触发测试 QA**：同样方式，输入一条技术问题 Discussion，确认正常回答
3. **边界测试**：用 `"LabVIEW 怎么 join 数组"` 确认分类为 QA 而非 JOIN
4. **LLM 降级测试**：临时改错 `LLM_API_KEY`，用普通评论（如 "hello"）确认降级为 OTHER 引导（而非触发完整 QA 链路）；再用含 `/join` 的评论确认降级后仍正确路由到 JOIN 逻辑
5. **正式上线**：Worker 代码部署后，自动路由生效
6. **回滚方案**：若出问题，Worker 中改回旧 `event_type` + Actions 禁用 `org-router.yml`

## 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| `webhook/cloudflare-worker.js` | 修改 ~8 行 | 统一 `event_type` + 附加 payload |
| `scripts/router.py` | **新建** ~280 行 | LLM 路由 + JOIN/QA/OTHER 逻辑 |
| `.github/workflows/org-router.yml` | **新建** ~70 行 | 统一 workflow 入口 |
| `scripts/discussion_bot.py` | 不碰 | 零风险 |
| `.github/workflows/csm-discussion-bot.yml` | 移除 discussion/discussion_comment 触发器（避免双重触发），保留 repository_dispatch 回滚 + workflow_dispatch 手动扫描 |
| `tests/test_discussion_bot.py` | 不碰 | 零风险 |
| `tests/test_router.py` | **新建** ~120 行 | router.py 单元测试 |

## 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| LLM 分类温度 | `temperature=0` | 确定性输出，同一评论不反复变化 |
| LLM 降级 | 失败降级为正则，默认 OTHER | 避免 LLM 故障时所有评论触发完整 QA 链路 |
| Worker 传入 comment_body | 附带在 payload 中，截断 800 字符 | Router 省一次 GraphQL 查询，800 字符足够 LLM 区分 JOIN/QA |
| BOT_MARKER 统一 | router 复用 `csm-qa-bot` | Worker 已有过滤逻辑无需修改 |
| CSM_QA 初始化 | 延迟到 QA 路径才 import | JOIN/OTHER 路径秒级完成 |
| JOIN 不限分类 | 任意 Discussion 下 `/join` 均生效 | 入口灵活 |
| QA 限制 Q&A 分类 | 非 Q&A 则引导用户过去 | 防止技术回答溢出到其他分类 |
| JOIN 条件配置 | GitHub Actions `vars`，逗号分隔 | 变更仓库列表无需改代码，即时生效 |

## 未来扩展

Router 架构自然支持添加新意图：

```
LLM 分类 → INTENT_REPORT_BUG  → 自动创建 Issue
         → INTENT_FEATURE_REQ → 自动创建 Discussion
         → INTENT_FAQ         → 检索 FAQ 文档回复
         ...
```

只需在分类 Prompt 和路由表中各加一行即可。
