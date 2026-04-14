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
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

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

# ── Timezone ──────────────────────────────────────────────────────────────────
BEIJING_TZ = timezone(timedelta(hours=8))

# ── Action constants ───────────────────────────────────────────────────────────
ACTION_ADD = "add"
ACTION_DELETE = "delete"
ICON_ADD = "⭐ add"
ICON_DELETE = "❌ delete"


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

def mask_private(name, repo_id):
    """Show only a safe prefix of a private repo name, then ****-<id>.

    The numeric repo_id suffix ensures that two private repos sharing the same
    first PRIVATE_VISIBLE_CHARS characters produce distinct display names so
    star counts and log entries are never merged across repos.
    """
    visible_chars = min(PRIVATE_VISIBLE_CHARS, max(len(name) - 1, 0))
    return f"{name[:visible_chars]}****-{repo_id}"


# ── Markdown generation ────────────────────────────────────────────────────────

def parse_existing_star_log(filepath):
    """Parse the Star Log table from an existing output file.

    Returns a list of (datetime, repo, user, action) tuples where action is
    "add" or "delete".  Rows without an explicit action column are treated as
    "add" (backwards-compatible with the old 3-column format).
    """
    entries = []
    in_log_table = False
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if "| Time (UTC)" in line and "Repository" in line and "User" in line:
                    in_log_table = True
                    continue
                if in_log_table and (line.startswith("|---") or line.startswith("| ---")):
                    continue
                if in_log_table and line.startswith("|"):
                    parts = [p.strip() for p in line.split("|")]
                    parts = [p for p in parts if p != ""]
                    if len(parts) < 3:
                        continue
                    time_str = parts[0]
                    repo = parts[1].strip("`")
                    user_str = parts[2]
                    m = re.match(r"\[([^\]]+)\]", user_str)
                    user = m.group(1) if m else user_str
                    action_raw = parts[3].strip() if len(parts) >= 4 else ACTION_ADD
                    action = ACTION_DELETE if action_raw in (ACTION_DELETE, ICON_DELETE) else ACTION_ADD
                    try:
                        dt = datetime.fromisoformat(time_str)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    entries.append((dt, repo, user, action))
                elif in_log_table and not line.startswith("|"):
                    if line.strip():
                        in_log_table = False
    except FileNotFoundError:
        pass
    return entries


def _build_chart(all_stars):
    """Return a mermaid xychart-beta block for net cumulative stars over time.

    Each "add" event contributes +1 and each "delete" event contributes -1 to
    the running total, so the chart reflects the actual live star count.
    """
    if not all_stars:
        return ""

    # Aggregate net star delta by month
    monthly = defaultdict(int)
    for dt, _, _, action in all_stars:
        monthly[dt.strftime("%Y-%m")] += 1 if action == ACTION_ADD else -1

    months_asc = sorted(monthly)

    # Build cumulative series
    cum_pairs = []
    total = 0
    for m in months_asc:
        total += monthly[m]
        cum_pairs.append((m, total))

    # Reduce to at most 8 x-axis ticks to keep the chart readable
    MAX_TICKS = 8
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
        f'    title "{ORG} Star Growth"\n'
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
    all_stars   : list of (datetime, repo_display_name, username, action),
                  newest first.  action is "add" or "delete".
    repo_counts : dict of {display_name: total_star_count} (unfiltered)
    """
    now_str = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M:%S UTC+8")
    total_stars = sum(repo_counts.values())
    lines = []

    # ── Title and summary ──────────────────────────────────────────────────
    lines.append("# Star History\n")
    lines.append(
        f"_Last updated: {now_str}_  \n"
        f"_Total stars: {total_stars}_\n"
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
    for _, _, user, action in all_stars:
        if action == ACTION_ADD:
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
    lines.append("| Time (UTC+8) | Repository | User | Action |")
    lines.append("|:-----------|:-----------|:-----|:------:|")
    for dt, repo, user, action in all_stars:
        action_icon = ICON_ADD if action == ACTION_ADD else ICON_DELETE
        lines.append(
            f"| {dt.astimezone(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S+08:00')} | `{repo}` |"
            f" [{user}](https://github.com/{user}) | {action_icon} |"
        )
    lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main(output_file=OUTPUT_FILE):
    print(f"Fetching repos for org: {ORG}")
    repos = get_repos()
    print(f"  Found {len(repos)} repos")

    if EXCLUDE_USERS:
        print(f"  Excluding users from log: {', '.join(sorted(EXCLUDE_USERS))}")

    # Parse existing star log to detect future unstar events
    old_entries = parse_existing_star_log(output_file)
    existing_deletes = {
        (repo, user)
        for (_, repo, user, action) in old_entries
        if action == ACTION_DELETE
    }

    now = datetime.now(timezone.utc)
    all_stars = []   # (datetime, display_name, username, action)
    repo_counts = {}  # display_name -> total star count (unfiltered)

    for repo in repos:
        name = repo["name"]
        is_private = repo.get("private", False)
        display = mask_private(name, repo["id"]) if is_private else name

        label = f"{name} (private)" if is_private else name
        print(f"  Fetching stars for {label} …")

        try:
            raw = get_repo_stars(name)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else 0
            print(f"    Skipped (HTTP {code}): {exc}")
            continue

        # Count all stars (owner included) for repo ranking
        repo_counts[display] = len(raw)

        # Build filtered (external-only) add events for the log/chart
        current_external_users = set()
        for dt, user in raw:
            if user not in EXCLUDE_USERS:
                all_stars.append((dt, display, user, ACTION_ADD))
                current_external_users.add(user)

        # Detect newly unstarred users (old add → no longer a current stargazer)
        old_add_users = {
            user
            for (_, repo_d, user, action) in old_entries
            if repo_d == display and action == ACTION_ADD and user not in EXCLUDE_USERS
        }
        for user in old_add_users - current_external_users:
            if (display, user) not in existing_deletes:
                all_stars.append((now, display, user, ACTION_DELETE))

        print(f"    {repo_counts[display]} stars total"
              f" ({len(current_external_users)} external)")

    # Preserve all historical delete entries from the old log
    for entry in old_entries:
        if entry[3] == ACTION_DELETE:
            all_stars.append(entry)

    # Sort newest first
    all_stars.sort(key=lambda x: x[0], reverse=True)

    total = sum(repo_counts.values())
    print(f"\nTotal: {total} stars across {len(repo_counts)} repos")

    content = build_markdown(all_stars, repo_counts)

    # Only write if the data content changed (ignore the "Last updated" timestamp line)
    _timestamp_re = re.compile(r"^_Last updated:.*?_\s*$", re.MULTILINE)
    existing_body = ""
    try:
        with open(output_file, "r", encoding="utf-8") as f:
            existing_body = _timestamp_re.sub("", f.read())
    except FileNotFoundError:
        pass

    if _timestamp_re.sub("", content) == existing_body:
        print("No data changes detected. Skipping update.")
        return

    print(f"Writing {output_file} …")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(content)

    print("Done.")


if __name__ == "__main__":
    _out = sys.argv[1] if len(sys.argv) > 1 else OUTPUT_FILE
    main(_out)
