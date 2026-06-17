#!/usr/bin/env python3
"""Org Membership Cleanup — auto-downgrade inactive organization members.

由 ``org-membership-cleanup.yml`` workflow 在每日 schedule 或手动
workflow_dispatch 时调用。

核心逻辑
────────
1. 以 ``csm-developer`` 为锚点，沿 GitHub Team parent 链向上追溯，
   获得完整团队层级链（如 ``csm-community → csm-module-author → csm-developer``）。
2. 遍历组织所有成员，判定其在链中的级别。锚点（csm-developer）永久豁免。
3. 对每个用户：若距上次检查已满 14 天，查询过去 ``[last_check, now]`` 区间的
   所有贡献（commits + authored/assigned issues + PRs）。
4. 有贡献 → ``last_check`` 更新为最近贡献时间，不降级。
5. 无贡献 → 沿链降一级（链底则移出组织）→ ``last_check = now``。
6. 状态持久化至 ``data/member_check_state.json``，由 workflow commit 回仓库。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests

# ── 确保包根目录在 sys.path ─────────────────────────────────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._utils import api_headers, configure_logging, paginate  # noqa: E402

logger = logging.getLogger("org_membership_cleanup")

# ── 常量 ────────────────────────────────────────────────────────────────────

GITHUB_API_URL = "https://api.github.com"
ORG = "NEVSTOP-LAB"
ANCHOR_TEAM = "csm-developer"
CHECK_INTERVAL_DAYS = 14
STATE_FILE = os.path.join(_REPO_ROOT, "data", "member_check_state.json")

# 贡献查询：每类搜索最多拉取条数（只需判断有无 + 取最新时间）
SEARCH_PER_PAGE = 5


# ── 工具函数 ────────────────────────────────────────────────────────────────


def _get_token() -> str:
    """从环境变量获取 SYNC_GITHUB_TOKEN。"""
    token = os.environ.get("SYNC_GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("SYNC_GITHUB_TOKEN 未配置")
    return token


def _rest_get(token: str, path: str, **params: Any) -> Any:
    """GET 请求 GitHub REST API，返回解析后的 JSON。"""
    url = f"{GITHUB_API_URL}{path}"
    resp = requests.get(url, headers=api_headers(token), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _rest_delete(token: str, path: str) -> bool:
    """DELETE 请求 GitHub REST API。成功返回 True。"""
    url = f"{GITHUB_API_URL}{path}"
    resp = requests.delete(url, headers=api_headers(token), timeout=30)
    resp.raise_for_status()
    return True


# ── 团队链发现 ──────────────────────────────────────────────────────────────


def discover_team_chain(token: str, org: str, anchor_slug: str) -> list[str]:
    """从锚点团队沿 parent 链向上追溯，返回从根到锚的有序列表。

    Example:
        discover_team_chain(token, "NEVSTOP-LAB", "csm-developer")
        → ["csm-community", "csm-module-author", "csm-developer"]
    """
    chain: list[str] = []
    current: Optional[str] = anchor_slug
    while current:
        try:
            team = _rest_get(token, f"/orgs/{org}/teams/{current}")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.warning("团队 '%s' 不存在，停止链追溯", current)
                break
            raise
        chain.append(team["slug"])
        parent = team.get("parent")
        current = parent["slug"] if parent else None
    chain.reverse()
    logger.info("团队层级链: %s", " → ".join(chain))
    return chain


# ── 用户级别判定 ────────────────────────────────────────────────────────────


def get_user_level(token: str, org: str, username: str, chain: list[str]) -> int:
    """返回用户在团队链中的索引，-1 表示不在任何 CSM 团队中。"""
    for i, slug in enumerate(chain):
        try:
            resp = requests.get(
                f"{GITHUB_API_URL}/orgs/{org}/teams/{slug}/memberships/{username}",
                headers=api_headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info("用户 %s 在团队 %s（级别 %d/%d）", username, slug, i, len(chain) - 1)
                return i
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                continue
            # 403 限速 / 5xx 服务端错误 → 向上抛出避免误判
            logger.error(
                "查询团队成员失败 %s/%s: HTTP %s",
                slug, username,
                exc.response.status_code if exc.response is not None else "?",
            )
            raise
    return -1


# ── 贡献查询 ────────────────────────────────────────────────────────────────


def _parse_iso_datetime(date_str: str) -> datetime:
    """将 ISO-8601 字符串解析为 UTC datetime。"""
    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    # 无时区偏移的旧格式字段 → 强制设为 UTC，避免后续 aware/naive 混合运算崩溃
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def query_last_contribution_time(
    token: str, org: str, username: str, since: datetime,
) -> Optional[datetime]:
    """查询用户自 ``since`` 以来最近一次贡献的时间戳。

    搜索范围：authored issues/PRs + assigned issues/PRs + commits。
    无贡献时返回 None。
    """
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%S")
    latest: Optional[datetime] = None

    # ── Issues / PRs 综合查询 ──────────────────────────────────────────
    # 用 created / closed 替代 updated 避免误判（其他人的更新不算用户贡献）
    issue_queries: list[tuple[str, str]] = [
        (f"org:{org} author:{username} created:>={since_iso}", "created"),
        (f"org:{org} assignee:{username} closed:>={since_iso}", "updated"),
    ]
    for q, sort_field in issue_queries:
        try:
            data = _rest_get(
                token, "/search/issues",
                q=q, sort=sort_field, order="desc", per_page=SEARCH_PER_PAGE,
            )
            for item in data.get("items", []):
                # 取 closed_at / created_at（与查询类型对应）
                time_str = item.get("closed_at") or item.get("created_at")
                if time_str:
                    dt = _parse_iso_datetime(time_str)
                    if latest is None or dt > latest:
                        latest = dt
        except Exception as exc:
            logger.warning("Issues 搜索失败 (%s): %s", q[:60], exc)
            # 403/429 限速 → 向上抛出，避免误判为"无贡献"导致错误降级
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                if exc.response.status_code in (403, 429):
                    raise
        # 搜索 API 限速 30 req/min，加短暂延迟
        time.sleep(2.0)

    # ── Commits 查询（需特殊 Accept 头）───────────────────────────────
    try:
        commit_headers = api_headers(
            token,
            extra_accept="application/vnd.github.cloak-preview+json",
        )
        resp = requests.get(
            f"{GITHUB_API_URL}/search/commits",
            headers=commit_headers,
            params={
                "q": f"org:{org} author:{username} committer-date:>={since_iso}",
                "sort": "committer-date",
                "order": "desc",
                "per_page": SEARCH_PER_PAGE,
            },
            timeout=30,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        for item in items:
            date_str = (item.get("commit", {})
                        .get("committer", {})
                        .get("date", ""))
            if date_str:
                dt = _parse_iso_datetime(date_str)
                if latest is None or dt > latest:
                    latest = dt
    except Exception as exc:
        logger.warning("Commits 搜索失败: %s", exc)
        # 403/429 限速 → 向上抛出，避免误判
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            if exc.response.status_code in (403, 429):
                raise
    # Commits 搜索也计入 Search API 配额，加延迟
    time.sleep(2.0)

    return latest


# ── 降级操作 ────────────────────────────────────────────────────────────────


def _user_in_other_teams(
    token: str, org: str, username: str, chain: list[str],
) -> bool:
    """检查用户是否属于 CSM 链之外的任何团队。

    遍历组织所有团队，排除链内团队，检查用户是否仍有成员身份。
    查询失败时保守处理：视为有其他团队，保留组织身份。
    """
    chain_set = set(chain)
    try:
        url = f"{GITHUB_API_URL}/orgs/{org}/teams"
        headers = api_headers(token)
        all_teams = paginate(url, headers)
    except Exception as exc:
        logger.warning("获取组织团队列表失败，保守处理：保留组织身份: %s", exc)
        return True  # 无法确认 → 保留，避免误移除

    for team in all_teams:
        slug = team.get("slug", "")
        if slug in chain_set:
            continue
        try:
            resp = requests.get(
                f"{GITHUB_API_URL}/orgs/{org}/teams/{slug}/memberships/{username}",
                headers=api_headers(token),
                timeout=15,
            )
            if resp.status_code == 200:
                logger.info(
                    "用户 %s 仍在其他团队 %s 中，保留组织身份",
                    username, slug,
                )
                return True
        except requests.HTTPError:
            continue  # 404 = 不在该团队，继续检查下一个
    return False


def downgrade_user(
    token: str, org: str, username: str, current_idx: int, chain: list[str],
) -> Optional[str]:
    """将用户沿团队链降一级。

    Args:
        current_idx: 用户在链中的当前索引。
        chain: 完整团队链（根在前，锚在后）。

    Returns:
        降级后用户所在团队 slug；若已移出组织则返回 None。
    """
    current_team = chain[current_idx]

    if current_idx == 0:
        # 已在链底（如 csm-community）
        # 先移除 CSM-Community 团队身份
        logger.warning("⬇ 降级 %s: 移除 %s 团队", username, current_team)
        try:
            _rest_delete(
                token,
                f"/orgs/{org}/teams/{current_team}/memberships/{username}",
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.info("%s 已不在 %s 中", username, current_team)
            else:
                raise

        # 检查是否还在其他团队中
        if _user_in_other_teams(token, org, username, chain):
            logger.info(
                "%s 在其他团队中仍有身份，保留组织成员资格",
                username,
            )
            return "kept"

        # 无其他团队 → 移出组织
        logger.warning("⛔ %s: 无其他团队，移出组织", username)
        try:
            _rest_delete(token, f"/orgs/{org}/memberships/{username}")
            logger.info("已移除 %s 的组织成员身份", username)
            return None
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.info("%s 已不在组织中", username)
                return None
            raise
    else:
        # 从当前团队移除，用户自然降级到父团队
        parent_team = chain[current_idx - 1]
        logger.warning("⬇ 降级 %s: %s → %s", username, current_team, parent_team)
        try:
            _rest_delete(
                token,
                f"/orgs/{org}/teams/{current_team}/memberships/{username}",
            )
            logger.info("已从 %s 移除 %s，现属 %s", current_team, username, parent_team)
            return parent_team
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                logger.info("%s 已不在 %s 中", username, current_team)
                return parent_team
            raise


# ── 状态文件 ────────────────────────────────────────────────────────────────


def load_state() -> dict:
    """加载成员检查状态文件。"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "_comment": "Per-user state for org-membership-cleanup workflow. DO NOT edit manually.",
        "_schema": {
            "users": {
                "<github-username>": {
                    "last_check": "ISO-8601 datetime of last check or last known contribution",
                    "team": "current CSM team slug (e.g. csm-community, csm-module-author) or 'removed'",
                }
            }
        },
        "users": {},
    }


