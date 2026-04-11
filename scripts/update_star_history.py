#!/usr/bin/env python3
"""Fetch external stars from NEVSTOP-LAB repos and update Star-History.md.

Usage:
    python scripts/update_star_history.py [output_file]

Environment variables:
    GITHUB_TOKEN            GitHub token with org read access (required)
    ORG                     GitHub org name (default: NEVSTOP-LAB)
    PRIVATE_VISIBLE_CHARS   Characters of private repo name to reveal (default: 10)
    EXCLUDE_USERS           Comma-separated usernames to exclude (default: "")
    TOP_N                   Number of top repos in ranking (default: 10)
    OUTPUT_FILE             Output markdown path (default: Star-History.md)
"""

import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ── Configuration ─────────────────────────────────────────────────────────────
ORG = os.environ.get("ORG", "NEVSTOP-LAB")
PRIVATE_VISIBLE_CHARS = int(os.environ.get("PRIVATE_VISIBLE_CHARS", "10"))
EXCLUDE_USERS = {
    u.strip()
    for u in os.environ.get("EXCLUDE_USERS", "").split(",")
    if u.strip()
}
TOP_N = int(os.environ.get("TOP_N", "10"))
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "Star-History.md")

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
if not GITHUB_TOKEN:
    print("ERROR: Set GITHUB_TOKEN or GH_TOKEN environment variable.", file=sys.stderr)
    sys.exit(1)

_BASE_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
_STAR_HEADERS = {**_BASE_HEADERS, "Accept": "application/vnd.github.star+v3+json"}


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def _paginate(url, headers, extra_params=None):
    """Yield all items from a paginated GitHub API endpoint."""
    params = {"per_page": 100, **(extra_params or {})}
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 404:
            raise requests.HTTPError(
                f"GitHub API endpoint not found or inaccessible: {resp.url}",
                response=resp,
            )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return
        yield from data
        if len(data) < 100:
            return
        page += 1
        time.sleep(0.05)  # be polite to the API


def get_repos():
    """Return list of all repos in the org (public + private)."""
    url = f"https://api.github.com/orgs/{ORG}/repos"
    return list(_paginate(url, _BASE_HEADERS, {"type": "all"}))


def get_repo_stars(repo_name):
    """Return list of (starred_at datetime, username) for a repo."""
    url = f"https://api.github.com/repos/{ORG}/{repo_name}/stargazers"
    result = []
    for item in _paginate(url, _STAR_HEADERS):
        starred_at = item.get("starred_at")
        user = item.get("user", {}).get("login", "")
        if starred_at and user:
            dt = datetime.fromisoformat(starred_at.replace("Z", "+00:00"))
            result.append((dt, user))
    return result


# ── Helpers ────────────────────────────────────────────────────────────────────

def mask_private(name):
    """Show only a safe prefix of a private repo name, then ****."""
    visible_chars = min(PRIVATE_VISIBLE_CHARS, max(len(name) - 1, 0))
    return name[:visible_chars] + "****"


# ── Markdown generation ────────────────────────────────────────────────────────

def _build_chart(all_stars):
    """Return a mermaid xychart-beta block for cumulative stars over time."""
    if not all_stars:
        return ""

    # Aggregate star counts by month
    monthly = defaultdict(int)
    for dt, _, _ in all_stars:
        monthly[dt.strftime("%Y-%m")] += 1

    months_asc = sorted(monthly)

    # Build cumulative series
    cum_pairs = []
    total = 0
    for m in months_asc:
        total += monthly[m]
        cum_pairs.append((m, total))

    # Reduce to at most 24 x-axis ticks to keep the chart readable
    MAX_TICKS = 24
    CHART_Y_PADDING = 1.2  # 20 % headroom above the highest value
    if len(cum_pairs) > MAX_TICKS:
        step = math.ceil(len(cum_pairs) / MAX_TICKS)
        sampled = cum_pairs[::step]
        if sampled[-1] != cum_pairs[-1]:
            sampled.append(cum_pairs[-1])
    else:
        sampled = cum_pairs

    labels = ", ".join(f'"{m}"' for m, _ in sampled)
    values = ", ".join(str(v) for _, v in sampled)
    y_max = max(v for _, v in sampled)
    # Round up to the nearest 10 after applying padding headroom
    y_ceil = int(math.ceil(y_max * CHART_Y_PADDING / 10) * 10)

    return (
        "```mermaid\n"
        "xychart-beta\n"
        f'    title "NEVSTOP-LAB Star Growth"\n'
        f"    x-axis [{labels}]\n"
        f'    y-axis "Cumulative Stars" 0 --> {y_ceil}\n'
        f"    line [{values}]\n"
        "```"
    )


