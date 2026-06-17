#!/usr/bin/env python3
"""Update CSM modsets repository listings in profile/README.md and csm-modsets.md.

Operates within ``<!-- CSM_MODSETS_START -->`` / ``<!-- CSM_MODSETS_END -->``
markers in profile/README.md to avoid touching unrelated content.

Usage:
    python scripts/update_csm_modsets.py [readme_path] [modsets_md_path]

Default paths:
    readme_path      = profile/README.md
    modsets_md_path  = csm-modsets.md

Environment variables:
    GITHUB_TOKEN      – GitHub personal access token (recommended for higher rate limits)
    GITHUB_REPOSITORY – used to build the full URL to csm-modsets.md (e.g. NEVSTOP-LAB/.github)
    CSM_MODSETS_URL   – override full URL prefix for csm-modsets.md (takes precedence)
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

# ── 确保包根目录在 sys.path（直接运行 scripts/ 时使用）──────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._utils import api_headers  # noqa: E402

# ── Constants ─────────────────────────────────────────────────────────────────
GITHUB_API = "https://api.github.com"
TOPIC = "csm-modsets"
MAX_PER_OWNER_IN_README = 5
DEFAULT_MARKER_START = "<!-- CSM_MODSETS_START -->"
DEFAULT_MARKER_END = "<!-- CSM_MODSETS_END -->"


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def _get_with_retry(url: str, params: dict | None = None, max_retries: int = 3) -> dict:
    """GET a GitHub API endpoint with retry and rate-limit handling."""
    headers = api_headers()
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=30)
            if resp.status_code in (403, 429):
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"  Rate limited – waiting {retry_after}s …", file=sys.stderr)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                wait = 2 ** attempt
                print(f"  Request failed ({exc}); retrying in {wait}s …", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"Failed after {max_retries} attempts: {last_exc}") from last_exc


def fetch_repos_with_topic(topic: str) -> list[dict]:
    """Return all public repos that carry *topic* (paginated, up to API limits).

    Note: The GitHub Search API caps results at 1,000. If the topic has more than
    1,000 public repositories only the first 1,000 will be returned and a warning
    is printed to stderr.
    """
    SEARCH_API_MAX = 1000
    repos: list[dict] = []
    page = 1
    total_count: int = 0
    while True:
        data = _get_with_retry(
            f"{GITHUB_API}/search/repositories",
            params={"q": f"topic:{topic}", "per_page": 100, "page": page},
        )
        items: list[dict] = data.get("items", [])
        repos.extend(items)
        total_count = data.get("total_count", 0)
        print(f"  Fetched page {page}: {len(items)} repos (total so far: {len(repos)}/{total_count})")
        if len(repos) >= total_count or len(items) < 100 or len(repos) >= SEARCH_API_MAX:
            break
        page += 1
        # Respect GitHub Search API secondary rate limits
        time.sleep(1)
    if total_count > SEARCH_API_MAX:
        print(
            f"  WARNING: total_count={total_count} exceeds the GitHub Search API limit of "
            f"{SEARCH_API_MAX} results. Only the first {SEARCH_API_MAX} repositories will be fetched.",
            file=sys.stderr,
        )
    return repos


# ── Data processing ────────────────────────────────────────────────────────────

def group_and_sort(repos: list[dict]) -> dict[str, list[dict]]:
    """Group repos by owner login; sort each group by stars desc.

    The owner groups themselves are ordered by descending total-star count.
    """
    raw: dict[str, list[dict]] = defaultdict(list)
    for repo in repos:
        raw[repo["owner"]["login"]].append(repo)

    for owner_repos in raw.values():
        owner_repos.sort(key=lambda r: r["stargazers_count"], reverse=True)

    return dict(
        sorted(raw.items(), key=lambda kv: sum(r["stargazers_count"] for r in kv[1]), reverse=True)
    )


# ── Content generators ─────────────────────────────────────────────────────────

def _repo_line_md(repo: dict) -> str:
    name = repo["name"]
    url = repo["html_url"]
    desc = repo.get("description") or ""
    stars = repo["stargazers_count"]
    star_str = f" ⭐{stars}" if stars > 0 else ""
    desc_str = f" - {desc}" if desc else ""
    return f"- [{name}]({url}){star_str}{desc_str}"


def generate_csm_modsets_md(groups: dict[str, list[dict]], updated_at: str) -> str:
    """Build the full content of csm-modsets.md."""
    lines = [
        "# CSM Modsets Repositories",
        "",
        f"> 自动生成，最后更新时间：{updated_at}",
        "",
        (
            "所有公开的、主题（topic）为 "
            "[`csm-modsets`](https://github.com/search?q=topic%3Acsm-modsets&type=repositories)"
            " 的仓库列表。"
        ),
        "",
    ]

    for owner, repos in groups.items():
        count = len(repos)
        # Use an explicit <a id> anchor so the fragment stays stable even when the
        # count changes.  GitHub Markdown preserves id attributes on <a> tags.
        lines.append(f'## <a id="{owner}"></a>[{owner}](https://github.com/{owner}) ({count})')
        lines.append("")
        lines.append("| 仓库 | ⭐ | 描述 |")
        lines.append("|------|:---:|------|")
        for repo in repos:
            name = repo["name"]
            url = repo["html_url"]
            desc = (repo.get("description") or "").replace("|", "\\|")
            stars = repo["stargazers_count"]
            star_str = str(stars) if stars > 0 else ""
            lines.append(f"| [{name}]({url}) | {star_str} | {desc} |")
        lines.append("")

    return "\n".join(lines)


def _csm_modsets_full_url(modsets_md_path: str) -> str:
    """Return the full URL base for csm-modsets.md links."""
    # Explicit override takes priority
    url = os.environ.get("CSM_MODSETS_URL")
    if url:
        return url.rstrip("/")

    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo:
        server = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/")
        return f"{server}/{repo}/blob/HEAD/{modsets_md_path}"

    # Fall back to a relative path (works for local testing)
    return modsets_md_path


def generate_readme_pre_content(
    groups: dict[str, list[dict]],
    modsets_md_path: str,
) -> str:
    """Build the HTML content that goes inside the <pre> block in profile/README.md."""
    base_url = _csm_modsets_full_url(modsets_md_path)
    html_lines: list[str] = []

    for owner, repos in groups.items():
        total = len(repos)
        display = repos[:MAX_PER_OWNER_IN_README]
        has_more = total > MAX_PER_OWNER_IN_README

        # Owner heading line
        html_lines.append(
            f'<a href="https://github.com/{html.escape(owner)}">{html.escape(owner)}</a> ({total})'
        )

        for repo in display:
            name = html.escape(repo["name"])
            url = html.escape(repo["html_url"])
            desc = html.escape(repo.get("description") or "")
            stars = repo["stargazers_count"]
            star_str = f" ⭐{stars}" if stars > 0 else ""
            desc_str = f" {desc}" if desc else ""
            html_lines.append(f'  <a href="{url}">{name}</a>{star_str}{desc_str}')

        if has_more:
            anchor_url = html.escape(f"{base_url}#{owner}")
            html_lines.append(f'  <a href="{anchor_url}">更多请查看 csm-modsets.md</a>')

        html_lines.append("")

    # Remove trailing blank line
    while html_lines and html_lines[-1] == "":
        html_lines.pop()

    return "\n".join(html_lines)


# ── File updaters ──────────────────────────────────────────────────────────────

def update_readme(
    readme_path: str,
    pre_content: str,
    *,
    marker_start: str = DEFAULT_MARKER_START,
    marker_end: str = DEFAULT_MARKER_END,
) -> bool:
    """Replace the *marker_start* / *marker_end* block in *readme_path*.

    If the markers are absent the block is appended before any trailing HTML
    comment.  Returns True when the file was actually changed.
    """
    with open(readme_path, encoding="utf-8") as fh:
        content = fh.read()

    new_block = (
        f"{marker_start}\n"
        f"<pre>\n{pre_content}\n</pre>\n"
        f"{marker_end}"
    )

    pattern = re.compile(
        re.escape(marker_start) + r".*?" + re.escape(marker_end),
        re.DOTALL,
    )

    if pattern.search(content):
        new_content = pattern.sub(new_block, content)
    else:
        # Insert before the last HTML comment block (if present) or append.
        pos = content.rfind("\n<!--")
        if pos != -1:
            new_content = content[:pos] + "\n\n" + new_block + "\n" + content[pos:]
        else:
            new_content = content.rstrip() + "\n\n" + new_block + "\n"

    if new_content == content:
        return False

    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
    return True


def write_csm_modsets_md(path: str, content: str) -> bool:
    """Write *content* to *path*.  Returns True when the file was changed."""
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == content:
                return False
    except FileNotFoundError:
        pass

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return True


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update CSM modsets listings")
    parser.add_argument(
        "readme_path", nargs="?", default="profile/README.md",
        help="Path to profile/README.md (default: %(default)s)",
    )
    parser.add_argument(
        "modsets_md_path", nargs="?", default="csm-modsets.md",
        help="Path to csm-modsets.md (default: %(default)s)",
    )
    parser.add_argument(
        "--marker-start", default=DEFAULT_MARKER_START,
        help="HTML comment marking the start of the editable region (default: %(default)s)",
    )
    parser.add_argument(
        "--marker-end", default=DEFAULT_MARKER_END,
        help="HTML comment marking the end of the editable region (default: %(default)s)",
    )
    args = parser.parse_args()

    print(f"Fetching public repos with topic '{TOPIC}' from GitHub …")
    repos = fetch_repos_with_topic(TOPIC)
    print(f"Total: {len(repos)} repositories found.")

    groups = group_and_sort(repos)
    print(f"Grouped into {len(groups)} owner(s): {', '.join(groups.keys())}")

    now = datetime.now(timezone.utc)
    updated_at = now.strftime("%Y-%m-%d %H:%M UTC")

    # ── Update csm-modsets.md ────────────────────────────────────────────────
    print(f"\nUpdating {args.modsets_md_path} …")
    modsets_content = generate_csm_modsets_md(groups, updated_at)
    changed = write_csm_modsets_md(args.modsets_md_path, modsets_content)
    print("  Updated." if changed else "  No changes.")

    # ── Update profile/README.md ─────────────────────────────────────────────
    print(f"\nUpdating {args.readme_path} …")
    pre_content = generate_readme_pre_content(groups, args.modsets_md_path)
    changed = update_readme(
        args.readme_path, pre_content,
        marker_start=args.marker_start,
        marker_end=args.marker_end,
    )
    print("  Updated." if changed else "  No changes.")

    print("\nDone.")
