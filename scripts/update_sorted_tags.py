#!/usr/bin/env python3
"""Update the "Sorted By Tags" section in profile/README.md.

Logic:
- Fetch all public repositories in the organization.
- Count topic/tag usage across repositories.
- Keep tags whose count is greater than MIN_TAG_COUNT (default 1).
- Sort tags by count descending, then by name.
- Render each line as: [`tag(count)`](search-url)
"""

from collections import Counter
import os
import re
import sys
import time
from urllib.parse import quote

import requests


ORG = os.environ.get("ORG", "NEVSTOP-LAB")
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "profile/README.md")
MIN_TAG_COUNT = int(os.environ.get("MIN_TAG_COUNT", "1"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
if GITHUB_TOKEN:
    _HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


def _paginate(url: str, extra_params: dict | None = None):
    params = {"per_page": 100, **(extra_params or {})}
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return
        yield from data
        if len(data) < 100:
            return
        page += 1
        time.sleep(0.05)


def get_public_repos() -> list[dict]:
    url = f"https://api.github.com/orgs/{ORG}/repos"
    return list(_paginate(url, {"type": "public"}))


def build_tag_lines(repos: list[dict]) -> list[str]:
    counts: Counter[str] = Counter()
    for repo in repos:
        for topic in repo.get("topics", []):
            if topic:
                counts[topic] += 1

    filtered = [(tag, count) for tag, count in counts.items() if count > MIN_TAG_COUNT]
    filtered.sort(key=lambda item: (-item[1], item[0].lower()))

    lines: list[str] = []
    for tag, count in filtered:
        query = quote(f"topic:{tag} org:{ORG} is:public", safe="")
        url = f"https://github.com/search?q={query}&type=Repositories"
        lines.append(f"[`{tag}({count})`]({url})")
    return lines


def update_readme(readme_path: str, tag_lines: list[str]) -> bool:
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()

    section_re = re.compile(
        r"(👩‍💻 \*\*Sorted By Tags\*\*\n-+\n)(.*?)(\n<!--)",
        re.DOTALL,
    )
    match = section_re.search(content)
    if not match:
        raise ValueError('Could not find "Sorted By Tags" section in README')

    body = "\n".join(tag_lines)
    if body:
        body += "\n"

    new_content = (
        content[: match.start(2)]
        + body
        + content[match.end(2):]
    )

    if new_content == content:
        return False

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


def main(output_file: str):
    print(f"Fetching public repositories for org: {ORG}")
    repos = get_public_repos()
    print(f"  Found {len(repos)} public repositories")

    tag_lines = build_tag_lines(repos)
    print(
        f"  Keeping {len(tag_lines)} tags with count > {MIN_TAG_COUNT}"
    )

    changed = update_readme(output_file, tag_lines)
    if changed:
        print(f"Updated {output_file}")
    else:
        print("No changes detected. Skipping update.")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else OUTPUT_FILE
    main(out)
