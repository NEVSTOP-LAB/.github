#!/usr/bin/env python3
"""Org Discussion Router — LLM 意图分类 + JOIN / QA / OTHER 分派处理。

由 ``org-router.yml`` workflow 在收到 ``org_msg_router`` repository_dispatch
或手动 workflow_dispatch 时调用。

路由流程
────────
1. 解析 CLI 参数（discussion_number, comment_body, comment_author, category_name）
2. LLM 三分类：JOIN / QA / OTHER（失败时降级正则）
3. JOIN  → 条件检测（关注组织 + Star 仓库）→ 通过则邀请
4. QA    → Q&A 分类下用 CSM_QA 回答，否则引导用户去 Q&A 区
5. OTHER → 友好引导回复
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# ── 确保包根目录在 sys.path ─────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._utils import configure_logging, SKIP_AUTHORS  # noqa: E402
from scripts._github import GitHubGraphQL  # noqa: E402

logger = logging.getLogger("org_router")


# ── 常量 ────────────────────────────────────────────────────────────────────

GITHUB_API_URL = "https://api.github.com"
BOT_MARKER = "<!-- csm-qa-bot -->"
BOT_FOOTER = (
    "\n\n---\n"
    "> 🤖 此回复由 [CSM-QA-Robot](https://github.com/NEVSTOP-LAB/CSM-QA-Robot) 自动处理。"
)

# JOIN 条件：从 GitHub Actions vars 注入，逗号分隔
JOIN_FOLLOW_ORG = os.getenv("JOIN_FOLLOW_ORG", "NEVSTOP-LAB")
JOIN_STAR_REPOS = [
    r.strip() for r in
    os.getenv("JOIN_STAR_REPOS", "Communicable-State-Machine,CSM-API-String-Arguments-Support,CSM-MassData-Parameter-Support,CSM-INI-Static-Variable-Support").split(",")
    if r.strip()
]
JOIN_STAR_OWNER = JOIN_FOLLOW_ORG  # Star 仓库所属组织与关注组织一致
JOIN_DEFAULT_TEAM = os.getenv("JOIN_DEFAULT_TEAM", "csm-community")

# LLM 分类提示词
INTENT_CLASSIFY_PROMPT = """你是一个 GitHub 讨论区路由助手。请判断以下评论属于哪一类意图，只回复一个标签：

标签说明：
- JOIN：用户**本人**表达了加入组织的意愿（例如"我想加入"、"申请加入"、"希望能成为成员"、"想参与贡献"等，无需特定命令）。**注意**：建议/邀请/欢迎**他人**加入（如"你可以加入"、"欢迎加入我们"、"推荐你申请"）不属于 JOIN，应归类为 OTHER；用户以组织成员身份回复他人时也不属于 JOIN。
- QA：用户提出技术问题或框架使用问题（可能包含 join/加入 等技术术语）
- OTHER：其他与上述无关的评论

用户评论：
'''
{comment_body}
'''

只回复标签名（JOIN / QA / OTHER），不要任何解释。"""

# 带 thread 上下文的分类提示词：将整个讨论历史传给 LLM，使其能理解追问的语境
INTENT_CLASSIFY_PROMPT_WITH_CONTEXT = """你是一个 GitHub 讨论区路由助手。以下是该 Discussion 的完整对话历史（按时间顺序，user=用户, assistant=Bot），请根据上下文判断最后一条用户评论的意图，只回复一个标签：

标签说明：
- JOIN：用户**本人**表达了加入组织的意愿（例如"我想加入"、"申请加入"、"希望能成为成员"、"想参与贡献"等，无需特定命令）。**注意**：建议/邀请/欢迎**他人**加入（如"你可以加入"、"欢迎加入我们"、"推荐你申请"）不属于 JOIN，应归类为 OTHER；用户以组织成员身份回复他人时也不属于 JOIN。
- QA：用户提出技术问题或框架使用问题（可能包含 join/加入 等技术术语）
- OTHER：其他与上述无关的评论

对话历史：
---
{history_text}
---

最后一条用户评论（需要你判断意图）：
'''
{comment_body}
'''