def build_markdown(all_stars, repo_counts):
    """
    Build the full Star-History.md content.

    Parameters
    ----------
    all_stars   : list of (datetime, repo_display_name, username), newest first
    repo_counts : dict of {display_name: star_count}
    """
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []

    # ── Title and summary ──────────────────────────────────────────────────
    lines.append("# Star History\n")
    lines.append(
        f"_Last updated: {now_str}_  \n"
        f"_Total external stars: {len(all_stars)}_\n"
    )

    # ── Mermaid growth chart ───────────────────────────────────────────────
    lines.append("## Star Growth Chart\n")
    chart = _build_chart(all_stars)
    if chart:
        lines.append(chart)
    else:
        lines.append("_No star data yet._")
    lines.append("")

    # ── Top N repos ranking ────────────────────────────────────────────────
    top_repos = sorted(repo_counts.items(), key=lambda x: -x[1])[:TOP_N]
    lines.append(f"## Top {TOP_N} Most Starred Repositories\n")
    lines.append("| Rank | Repository | Stars |")
    lines.append("|:----:|:-----------|------:|")
    for rank, (repo, count) in enumerate(top_repos, 1):
        lines.append(f"| {rank} | `{repo}` | {count} |")
    lines.append("")

    # ── Top N users by number of repos starred ────────────────────────────
    user_counts: dict[str, int] = defaultdict(int)
    for _, _, user in all_stars:
        user_counts[user] += 1
    top_users = sorted(user_counts.items(), key=lambda x: -x[1])[:TOP_N]
    lines.append(f"## Top {TOP_N} Users by Stars Given\n")
    lines.append("| Rank | User | Stars Given |")
    lines.append("|:----:|:-----|------------:|")
    for rank, (user, count) in enumerate(top_users, 1):
        lines.append(f"| {rank} | [{user}](https://github.com/{user}) | {count} |")
    lines.append("")

    # ── Full star log table ────────────────────────────────────────────────
    lines.append("## Star Log\n")
    lines.append("| Time (UTC) | Repository | User |")
    lines.append("|:-----------|:-----------|:-----|")
    for dt, repo, user in all_stars:
        lines.append(
            f"| {dt.strftime('%Y-%m-%d %H:%M:%S')} | `{repo}` | [{user}](https://github.com/{user}) |"
        )
    lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main(output_file=OUTPUT_FILE):
    print(f"Fetching repos for org: {ORG}")
    repos = get_repos()
    print(f"  Found {len(repos)} repos")

    if EXCLUDE_USERS:
        print(f"  Excluding users: {', '.join(sorted(EXCLUDE_USERS))}")

    all_stars = []   # (datetime, display_name, username)
    repo_counts = {}  # display_name -> star count

    for repo in repos:
        name = repo["name"]
        is_private = repo.get("private", False)
        display = mask_private(name) if is_private else name

        label = f"{name} (private)" if is_private else name
        print(f"  Fetching stars for {label} …")

        try:
            raw = get_repo_stars(name)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            print(f"    Skipped (HTTP {code}): {exc}")
            continue

        filtered = [
            (dt, display, user)
            for dt, user in raw
            if user not in EXCLUDE_USERS
        ]
        all_stars.extend(filtered)
        repo_counts[display] = len(filtered)
        print(f"    {len(filtered)} stars")

    # Sort newest first
    all_stars.sort(key=lambda x: x[0], reverse=True)

    print(f"\nTotal: {len(all_stars)} stars across {len(repo_counts)} repos")
    print(f"Writing {output_file} …")

    content = build_markdown(all_stars, repo_counts)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(content)

    print("Done.")


if __name__ == "__main__":
    _out = sys.argv[1] if len(sys.argv) > 1 else OUTPUT_FILE
    main(_out)