def save_state(state: dict) -> None:
    """保存成员检查状态文件。"""
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ── 组织成员列表 ────────────────────────────────────────────────────────────


def list_org_members(token: str, org: str) -> list[str]:
    """获取组织所有公开成员的用户名列表。

    使用 ``_utils.paginate`` 自动处理分页。
    """
    url = f"{GITHUB_API_URL}/orgs/{org}/members"
    headers = api_headers(token)
    items = paginate(url, headers)
    return [m["login"] for m in items]


# ── 主流程 ──────────────────────────────────────────────────────────────────


def run(dry_run: bool = False) -> None:
    """执行一次完整的成员活跃度检查与降级。"""
    token = _get_token()
    now = datetime.now(timezone.utc)

    # 1. 发现团队层级链
    chain = discover_team_chain(token, ORG, ANCHOR_TEAM)
    if len(chain) < 2:
        logger.error(
            "团队链过短（%d 级），至少需要 2 级才能执行降级操作。链: %s",
            len(chain), chain,
        )
        return
    anchor_idx = len(chain) - 1

    # 2. 加载状态
    state = load_state()
    users_state: dict = state.setdefault("users", {})

    # 3. 列出所有组织成员
    try:
        members = list_org_members(token, ORG)
        logger.info("组织共有 %d 名成员", len(members))
    except Exception as exc:
        logger.error("获取组织成员列表失败: %s", exc)
        return

    # 4. 逐用户检查
    summary: dict[str, int] = {"skipped": 0, "passed": 0, "downgraded": 0, "removed": 0}

    for username in members:
        # 4a. 判定级别
        level_idx = get_user_level(token, ORG, username, chain)

        if level_idx < 0:
            logger.debug("跳过 %s: 不在任何 CSM 团队中", username)
            summary["skipped"] += 1
            continue

        # 4b. 锚点豁免
        if level_idx == anchor_idx:
            logger.debug("跳过 %s: 锚点级别（%s），永久豁免", username, chain[anchor_idx])
            summary["skipped"] += 1
            continue

        # 4c. 解析上次检查时间
        user_state = users_state.get(username, {})
        last_check_str: Optional[str] = user_state.get("last_check")
        if last_check_str:
            try:
                last_check = _parse_iso_datetime(last_check_str)
            except (ValueError, TypeError):
                # 状态文件被篡改或旧格式无时区 → 按首次处理
                logger.warning(
                    "%s: last_check 解析失败 (%s)，按首次检查处理",
                    username, last_check_str,
                )
                last_check = now - timedelta(days=CHECK_INTERVAL_DAYS + 1)
        else:
            # 首次遇见 → 初始化为 14 天前，立即触发检查
            last_check = now - timedelta(days=CHECK_INTERVAL_DAYS + 1)
            logger.info("%s: 首次记录，设为需检查状态", username)

        # 4d. 检查窗口判定
        days_since = (now - last_check).days
        if days_since < CHECK_INTERVAL_DAYS:
            logger.debug(
                "跳过 %s: 距上次检查仅 %d 天（需 >= %d）",
                username, days_since, CHECK_INTERVAL_DAYS,
            )
            summary["skipped"] += 1
            # 确保状态文件中有记录
            if username not in users_state:
                users_state[username] = {
                    "last_check": last_check.isoformat(),
                    "team": chain[level_idx],
                }
            continue

        # 4e. 执行检查
        current_team = chain[level_idx]
        logger.info(
            "🔍 检查 %s: 级别=%s (%d/%d), 上次检查=%s, 距今=%d 天",
            username, current_team, level_idx, anchor_idx,
            last_check.isoformat(), days_since,
        )

        try:
            last_contribution = query_last_contribution_time(
                token, ORG, username, last_check,
            )
        except Exception as exc:
            # Search API 限速或其他不可恢复错误 → 跳过该用户，不降级
            logger.error(
                "⚠️ %s: 贡献查询失败 (%s)，跳过本次检查（不降级）",
                username, exc,
            )
            summary["skipped"] += 1
            continue

        if last_contribution is not None:
            # 有贡献 → 更新 last_check 到最近贡献时间
            logger.info(
                "✅ %s: 有贡献（最近 %s），更新检查时间",
                username, last_contribution.isoformat(),
            )
            users_state[username] = {
                "last_check": last_contribution.isoformat(),
                "team": current_team,
            }
            summary["passed"] += 1
        else:
            # 无贡献 → 降级
            logger.warning(
                "❌ %s: 自 %s 以来无贡献，执行降级",
                username, last_check.isoformat(),
            )
            if dry_run:
                if level_idx == 0:
                    logger.info("[DRY-RUN] 将移除 %s 的组织成员身份", username)
                else:
                    logger.info(
                        "[DRY-RUN] 将降级 %s: %s → %s",
                        username, current_team, chain[level_idx - 1],
                    )
                users_state[username] = {
                    "last_check": now.isoformat(),
                    "team": current_team,
                }
                summary["downgraded"] += 1
            else:
                try:
                    new_team = downgrade_user(token, ORG, username, level_idx, chain)
                    if new_team is None:
                        # 已移出组织
                        users_state[username] = {
                            "last_check": now.isoformat(),
                            "team": "removed",
                        }
                        summary["removed"] += 1
                    elif new_team == "kept":
                        # 有其他团队，保留组织身份
                        users_state[username] = {
                            "last_check": now.isoformat(),
                            "team": current_team,
                        }
                        summary["downgraded"] += 1
                    else:
                        # 降级到父团队
                        users_state[username] = {
                            "last_check": now.isoformat(),
                            "team": new_team,
                        }
                        summary["downgraded"] += 1
                except Exception as exc:
                    logger.error("降级 %s 失败: %s", username, exc)
                    # 仍然更新 last_check 避免重复尝试
                    users_state[username] = {
                        "last_check": now.isoformat(),
                        "team": current_team,
                    }

    # 5. 保存状态（dry-run 不落盘，避免错误推进 last_check）
    if not dry_run:
        save_state(state)
        logger.info("状态已保存至 %s", STATE_FILE)
    else:
        logger.info("[DRY-RUN] 跳过状态保存，无文件变更")

    # 6. 汇总
    logger.info(
        "检查完成: 跳过=%d  通过=%d  降级=%d  移除=%d",
        summary["skipped"], summary["passed"],
        summary["downgraded"], summary["removed"],
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Org Membership Cleanup — 自动降级不活跃组织成员",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="仅输出操作日志，不实际执行降级",
    )
    args = parser.parse_args(argv)

    run(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
