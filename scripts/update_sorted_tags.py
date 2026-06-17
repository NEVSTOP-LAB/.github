#!/usr/bin/env python3
"""Update the "Sorted By Tags" section in profile/README.md.

Operates within ``<!-- SORTED_TAGS_START -->`` / ``<!-- SORTED_TAGS_END -->``
markers to avoid touching unrelated content.

Logic:
- Fetch all public repositories in the organization.
- Count topic/tag usage across repositories.
- Keep tags whose count is greater than MIN_TAG_COUNT (default 1).
- Sort tags by count descending, then by name.
- Render each line as: [`tag(count)`](search-url)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from urllib.parse import quote

# ── 确保包根目录在 sys.path（直接运行 scripts/ 时使用）──────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._utils import api_headers, marker_start, marker_end, paginate  # noqa: E402

ORG = os.environ.get("ORG", "NEVSTOP-LAB")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "profile/README.md")
MIN_TAG_COUNT = int(os.environ.get("MIN_TAG_COUNT", "1"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

# ── Region marker ─────────────────────────────────────────────────────────────
DEFAULT_REGION = "SORTED_TAGS"


def get_public_repos() -> list[dict]:
    url = f"https://api.github.com/orgs/{ORG}/repos"
    headers = api_headers(GITHUB_TOKEN, user_agent="NEVSTOP-LAB-sorted-tags-updater")
    repos = paginate(url, headers, {"type": "public"})

    # Keep the first occurrence of each repository to avoid duplicated counts
    # if paginated API responses overlap between requests.
    unique_repos: dict[str, dict] = {}
    for repo in repos:
        repo_id = repo.get("id")
        if repo_id is not None:
            key = str(repo_id)
        else:
            key = repo.get("full_name") or repo.get("name")
        if key is not None and key not in unique_repos:
            unique_repos[key] = repo
    return list(unique_repos.values())


def build_tag_lines(repos: list[dict]) -> list[str]:
    counts: Counter[str] = Counter()
    for repo in repos:
        for topic in set(repo.get("topics", [])):
            if topic:
                counts[topic] += 1

    filtered = [(tag, count) for tag, count in counts.items() if count > MIN_TAG_COUNT]
    filtered.sort(key=lambda item: (-item[1], item[0].lower()))

    lines: list[str] = []
    for tag, count in filtered:
        query = quote(f"topic:{tag} org:{ORG} is:public", safe=":")
        url = f"https://github.com/search?q={query}&type=Repositories"
        lines.append(f"[`{tag}({count})`]({url})")
    return lines


def update_readme(
    readme_path: str,
    tag_lines: list[str],
    *,
    region: str = DEFAULT_REGION,
) -> bool:
    """Replace the content between *region* markers with *tag_lines*.

    Returns ``True`` if the file was modified, ``False`` otherwise.
    """
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()

    marker_s = marker_start(region)
    marker_e = marker_end(region)

    # ── Locate the marker block ───────────────────────────────────────────
    start_pos = content.find(marker_s)
    end_pos = content.find(marker_e)
    if start_pos == -1 or end_pos == -1 or end_pos <= start_pos:
        raise ValueError(f"Markers for region {region!r} not found in {readme_path}")

    before = content[:start_pos + len(marker_s)]
    after = content[end_pos:]

    body = "\n".join(tag_lines)
    new_content = before + "\n" + body + "\n" + after

    if new_content == content:
        return False

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


def main(
    output_file: str,
    *,
    region: str = DEFAULT_REGION,
) -> None:
    print(f"Fetching public repositories for org: {ORG}")
    repos = get_public_repos()
    print(f"  Found {len(repos)} public repositories")

    tag_lines = build_tag_lines(repos)
    print(f"  Keeping {len(tag_lines)} tags with count > {MIN_TAG_COUNT}")

    changed = update_readme(output_file, tag_lines, region=region)
    if changed:
        print(f"Updated {output_file}")
    else:
        print("No changes detected. Skipping update.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update Sorted By Tags in profile/README.md")
    parser.add_argument(
        "output_file", nargs="?", default=OUTPUT_FILE,
        help="Path to profile/README.md (default: %(default)s)",
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION,
        help="Region marker name (default: %(default)s)",
    )
    args = parser.parse_args()
    main(args.output_file, region=args.region)
