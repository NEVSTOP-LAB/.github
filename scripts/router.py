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
import urllib.error
import urllib.request
from typing import Any, Optional

# ── 确保包根目录在 sys.path ─────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logger = logging.getLogger("org_router")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ── 常量 ────────────────────────────────────────────────────────────────────

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
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
    os.getenv("JOIN_STAR_REPOS", "csm-core,API String,MassData,INIVariable").split(",")
    if r.strip()
]
JOIN_STAR_OWNER = JOIN_FOLLOW_ORG  # Star 仓库所属组织与关注组织一致

# LLM 分类提示词
INTENT_CLASSIFY_PROMPT = """你是一个 GitHub 讨论区路由助手。请判断以下评论属于哪一类意图，只回复一个标签：

标签说明：
- JOIN：用户申请加入组织、想成为成员
- QA：用户提出技术问题或框架使用问题（可能包含 join/加入 等技术术语）
- OTHER：其他与上述无关的评论

用户评论：
'''
{comment_body}
'''

只回复标签名（JOIN / QA / OTHER），不要任何解释。"""

# DeepSeek API（兼容 OpenAI 格式）
LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.deepseek.com/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# QA 分类名（大小写精确匹配）
QA_CATEGORY_NAME = "Q&A"

# 降级正则
_RE_JOIN = re.compile(r"/join|加入|申请|apply", re.IGNORECASE)
_RE_QA = re.compile(r"问|？|\?|怎么|如何|是什么|报错|bug|error|请教|求助", re.IGNORECASE)

# ── GQL 客户端 ──────────────────────────────────────────────────────────────