请仅根据最后这条评论在整个对话中的上下文来判断其意图。
只回复标签名（JOIN / QA / OTHER），不要任何解释。"""


def _build_history_text(history: list[dict[str, str]]) -> str:
    """将 history 列表格式化为可读的对话文本。

    限制策略：最多保留最近 20 条消息，且总字符数不超过 8000，
    防止超长线程撑爆 LLM 上下文窗口。"""
    # 最多保留最近 N 条消息（原帖首条始终保留）
    MAX_ENTRIES = 20
    if len(history) > MAX_ENTRIES:
        # 保留首条（原帖）+ 最近 MAX_ENTRIES-1 条
        history = history[:1] + history[-(MAX_ENTRIES - 1):]

    MAX_CHARS = 8000
    lines: list[str] = []
    for entry in history:
        role = entry.get("role", "")
        if role == "user":
            role_label = "用户"
        elif role == "assistant":
            role_label = "Bot"
        else:
            role_label = "用户"  # 未知角色按用户处理
        content = entry.get("content", "").strip()
        if not content:
            continue
        # 截断每条消息
        lines.append(f"[{role_label}]: {content[:500]}")

    # 总长度限制
    text = "\n\n".join(lines)
    if len(text) > MAX_CHARS:
        text = text[-MAX_CHARS:]
        # 寻找第一个换行符后的位置作为自然截断点
        first_nl = text.find("\n")
        if first_nl >= 0 and first_nl < len(text) - 1:
            text = text[first_nl + 1:]
        text = "…[上文已省略]…\n\n" + text
    return text

# DeepSeek API（兼容 OpenAI 格式）
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# QA 分类名（大小写精确匹配）
QA_CATEGORY_NAME = "Q&A"

# 降级正则：JOIN 匹配在 _fallback_classify 内按强弱分层
_RE_QA = re.compile(r"问|？|\?|怎么|如何|是什么|报错|bug|error|请教|求助", re.IGNORECASE)


def _repo_link(repo: str) -> str:
    """把仓库名转为 Markdown 链接。"""
    return f"[{repo}](https://github.com/{JOIN_STAR_OWNER}/{repo})"

# App 安装信息（用于发送组织邀请，需要 org Members 写权限）
_GITHUB_APP_ID = os.getenv("GITHUB_APP_ID", "")
_GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
_GITHUB_APP_INSTALL_ID = os.getenv("GITHUB_APP_INSTALL_ID", "")


# ── REST helper ──────────────────────────────────────────────────────────────


# ── App Installation Token ──────────────────────────────────────────────────

def _get_app_installation_token(owner: str, repo: str) -> Optional[str]:
    """获取 GitHub App installation token（含 org Members 权限）。

    邀请 API ``POST /orgs/{org}/invitations`` 需要 org:write 权限，
    ``CSM_QA_GH_TOKEN``（Fine-grained PAT）只有 discussions 权限，
    因此需用 App 私钥签发 JWT 换取 installation token。
    """
    app_id = os.getenv("GITHUB_APP_ID", "")
    key_pem = os.getenv("GITHUB_APP_PRIVATE_KEY", "")
    if not app_id or not key_pem:
        return None

    try:
        import jwt
        now = int(time.time())
        jwt_token = jwt.encode(
            {"iat": now - 60, "exp": now + 10 * 60, "iss": str(app_id)},
            key_pem,
            algorithm="RS256",
        )
    except Exception as exc:
        logger.warning("App JWT 签发失败: %s", exc)
        return None

    try:
        resp = _rest_req(jwt_token, "GET", f"/repos/{owner}/{repo}/installation")
        install = json.loads(resp.read())
        install_id = install.get("id")
        if not install_id:
            raise RuntimeError("未找到 installation id")
    except Exception as exc:
        logger.warning("获取 installation id 失败: %s", exc)
        return None

    try:
        data = json.dumps({}).encode()
        req = urllib.request.Request(
            f"{GITHUB_API_URL}/app/installations/{install_id}/access_tokens",
            data=data,
            headers={
                "Authorization": f"Bearer {jwt_token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "org-router/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        token = result.get("token")
        if token:
            perms = result.get("permissions", {})
            logger.info(
                "App installation token 获取成功 (id=%s, permissions=%s)",
                install_id, json.dumps(perms),
            )
        else:
            logger.warning("installation token 响应为空: %s", json.dumps(result)[:300])
        return token
    except Exception as exc:
        logger.warning("换取 installation token 失败: %s", exc)
        return None


# ── REST helper ──────────────────────────────────────────────────────────────


def _rest_req(token: str, method: str, path: str) -> Any:
    """发送 GitHub REST API 请求，返回 HTTP response 对象。

    Raises:
        RuntimeError: 网络错误或超时。
        urllib.error.HTTPError: HTTP 4xx/5xx。
    """
    quoted = urllib.parse.quote(path, safe="/?:&=#")
    url = f"{GITHUB_API_URL}{quoted}"
    req = urllib.request.Request(
        url,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "org-router/1.0",
        },
    )
    try:
        return urllib.request.urlopen(req, timeout=15)
    except urllib.error.HTTPError:
        raise  # 4xx/5xx 原样抛出，由 _check_* 解析 404 语义
    except urllib.error.URLError as exc:
        raise RuntimeError(f"REST 请求失败 {method} {path}: {exc}") from exc


# ── LLM 意图分类 ────────────────────────────────────────────────────────────


def classify_intent(comment_body: str, history: Optional[list[dict[str, str]]] = None) -> str:
    """对评论正文做 LLM 三分类，返回 ``"JOIN"`` / ``"QA"`` / ``"OTHER"``。

    当提供 ``history``（整个 discussion 的对话历史）时，使用带上下文的 prompt，
    帮助 LLM 更准确地理解追问类消息的意图。

    失败时降级为正则匹配（强弱分层）：
    - 强关键词（加入/申请加入/成为成员/想加入/参与贡献）→ JOIN
    - 弱关键词 join + QA 模式共存（如 "怎么 join 数组"）→ QA
    - 仅弱关键词 join → JOIN
    - QA 关键词 → QA
    - 其余 → OTHER
    """
    text = comment_body.strip()
    if not text:
        return "OTHER"

    if not LLM_API_KEY:
        logger.warning("LLM_API_KEY 未配置，使用正则降级分类")
        return _fallback_classify(text)

    # 构建 LLM 消息：有历史上下文时使用带上下文的 prompt
    # 先转义用户内容中的花括号，避免 str.format() KeyError
    safe_text = text[:800].replace("{", "{{").replace("}", "}}")

    try:
        if history and len(history) > 0:
            history_text = _build_history_text(history).replace("{", "{{").replace("}", "}}")
            prompt = INTENT_CLASSIFY_PROMPT_WITH_CONTEXT.format(
                history_text=history_text,
                comment_body=safe_text,
            )
        else:
            prompt = INTENT_CLASSIFY_PROMPT.format(comment_body=safe_text)

        payload = json.dumps({
            "model": LLM_MODEL,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": 0,
            "max_tokens": 10,
        }).encode()

        req = urllib.request.Request(
            f"{LLM_API_BASE}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())

        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        label = content.strip().upper()
        for tag in ("JOIN", "QA", "OTHER"):
            if tag in label:
                logger.info("LLM 分类结果: %s", tag)
                return tag

        logger.warning("LLM 返回无法解析: %r，降级正则分类", label)
    except Exception as exc:
        logger.warning("LLM 调用失败: %s，降级正则分类", exc)

    return _fallback_classify(text)


def _fallback_classify(text: str) -> str:
    # 第二人称建议加入模式（建议/邀请他人加入，不是评论者自己要加入）
    _RE_SUGGEST_JOIN = re.compile(
        r"(你可以|建议你|推荐你|欢迎你|邀请你|你试试|你去|你能够)"
        r".{0,15}?"
        r"(加入|申请|参与|成为成员)",
        re.IGNORECASE,
    )
    if _RE_SUGGEST_JOIN.search(text):
        logger.info("正则降级分类: OTHER（第二人称建议加入模式）")
        return "OTHER"

    # 强 JOIN 模式（中文明确表达加入意图）
    _RE_JOIN_STRONG = re.compile(r"加入|申请加入|成为成员|想加入|申请成为|参与贡献", re.IGNORECASE)
    has_strong_join = _RE_JOIN_STRONG.search(text)
    has_qa = _RE_QA.search(text)
    has_weak_join = re.search(r"\bjoin\b", text, re.IGNORECASE) if not has_strong_join else None

    if has_strong_join:
        logger.info("正则降级分类: JOIN（强关键词）")
        return "JOIN"

    if has_qa and has_weak_join:
        # "LabVIEW 怎么 join 数组" → QA 优先于弱 join
        logger.info("正则降级分类: QA（弱 join + QA 模式共存）")
        return "QA"

    if has_weak_join:
        logger.info("正则降级分类: JOIN（弱 join）")
        return "JOIN"

    if has_qa:
        logger.info("正则降级分类: QA")
        return "QA"

    logger.info("正则降级分类: OTHER")
    return "OTHER"


# ── JOIN 逻辑 ────────────────────────────────────────────────────────────────


def _check_following(token: str, username: str, org: str) -> tuple[bool, str]:
    """检查用户是否已关注组织。返回 ``(passed, detail)``。"""
    try:
        resp = _rest_req(token, "GET", f"/users/{username}/following/{org}")
        # 请求成功（未抛 HTTPError）→ 已关注
        return True, f"已关注 @{org}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, f"未关注 @{org}"
        raise RuntimeError(f"检查关注失败: HTTP {exc.code}") from exc


def _is_org_member(token: str, org: str, username: str) -> bool:
    """通过 REST API 检查用户是否已在组织内。

    App installation token 需要 org Members:Read 权限。
    204 = 是成员，404 = 非成员，其他状态码 = 查询失败返回 False。
    """
    try:
        _rest_req(token, "GET", f"/orgs/{org}/members/{username}")
        logger.info("_is_org_member: %s 已在 %s 组织内 (204)", username, org)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info("_is_org_member: %s 不在 %s 组织内 (404)", username, org)
            return False
        logger.warning(
            "_is_org_member 查询失败: %s org=%s HTTP %d body=%s",
            username, org, exc.code,
            exc.read().decode("utf-8", errors="replace")[:200],
        )
        return False
    except Exception as exc:
        logger.warning("_is_org_member 网络异常: %s %s %s", username, org, exc)
        return False


def _get_starred_repos(token: str, username: str) -> set[str]:
    """获取用户所有 Star 仓库的 full_name 集合（仅第一页 100 条）。

    ``GET /users/{username}/starred/{owner}/{repo}`` 端点在 PAT 认证下
    不可靠（始终返回 404），改用列表+过滤方式。
    """
    starred: set[str] = set()
    page = 1
    while page <= 5:  # 最多 500 条，够用
        try:
            resp = _rest_req(
                token, "GET",
                f"/users/{username}/starred?per_page=100&page={page}",
            )
            data = json.loads(resp.read())
            if not data:
                break
            for item in data:
                starred.add(item.get("full_name", ""))
            page += 1
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                f"获取 Star 列表失败 {username}: HTTP {exc.code}"
            ) from exc
    return starred


def check_all_conditions(token: str, username: str) -> tuple[bool, list[dict[str, Any]]]:
    """汇总所有加入条件检测结果。

    Returns:
        ``(all_met, results)`` 其中 ``results`` 为条件列表，每项含
        ``name`` / ``icon`` / ``passed`` / ``detail``。
    """
    results: list[dict[str, Any]] = []

    # 条件 1：关注组织
    ok_follow, detail_follow = _check_following(token, username, JOIN_FOLLOW_ORG)
    results.append({
        "name": f"关注 @{JOIN_FOLLOW_ORG}",
        "icon": "👀",
        "passed": ok_follow,
        "detail": detail_follow,
    })

    # 条件 2：Star 全部指定仓库 — 先拉取全部 Star 列表，再逐个比对
    try:
        starred = _get_starred_repos(token, username)
    except RuntimeError as exc:
        logger.error("获取 Star 列表失败: %s", exc)
        starred = set()

    star_details: list[str] = []
    for repo in JOIN_STAR_REPOS:
        full = f"{JOIN_STAR_OWNER}/{repo}"
        if full not in starred:
            star_details.append(repo)

    star_passed = len(star_details) == 0
    if star_passed:
        star_detail = "已 Star 全部"
    else:
        star_detail = "缺少：" + ", ".join(_repo_link(r) for r in star_details)
    results.append({
        "name": "Star 指定仓库",
        "icon": "⭐",
        "passed": star_passed,
        "detail": star_detail,
    })

    all_met = all(r["passed"] for r in results)
    return all_met, results


def build_condition_report(
    username: str,
    all_met: bool,
    results: list[dict[str, Any]],
) -> str:
    """生成条件检测 Markdown 报告。"""
    passed_count = sum(1 for r in results if r["passed"])
    total = len(results)

    lines = [
        f"## 📋 @{username} 的加入申请审核结果",
        "",
        "| 条件 | 状态 | 详情 |",
        "|------|:----:|------|",
    ]
    for r in results:
        icon = r["icon"]
        emoji = "✅" if r["passed"] else "❌"
        lines.append(f"| {icon} {r['name']} | {emoji} | {r['detail']} |")

    lines.append("")
    if all_met:
        lines.append(
            f"🎉 全部通过 ({passed_count}/{total})！正在发送邀请…"
        )
    else:
        lines.append(
            f"🔴 需要全部满足，当前 {passed_count}/{total} 项通过。"
            f"满足后请再次发送申请，无需特殊格式，说明来意即可。"
        )

    lines.append("")
    lines.append(
        f"> ⭐ 需 Star 的仓库：{', '.join(_repo_link(r) for r in JOIN_STAR_REPOS)}"
    )

    if all_met:
        lines.append("")
        lines.append(
            "\n---\n"
            "> ### 🏷️ 团队分组说明\n"
            "> 加入组织后默认邀请至 **CSM-Community**（CSM 社区爱好者）团队。\n"
            "> \n"
            "> | 团队 | 说明 |\n"
            "> |------|------|\n"
            "> | **CSM-Community** | CSM 社区爱好者（默认加入） |\n"
            "> | ├─ **CSM-Module-Author** | CSM 模块的贡献者 |\n"
            "> |    └─ **CSM-Developer** | CSM 开发人员 |\n"
            "> \n"
            "> 团队分组权限不会主动提升，@NEVSTOP-LAB/csm-committee 会根据贡献"
            "提高对应的项目权限。\n"
            "> \n"
            "> ---\n"
            "> ### 💡 温馨提示\n"
            "> \n"
            "> - 💬 Q&A 中提问 CSM 相关问题，机器人会自动回复；你也可以创建仓库上传"
            "你的 CSM 模块，在 Issue 中 @nevstop 或 @NEVSTOP-LAB/csm-committee，"
            "我们会帮你 Review 并提出改进建议\n"
            "> - 📋 [项目任务看板](https://github.com/orgs/NEVSTOP-LAB/projects/18) "
            "中是主要需要完成的任务列表，欢迎认领并完成对应的 Issue\n"
            "> - ⚠️ 成员需每月有公开贡献（commit / Issue / PR），"
            "长期无贡献将被自动移出组织。请保持活跃，为社区做出贡献！"
        )

    return "\n".join(lines)


def _resolve_user_id(token: str, username: str) -> int:
    """通过 REST API 获取用户数字 ID（邀请 API 需要）。"""
    try:
        resp = _rest_req(token, "GET", f"/users/{username}")
        data = json.loads(resp.read())
        user_id = data.get("id")
        if not user_id:
            raise RuntimeError(f"GET /users/{username} 未返回 id 字段")
        return int(user_id)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"获取用户 {username} 信息失败: HTTP {exc.code}"
        ) from exc


def send_invitation(token: str, org: str, user_id: int) -> bool:
    """发送组织邀请。返回 True 表示成功。"""
    try:
        # 标记 token 类型方便日志排查
        token_type = "App" if token != os.environ.get("CSM_QA_GH_TOKEN", "") else "PAT"
        data = json.dumps({"invitee_id": user_id}).encode()
        req = urllib.request.Request(
            f"{GITHUB_API_URL}/orgs/{org}/invitations",
            data=data,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
                "User-Agent": "org-router/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        logger.info(
            "%s token 邀请成功: org=%s user_id=%d status=%d",
            token_type, org, user_id, resp.status,
        )
        return resp.status in (201, 200)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "%s token 邀请失败: org=%s user_id=%d HTTP %d body=%s",
            token_type, org, user_id, exc.code, body[:400],
        )
        return False


def _add_team_membership(token: str, org: str, team_slug: str, username: str) -> bool:
    """将用户添加到指定团队。需要 org Members:Write 权限。

    ``PUT /orgs/{org}/teams/{team_slug}/memberships/{username}``
    成功返回 True，失败返回 False。
    """
    try:
        team_url = f"/orgs/{org}/teams/{team_slug}/memberships/{username}"
        _rest_req(token, "PUT", team_url)
        logger.info(
            "用户已添加到团队: %s → %s/%s",
            username, org, team_slug,
        )
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            # 用户已在团队中 → 视为成功
            logger.info("用户已在团队中: %s/%s", username, team_slug)
            return True
        body = exc.read().decode("utf-8", errors="replace")
        logger.error(
            "添加团队失败: %s → %s/%s HTTP %d body=%s",
            username, org, team_slug, exc.code, body[:300],
        )
        return False
    except Exception as exc:
        logger.error("添加团队网络异常: %s/%s %s", username, team_slug, exc)
        return False


def post_reply(token: str, discussion_id: str, body: str) -> str:
    """向 Discussion 发布评论，返回新评论的 node ID。"""
    gql_client = GitHubGraphQL(token, user_agent="org-router/1.0")
    full_body = f"{body}{BOT_FOOTER}\n{BOT_MARKER}"
    gql = """
    mutation($discussionId: ID!, $body: String!) {
      addDiscussionComment(input: {discussionId: $discussionId, body: $body}) {
        comment {
          id
          url
        }
      }
    }
    """
    data = gql_client.query(gql, {"discussionId": discussion_id, "body": full_body})
    comment = data.get("addDiscussionComment", {}).get("comment", {})
    comment_url = comment.get("url", "")
    logger.info("评论已发布: %s", comment_url)
    return comment.get("id", "")


# ── QA 逻辑 ──────────────────────────────────────────────────────────────────


def _handle_qa(
    token: str,
    discussion_number: int,
    category_name: str,
    dry_run: bool,
    comment_body: str = "",
    comment_author: str = "",
) -> None:
    """处理 QA 意图：Q&A 分类下调用 CSM_QA 回答，否则引导。

    当 CSM_QA 初始化失败或生成回答出错时，发布错误提示回复而非崩溃，
    确保 workflow 不会因临时故障（网络、模型下载等）整体失败。

    模拟模式（discussion_number=0）时跳过 Discussion API 调用：
    直接使用 --comment-body 作为问题，调用 CSM_QA 生成回答并打印到 stdout。
    """
    source_owner, source_repo = _get_source_repo_parts()

    # ── 模拟模式（discussion_number=0）───────────────────────────────────
    if discussion_number == 0:
        simulate_question = comment_body.strip()
        if not simulate_question:
            logger.error("[SIMULATE] 模拟模式需要提供 --comment-body（提问正文）")
            return
        logger.info("[SIMULATE] QA 模拟模式：question=%s", simulate_question[:100])

        # 延迟导入 CSM_QA
        try:
            from csm_llm_qa import CSM_QA  # noqa: F811
        except Exception:
            logger.exception("[SIMULATE] 导入 CSM_QA 失败")
            return

        try:
            qa_engine = CSM_QA.from_env(temperature=0)
        except Exception:
            logger.exception("[SIMULATE] CSM_QA.from_env() 初始化失败")
            return

        try:
            answer = qa_engine.ask(simulate_question)
            # 模拟模式下将回答打印到 stdout，供 workflow 日志查看
            print("=" * 60)
            print("  SIMULATE QA Answer")
            print("=" * 60)
            print(f"Question: {simulate_question}")
            print("-" * 40)
            print(answer)
            print("=" * 60)
            logger.info("[SIMULATE] QA 回答已生成（%d chars）", len(answer))
        except Exception:
            logger.exception("[SIMULATE] 生成 QA 回答失败")
        return

    if category_name != QA_CATEGORY_NAME:
        # 非 Q&A 分类 → 引导到 Q&A 区
        try:
            gql_client = GitHubGraphQL(token, user_agent="org-router/1.0")
            discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)
            disc_id = discussion.get("id", "")
            qa_url = f"https://github.com/orgs/{source_owner}/discussions/categories/q-a"
            guide_body = (
                f"💡 技术问题请在 [Q&A 分类]({qa_url}) 下提出，"
                f"那里的 Bot 会自动为你解答。感谢理解！"
            )
            if not dry_run:
                post_reply(token, disc_id, guide_body)
            else:
                logger.info("[DRY-RUN] 将引导至 Q&A 分类: discussion_id=%s", disc_id)
        except Exception:
            logger.exception("非 Q&A 引导回复失败")
        return

    # ── Q&A 分类 → 延迟导入并初始化 CSM_QA ──────────────────────────────
    logger.info("Q&A 分类下的 QA 请求，初始化 CSM_QA…")

    # 先尝试获取 discussion 元信息（失败时可发布错误回复）
    try:
        gql_client = GitHubGraphQL(token, user_agent="org-router/1.0")
        discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)
        disc_id = discussion.get("id", "")
    except Exception:
        logger.exception("获取 discussion #%d 失败", discussion_number)
        return

    # 延迟导入 CSM_QA 和 discussion_bot 函数（JOIN/OTHER 路径不触发）
    try:
        from scripts.discussion_bot import (  # type: ignore[import-not-found]
            compute_reply_plan,
            build_reply,
            post_comment,
            fetch_discussion as fetch_disc,
        )
        from csm_llm_qa import CSM_QA
    except Exception:
        logger.exception("导入 CSM_QA / discussion_bot 模块失败")
        if not dry_run and disc_id:
            try:
                post_reply(
                    token, disc_id,
                    "⚠️ QA Bot 初始化失败（依赖模块导入错误），请稍后重试。"
                    "若问题持续，请联系管理员。",
                )
            except Exception:
                logger.exception("发布错误提示失败")
        return

    client = GitHubGraphQL(token)

    # 初始化 RAG 问答引擎（可能下载模型/构建向量库，耗时较长）
    try:
        qa_engine = CSM_QA.from_env(temperature=0, max_tokens=2048)
    except Exception:
        logger.exception("CSM_QA.from_env() 初始化失败")
        if not dry_run and disc_id:
            try:
                post_reply(
                    token, disc_id,
                    "⚠️ QA Bot 初始化失败（模型加载或向量库构建出错），请稍后重试。"
                    "若问题持续，请联系管理员。",
                )
            except Exception:
                logger.exception("发布错误提示失败")
        return

    # 获取 Bot 自身的登录名（用于 compute_reply_plan 作者校验）
    try:
        viewer_data = client.query("query { viewer { login } }")
        bot_login = viewer_data.get("viewer", {}).get("login")
    except Exception:
        bot_login = None

    # 检查 discussion 作者是否在跳过列表中（GitHub login 大小写不敏感）
    author_login = (discussion.get("author") or {}).get("login", "").casefold()
    if author_login in SKIP_AUTHORS:
        logger.info(
            "Discussion #%d 作者 %r 在跳过列表中，跳过", discussion_number, author_login
        )
        return

    plan = compute_reply_plan(discussion, bot_login)
    if plan is None:
        logger.info("无需回复（已回复且无追问）")
        return

    question, history = plan
    logger.info("生成回答中 (question=%s chars, history=%d turns)", len(question), len(history))

    if not dry_run:
        try:
            answer = qa_engine.ask(question, history=history)
            reply_body = build_reply(answer)  # build_reply 已含 footer + marker
            post_comment(client, disc_id, reply_body)
        except Exception:
            logger.exception("生成 QA 回答失败")
            try:
                post_reply(
                    token, disc_id,
                    "⚠️ 生成回答时出错，请稍后重试。若问题持续，请联系管理员。",
                )
            except Exception:
                logger.exception("发布错误提示失败")
    else:
        logger.info("[DRY-RUN] 将生成 QA 回答: question=%.100s", question)


# ── 辅助函数 ─────────────────────────────────────────────────────────────────


def _get_repo_parts() -> tuple[str, str]:
    """从 GITHUB_REPOSITORY 解析 owner/repo。"""
    repo_env = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo_env:
        raise ValueError(f"GITHUB_REPOSITORY 格式不正确: {repo_env!r}，期望 'owner/repo'")
    owner, repo = repo_env.split("/", 1)
    return owner, repo


def _get_source_repo_parts() -> tuple[str, str]:
    """获取组织 Discussion 实际归属的源仓库 owner/repo。

    默认 ``<org>/.github``，其中 ``<org>`` 取自 ``GITHUB_REPOSITORY``。
    可通过环境变量 ``DISCUSSION_SOURCE_REPO`` 覆盖（格式 ``owner/repo``）。
    """
    source_env = (os.environ.get("DISCUSSION_SOURCE_REPO") or "").strip()
    if source_env:
        if "/" not in source_env:
            logger.warning(
                "DISCUSSION_SOURCE_REPO=%r 格式不正确（期望 owner/repo），回退默认",
                source_env,
            )
        else:
            parts = source_env.split("/", 1)
            if parts[0] and parts[1]:
                return parts[0], parts[1]
            logger.warning(
                "DISCUSSION_SOURCE_REPO=%r 含空 owner 或 repo，回退默认",
                source_env,
            )
    src_owner, _ = _get_repo_parts()
    src_repo = ".github"
    logger.info("使用默认源仓库: %s/%s", src_owner, src_repo)
    return src_owner, src_repo


def _build_classify_history(
    token: str, discussion_number: int, classify_input: str
) -> Optional[list[dict[str, str]]]:
    """为分类构建 discussion thread 历史上下文。

    拉取 discussion 数据后，将标题+正文作为首条 user 消息，后续评论按
    user/assistant 角色组装为对话历史。Bot 评论通过 BOT_MARKER 识别。

    返回的 history 不包含当前要分类的评论本身（即 classify_input），
    格式与 compute_reply_plan 一致：``[{"role": "user"|"assistant", "content": str}]``。
    """
    source_owner, source_repo = _get_source_repo_parts()
    gql_client = GitHubGraphQL(token, user_agent="org-router/1.0")
    discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)

    title = (discussion.get("title") or "").strip()
    body = (discussion.get("body") or "").strip()
    original_question = f"{title}\n\n{body}".strip() if body else title

    comments = discussion.get("comments", {}).get("nodes", []) or []

    # 构建对话历史
    history: list[dict[str, str]] = []
    if original_question:
        history.append({"role": "user", "content": original_question})

    # 当前要分类的消息文本（strip 后用于比较）
    classify_stripped = classify_input.strip()

    for c in comments:
        c_body = (c.get("body") or "").strip()
        if not c_body:
            continue
        # 跳过当前要分类的评论本身（避免重复喂给 LLM）
        # 对于 discussion_comment 事件，新评论总是最后一条；同时用
        # strip 后的文本比较，容忍空白差异
        if c_body == classify_stripped:
            continue
        is_bot = BOT_MARKER in c_body
        role = "assistant" if is_bot else "user"
        history.append({"role": role, "content": c_body})

    return history if history else None


def fetch_discussion(
    client: Any, owner: str, repo: str, number: int
) -> dict[str, Any]:
    """拉取指定 discussion 的详情（含所有评论，自动分页）。"""
    gql = """
    query($owner: String!, $repo: String!, $number: Int!) {
      repository(owner: $owner, name: $repo) {
        discussion(number: $number) {
          id
          number
          title
          body
          url
          closed
          author { login }
          category {
            id
            name
          }
          comments(first: 100) {
            nodes {
              id
              body
              createdAt
              author { login }
            }
            pageInfo {
              hasNextPage
              endCursor
            }
          }
        }
      }
    }
    """
    data = client.query(gql, {"owner": owner, "repo": repo, "number": number})
    repository = data.get("repository") or {}
    disc = repository.get("discussion")
    if not disc:
        raise RuntimeError(f"Discussion #{number} 不存在或无权限访问")

    # 分页拉取剩余评论
    while disc["comments"]["pageInfo"]["hasNextPage"]:
        cursor = disc["comments"]["pageInfo"]["endCursor"]
        more_gql = """
        query($discussionId: ID!, $cursor: String!) {
          node(id: $discussionId) {
            ... on Discussion {
              comments(first: 100, after: $cursor) {
                nodes {
                  id
                  body
                  createdAt
                  author { login }
                }
                pageInfo {
                  hasNextPage
                  endCursor
                }
              }
            }
          }
        }
        """
        more_data = client.query(more_gql, {"discussionId": disc["id"], "cursor": cursor})
        node = more_data.get("node") or {}
        more = node.get("comments", {})
        disc["comments"]["nodes"].extend(more.get("nodes", []))
        disc["comments"]["pageInfo"] = more.get(
            "pageInfo", {"hasNextPage": False, "endCursor": None}
        )

    return disc


def _handle_join(
    token: str,
    discussion_number: int,
    comment_author: str,
    dry_run: bool,
) -> None:
    """处理 JOIN 意图：条件检测 → 报告 → 通过则邀请。

    模拟模式（discussion_number=0）时跳过所有 API 调用，仅打印检测报告。
    """
    source_owner, source_repo = _get_source_repo_parts()

    # ── 模拟模式（discussion_number=0）：仅打印报告，不调用 API ────────
    if discussion_number == 0:
        if not comment_author:
            logger.info("[SIMULATE] JOIN 模拟需要 comment_author 才能做条件检测，"
                        "当前未提供，仅展示流程说明")
            logger.info(
                "[SIMULATE] JOIN 条件检测流程：\n"
                "  1. 检查是否关注 @%s\n"
                "  2. 检查是否 Star 以下仓库：%s\n"
                "  3. 全部通过 → 发送组织邀请（%s）→ 添加团队（%s）\n"
                "  4. 未通过 → 发布条件未满足报告",
                JOIN_FOLLOW_ORG, ", ".join(JOIN_STAR_REPOS),
                JOIN_FOLLOW_ORG, JOIN_DEFAULT_TEAM,
            )
            return
        logger.info("[SIMULATE] JOIN 条件检测: username=%s org=%s star_repos=%s (跳过实际 API 调用)",
                    comment_author, JOIN_FOLLOW_ORG, JOIN_STAR_REPOS)
        # 生成模拟报告（假设条件通过）
        results = [
            {"name": f"关注 @{JOIN_FOLLOW_ORG}", "icon": "👀", "passed": True, "detail": "已关注（模拟）"},
            {"name": "Star 指定仓库", "icon": "⭐", "passed": True, "detail": "已全部 Star（模拟）"},
        ]
        report = build_condition_report(comment_author, True, results)
        report += (
            "\n\n> ⚠️ 模拟模式：以上为模拟检测结果，未实际调用 API。"
            "真实检测请提供有效的 discussion_number。"
        )
        logger.info("[SIMULATE] JOIN 报告:\n%s", report)
        return

    # ── 真实模式 ─────────────────────────────────────────────────────────
    if not comment_author:
        logger.warning("未提供 comment_author，无法执行 JOIN 检测")
        return

    try:
        gql_client = GitHubGraphQL(token, user_agent="org-router/1.0")
    except Exception:
        logger.exception("GitHubGraphQL 初始化失败（token 无效或未配置）")
        return

    # 0. 获取 App installation token（用于 org membership 检查和邀请发送，
    #    CSM_QA_GH_TOKEN 是 fine-grained PAT，无 org 相关权限）
    app_token = _get_app_installation_token(source_owner, source_repo)
    effective_token = app_token if app_token else token
    if not app_token:
        logger.info(
            "未能获取 App token（GITHUB_APP_ID/PRIVATE_KEY 未配置或 JWT 失败），"
            "成员检查和邀请发送将使用 PAT（可能无权限）"
        )

    # 1. 先检查是否已在组织内（使用 App token，PAT 无 org 权限）
    try:
        if _is_org_member(effective_token, JOIN_FOLLOW_ORG, comment_author):
            discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)
            disc_id = discussion.get("id", "")
            body = (
                f"👋 @{comment_author}，你已经是 **{JOIN_FOLLOW_ORG}** 组织的成员了，无需重复申请。\n\n"
                "如有疑问，欢迎在 Q&A 分类下提出。"
            )
            if not dry_run:
                post_reply(token, disc_id, body)
            else:
                logger.info("[DRY-RUN] 用户已是成员: %s", comment_author)
            return
    except Exception:
        logger.exception("检查组织成员状态失败")
        return

    logger.info(
        "JOIN 检测: username=%s org=%s star_repos=%s",
        comment_author, JOIN_FOLLOW_ORG, JOIN_STAR_REPOS,
    )

    # 条件检测
    try:
        all_met, results = check_all_conditions(token, comment_author)
    except Exception:
        logger.exception("JOIN 条件检测失败")
        return

    # 拉取 discussion 获取 node ID（复用已创建的 gql_client）
    try:
        discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)
        disc_id = discussion.get("id", "")
    except Exception:
        logger.exception("拉取 discussion #%d 失败", discussion_number)
        return

    # 生成报告
    report = build_condition_report(comment_author, all_met, results)

    if all_met:
        # 通过 → 发送邀请（复用已获取的 App token，有 org Members 权限）
        try:
            user_id = _resolve_user_id(token, comment_author)
            ok = send_invitation(effective_token, JOIN_FOLLOW_ORG, user_id)
            if ok:
                report += (
                    "\n\n✅ 邀请已成功发送！请查收 GitHub 注册邮箱，"
                    "点击邮件中的 Accept invitation 即可加入组织。"
                )
                # 邀请成功后，尝试添加到默认团队
                team_ok = _add_team_membership(
                    effective_token, JOIN_FOLLOW_ORG, JOIN_DEFAULT_TEAM, comment_author,
                )
                if team_ok:
                    report += (
                        f"\n\n✅ 已邀请加入 **CSM-Community** 团队。"
                        f"更多权限说明见上方团队分组信息。"
                    )
                else:
                    report += (
                        "\n\n⚠️ 团队邀请暂未自动发送（需 App Members 权限），请管理员手动处理。"
                    )
            else:
                report += "\n\n⚠️ 邀请发送失败，请联系管理员。"
        except Exception as exc:
            logger.exception("邀请流程失败")
            report += f"\n\n⚠️ 邀请发送失败（{exc}），请联系管理员。"

    if not dry_run:
        post_reply(token, disc_id, report)
    else:
        logger.info("[DRY-RUN] 将发布 JOIN 报告:\n%s", report)


def _handle_other(
    token: str,
    discussion_number: int,
    dry_run: bool,
    comment_author: str = "",
) -> None:
    """处理 OTHER 意图：友好引导回复。"""
    source_owner, source_repo = _get_source_repo_parts()

    body = (
        "👋 你好！我暂时无法识别你的意图。\n\n"
        "你可以：\n"
        "- 说明你想加入组织（无需特定格式，表达意愿即可）\n"
        "- 在 [Q&A 分类](https://github.com/orgs/{org}/discussions/categories/q-a) "
        "下提出技术问题\n"
        "- 直接描述你的需求，我会尝试引导你\n\n"
        "感谢使用！"
    ).format(org=source_owner)

    # 模拟模式（discussion_number=0）：仅打印，不调用 Discussion API
    if discussion_number == 0:
        logger.info("[SIMULATE] OTHER 引导回复:\n%s", body)
        return

    try:
        gql_client = GitHubGraphQL(token, user_agent="org-router/1.0")
        discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)
        disc_id = discussion.get("id", "")
        if not dry_run:
            post_reply(token, disc_id, body)
        else:
            logger.info("[DRY-RUN] 将发布 OTHER 引导: discussion_id=%s", disc_id)
    except Exception:
        logger.exception("OTHER 引导回复失败")


# ── CLI ──────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Org Discussion Router")
    parser.add_argument(
        "--discussion-number",
        type=int,
        default=0,
        help="Discussion 编号（0 = 模拟模式，不触发 Discussion API 调用）",
    )
    parser.add_argument(
        "--comment-body",
        type=str,
        default="",
        help="评论正文（Webhook 传入，最大 800 字符）",
    )
    parser.add_argument(
        "--discussion-title",
        type=str,
        default="",
        help="Discussion 标题（discussion 事件时用于拼接分类输入）",
    )
    parser.add_argument(
        "--comment-author",
        type=str,
        default="",
        help="评论作者用户名",
    )
    parser.add_argument(
        "--category-name",
        type=str,
        default="",
        help="Discussion 所属分类名",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="只打印不发布评论",
    )
    parser.add_argument(
        "--event-type",
        type=str,
        default="",
        help="GitHub 事件类型（discussion / discussion_comment）",
    )
    parser.add_argument(
        "--classify-only",
        action="store_true",
        default=False,
        help="仅分类并输出意图，不执行任何操作",
    )
    parser.add_argument(
        "--intent",
        type=str,
        default="",
        choices=["JOIN", "QA", "OTHER"],
        help="跳过 LLM 分类，直接使用指定意图处理",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    configure_logging()
    args = parse_args(argv)

    # ── 模拟模式检测 ─────────────────────────────────────────────────────
    is_simulate = args.discussion_number == 0
    if is_simulate:
        logger.info("模拟模式：discussion_number=0，将跳过所有 Discussion API 调用")
        if not args.comment_body.strip():
            logger.error("模拟模式需要提供 --comment-body（提问正文）")
            return 1

    logger.info(
        "Router 启动: discussion=%d author=%s category=%s dry_run=%s classify_only=%s intent=%s simulate=%s",
        args.discussion_number,
        args.comment_author,
        args.category_name,
        args.dry_run,
        args.classify_only,
        args.intent,
        is_simulate,
    )

    # ── 解析意图 ─────────────────────────────────────────────────────────

    # 1. 构造分类输入：discussion 事件将标题拼入正文（标题承载主要意图）
    classify_input = args.comment_body
    if args.event_type == "discussion" and args.discussion_title.strip():
        classify_input = f"{args.discussion_title.strip()}\n\n{args.comment_body}".strip()

    # 1b. 尝试获取 discussion thread 上下文，用于 LLM 分类时提供完整对话历史
    #     模拟模式下跳过（无真实 Discussion 可拉取）
    thread_history: Optional[list[dict[str, str]]] = None
    if not args.intent and not is_simulate:
        # 只有需要 LLM 分类且非模拟模式时才构建上下文
        token_early = os.environ.get("CSM_QA_GH_TOKEN", "")
        if token_early:
            try:
                thread_history = _build_classify_history(
                    token_early, args.discussion_number, classify_input
                )
                if thread_history:
                    logger.info(
                        "获取到 thread 上下文: %d 轮对话",
                        len(thread_history),
                    )
            except Exception:
                logger.warning(
                    "获取 thread 上下文失败，将使用无上下文分类",
                    exc_info=True,
                )

    # 2. 获取意图（--intent 跳过 LLM）
    if args.intent:
        intent = args.intent
        logger.info("意图（手动指定）: %s", intent)
    else:
        intent = classify_intent(classify_input, history=thread_history)
        logger.info("意图分类: %s", intent)

        # 2b. Q&A 分类的 discussion.created：正文短 → 直接按 QA 处理
        if (
            args.event_type == "discussion"
            and intent == "OTHER"
            and args.category_name == QA_CATEGORY_NAME
            and len(args.comment_body.strip()) <= 20
        ):
            logger.info("短正文 + Q&A 分类 + discussion.created → 按 QA 处理")
            intent = "QA"

        # 2c. 空评论的特殊处理（模拟模式下跳过，由 2a 已检查）
        if not is_simulate and not classify_input.strip() and args.event_type == "discussion":
            if args.category_name == QA_CATEGORY_NAME:
                logger.info("空内容 + Q&A 分类 + discussion.created → 按 QA 处理")
                intent = "QA"
            else:
                logger.info("空内容 + 非 Q&A + discussion.created → 跳过（不回复）")
                return 0

    # 3. --classify-only：仅输出意图供 workflow 捕获（token 仅用于获取讨论上下文，非必需）
    if args.classify_only:
        print(intent)
        return 0

    # ── 后续操作需要 token ────────────────────────────────────────────
    #    模拟模式下 token 仅用于 LLM QA 的 GitHub API（wiki 克隆等），
    #    不需要 Discussions 写权限；非模拟模式 token 为必需。

    token = os.environ.get("CSM_QA_GH_TOKEN", "")
    if not is_simulate and not token:
        logger.error("CSM_QA_GH_TOKEN 未配置")
        return 1

    # ── 跳过名单检查：评论作者在 SKIP_AUTHORS 中则直接返回 ───────────
    if args.comment_author:
        if args.comment_author.casefold() in SKIP_AUTHORS:
            logger.info(
                "评论作者 %r 在跳过列表中，跳过处理", args.comment_author
            )
            return 0

    # ── 按意图分派 ───────────────────────────────────────────────────────
    if intent == "JOIN":
        _handle_join(token, args.discussion_number, args.comment_author, args.dry_run or is_simulate)
    elif intent == "QA":
        _handle_qa(token, args.discussion_number, args.category_name, args.dry_run or is_simulate, args.comment_body, args.comment_author)
    else:
        _handle_other(token, args.discussion_number, args.dry_run or is_simulate, args.comment_author)

    logger.info("Router 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