class GQL:
    """最小化 GitHub GraphQL 客户端（stdlib urllib）。"""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("GitHub token (CSM_QA_GH_TOKEN) 未配置")
        self._token = token

    def query(self, gql: str, variables: Optional[dict] = None) -> dict:
        payload = json.dumps({"query": gql, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            GITHUB_GRAPHQL_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "org-router/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub GraphQL HTTP {exc.code}: {body[:400]}"
            ) from exc

        result: dict = json.loads(raw)
        if result.get("errors"):
            messages = "; ".join(e.get("message", "") for e in result["errors"])
            raise RuntimeError(f"GitHub GraphQL errors: {messages}")
        return result.get("data", {})


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


def classify_intent(comment_body: str) -> str:
    """对评论正文做 LLM 三分类，返回 ``"JOIN"`` / ``"QA"`` / ``"OTHER"``。

    失败时降级为正则匹配：
    - 含 join/加入/申请/apply → JOIN
    - 含问号/怎么/如何等 → QA
    - 其余 → OTHER
    """
    text = comment_body.strip()
    if not text:
        return "OTHER"

    if not LLM_API_KEY:
        logger.warning("LLM_API_KEY 未配置，使用正则降级分类")
        return _fallback_classify(text)

    try:
        payload = json.dumps({
            "model": LLM_MODEL,
            "messages": [
                {"role": "user", "content": INTENT_CLASSIFY_PROMPT.format(comment_body=text[:800])}
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
    if _RE_JOIN.search(text):
        logger.info("正则降级分类: JOIN")
        return "JOIN"
    if _RE_QA.search(text):
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
    """检查用户是否已在组织内。204=是，404=否。"""
    try:
        _rest_req(token, "GET", f"/orgs/{org}/members/{username}")
        return True  # 204
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise RuntimeError(f"检查成员资格失败: HTTP {exc.code}") from exc


def _check_star(token: str, username: str, owner: str, repo: str) -> tuple[bool, str]:
    """检查用户是否已 Star 指定仓库。返回 ``(passed, detail)``。"""
    try:
        resp = _rest_req(token, "GET", f"/users/{username}/starred/{owner}/{repo}")
        # 204 = 已 Star
        return True, f"已 Star {owner}/{repo}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False, f"未 Star {owner}/{repo}"
        raise RuntimeError(f"检查 Star 失败 {owner}/{repo}: HTTP {exc.code}") from exc


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

    # 条件 2：Star 全部指定仓库
    star_details: list[str] = []
    all_starred = True
    for repo in JOIN_STAR_REPOS:
        ok_star, detail_star = _check_star(token, username, JOIN_STAR_OWNER, repo)
        if not ok_star:
            all_starred = False
            star_details.append(repo)

    star_passed = all_starred
    star_detail = "已 Star 全部" if star_passed else f"缺少：{', '.join(star_details)}"
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
            f"🎉 全部通过 ({passed_count}/{total})！邀请已发送，"
            f"请查收 GitHub 邮件并点击 Accept。"
        )
    else:
        lines.append(
            f"🔴 需要全部满足，当前 {passed_count}/{total} 项通过。"
            f"满足后再次发送 `/join` 重试。"
        )

    lines.append("")
    lines.append(
        f"> ⭐ 需 Star 的仓库：{', '.join(JOIN_STAR_REPOS)}"
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
        logger.info("邀请已发送: org=%s user_id=%d status=%d", org, user_id, resp.status)
        return resp.status in (201, 200)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        logger.error("邀请发送失败: HTTP %d %s", exc.code, body[:400])
        return False


def post_reply(token: str, discussion_id: str, body: str) -> str:
    """向 Discussion 发布评论，返回新评论的 node ID。"""
    gql_client = GQL(token)
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
) -> None:
    """处理 QA 意图：Q&A 分类下调用 CSM_QA 回答，否则引导。"""
    source_owner, source_repo = _get_source_repo_parts()

    if category_name != QA_CATEGORY_NAME:
        # 非 Q&A 分类 → 引导到 Q&A 区
        gql_client = GQL(token)
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
        return

    # Q&A 分类 → 延迟导入 CSM_QA 和 discussion_bot 函数
    logger.info("Q&A 分类下的 QA 请求，初始化 CSM_QA…")
    from scripts.discussion_bot import (  # type: ignore[import-not-found]
        GitHubGraphQL,
        compute_reply_plan,
        build_reply,
        post_comment,
        fetch_discussion as fetch_disc,
    )
    from csm_llm_qa import CSM_QA

    client = GitHubGraphQL(token)
    qa_engine = CSM_QA.from_env()

    # 获取 Bot 自身的登录名（用于 compute_reply_plan 作者校验）
    try:
        viewer_data = client.query("query { viewer { login } }")
        bot_login = viewer_data.get("viewer", {}).get("login")
    except Exception:
        bot_login = None

    discussion = fetch_disc(client, source_owner, source_repo, discussion_number)
    disc_id = discussion.get("id", "")

    plan = compute_reply_plan(discussion, bot_login)
    if plan is None:
        logger.info("无需回复（已回复且无追问）")
        return

    question, history = plan
    logger.info("生成回答中 (question=%s chars, history=%d turns)", len(question), len(history))

    if not dry_run:
        answer = qa_engine.ask(question, history=history)
        reply_body = build_reply(answer)  # build_reply 已含 footer + marker
        post_comment(client, disc_id, reply_body)
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
    """处理 JOIN 意图：条件检测 → 报告 → 通过则邀请。"""
    if not comment_author:
        logger.warning("未提供 comment_author，无法执行 JOIN 检测")
        return

    source_owner, source_repo = _get_source_repo_parts()
    gql_client = GQL(token)

    # 0. 先检查是否已在组织内
    if _is_org_member(token, JOIN_FOLLOW_ORG, comment_author):
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

    logger.info(
        "JOIN 检测: username=%s org=%s star_repos=%s",
        comment_author, JOIN_FOLLOW_ORG, JOIN_STAR_REPOS,
    )

    # 条件检测
    all_met, results = check_all_conditions(token, comment_author)

    # 拉取 discussion 获取 node ID（复用已创建的 gql_client）
    discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)
    disc_id = discussion.get("id", "")

    # 生成报告
    report = build_condition_report(comment_author, all_met, results)

    if all_met:
        # 通过 → 发送邀请
        try:
            user_id = _resolve_user_id(token, comment_author)
            ok = send_invitation(token, JOIN_FOLLOW_ORG, user_id)
            if not ok:
                report += "\n\n⚠️ 邀请发送失败，请联系管理员。"
        except Exception as exc:
            logger.error("邀请流程失败: %s", exc)
            report += f"\n\n⚠️ 邀请发送失败（{exc}），请联系管理员。"

    if not dry_run:
        post_reply(token, disc_id, report)
    else:
        logger.info("[DRY-RUN] 将发布 JOIN 报告:\n%s", report)


def _handle_other(
    token: str,
    discussion_number: int,
    dry_run: bool,
) -> None:
    """处理 OTHER 意图：友好引导回复。"""
    source_owner, source_repo = _get_source_repo_parts()
    gql_client = GQL(token)
    discussion = fetch_discussion(gql_client, source_owner, source_repo, discussion_number)
    disc_id = discussion.get("id", "")

    body = (
        "👋 你好！我暂时无法识别你的意图。\n\n"
        "你可以：\n"
        "- 发送 **`/join`** 申请加入组织\n"
        "- 在 [Q&A 分类](https://github.com/orgs/{org}/discussions/categories/q-a) "
        "下提出技术问题\n"
        "- 直接描述你的需求，我会尝试引导你\n\n"
        "感谢使用！"
    ).format(org=source_owner)

    if not dry_run:
        post_reply(token, disc_id, body)
    else:
        logger.info("[DRY-RUN] 将发布 OTHER 引导: discussion_id=%s", disc_id)


# ── CLI ──────────────────────────────────────────────────────────────────────


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Org Discussion Router")
    parser.add_argument(
        "--discussion-number",
        type=int,
        required=True,
        help="Discussion 编号",
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
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    _configure_logging()
    args = parse_args(argv)

    token = os.environ.get("CSM_QA_GH_TOKEN", "")
    if not token:
        logger.error("CSM_QA_GH_TOKEN 未配置")
        return 1

    logger.info(
        "Router 启动: discussion=%d author=%s category=%s dry_run=%s",
        args.discussion_number,
        args.comment_author,
        args.category_name,
        args.dry_run,
    )

    # 1. 构造分类输入：discussion 事件将标题拼入正文（标题承载主要意图）
    classify_input = args.comment_body
    if args.event_type == "discussion" and args.discussion_title.strip():
        classify_input = f"{args.discussion_title.strip()}\n\n{args.comment_body}".strip()

    intent = classify_intent(classify_input)
    logger.info("意图分类: %s", intent)

    # 1b. Q&A 分类的 discussion.created：正文短 → 直接按 QA 处理
    if (
        args.event_type == "discussion"
        and intent == "OTHER"
        and args.category_name == QA_CATEGORY_NAME
        and len(args.comment_body.strip()) <= 20
    ):
        logger.info("短正文 + Q&A 分类 + discussion.created → 按 QA 处理")
        intent = "QA"

    # 1c. 空评论的特殊处理：discussion.created 事件无评论、但正文可能是 QA
    if not classify_input.strip() and args.event_type == "discussion":
        if args.category_name == QA_CATEGORY_NAME:
            logger.info("空内容 + Q&A 分类 + discussion.created → 按 QA 处理")
            intent = "QA"
        else:
            logger.info("空内容 + 非 Q&A + discussion.created → 跳过（不回复）")
            return 0

    # 2. 按意图分派
    if intent == "JOIN":
        _handle_join(token, args.discussion_number, args.comment_author, args.dry_run)
    elif intent == "QA":
        _handle_qa(token, args.discussion_number, args.category_name, args.dry_run)
    else:
        _handle_other(token, args.discussion_number, args.dry_run)

    logger.info("Router 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
