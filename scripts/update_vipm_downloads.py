#!/usr/bin/env python3
"""Update VIPM download counts in the mermaid chart inside profile/README.md.

Logic:
- Fetch install counts from VIPM badge SVGs for the 6 CSM packages.
- Find the xychart-beta mermaid block in the README.
- If the most-recent bar line already belongs to the current month (its last
  value is YYYY.MM matching today), replace it with the freshly-fetched counts.
- Otherwise insert a new bar line at the top for the current month.
- Auto-expand the y-axis ceiling when the maximum count reaches it.
"""

import re
import sys
import time

import requests
from datetime import datetime, timedelta, timezone

# ── Beijing timezone (UTC+8) ───────────────────────────────────────────────────
_BEIJING_TZ = timezone(timedelta(hours=8))

# ── Package list – order matches x-axis: Core, API String, MassData,
#    INI-Variable, DAQ-Example, TCP-Example ──────────────────────────────────
PACKAGES = [
    "nevstop_lib_communicable_state_machine",                    # Core
    "nevstop_lib_csm_api_string_arguments_support",              # API String
    "nevstop_lib_csm_massdata_parameter_support",                # MassData
    "nevstop_lib_csm_ini_static_variable_support",               # INI-Variable
    "nevstop_lib_csm_continuous_meausrement_and_logging_example",  # DAQ-Example (typo in official package name)
    "nevstop_lib_csm_tcp_router_example",                        # TCP-Example
]

BADGE_URL = "https://www.vipm.io/package/{}/badge.svg?metric=installs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


def _parse_count_text(text: str) -> int | None:
    """Try to parse a human-readable count string such as '5708', '5,708', '5.7k'."""
    text = text.strip()
    if not text:
        return None
    if text.lower().endswith("k"):
        try:
            return int(float(text[:-1]) * 1_000)
        except ValueError:
            return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def get_install_count(package_name: str, max_retries: int = 3) -> int:
    """Fetch the install count for *package_name* from the VIPM badge SVG."""
    url = BADGE_URL.format(package_name)
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            svg = resp.text

            # 1. Prefer numbers in <text> elements (shields.io / VIPM badge format)
            candidates: list[int] = []
            for raw in re.findall(r"<text[^>]*>\s*([^<]+?)\s*</text>", svg):
                count = _parse_count_text(raw)
                if count is not None and count > 0:
                    candidates.append(count)

            if candidates:
                return max(candidates)

            # 2. Fallback: any standalone 3+-digit integer in the SVG
            fallback = [int(n) for n in re.findall(r"(?<![.\d])(\d{3,})(?![.\d])", svg)]
            if fallback:
                return max(fallback)

            raise ValueError(f"Could not parse install count from badge SVG for {package_name}")

        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(2**attempt)

    raise RuntimeError(f"Failed to fetch count for {package_name}: {last_exc}") from last_exc


def update_readme(readme_path: str, counts: list[int]) -> str:
    """Update the mermaid chart in *readme_path* and return the month string."""
    with open(readme_path, encoding="utf-8") as f:
        content = f.read()

    now = datetime.now(_BEIJING_TZ)
    current_month = f"{now.year}.{now.month:02d}"

    # ── Locate the xychart-beta mermaid block ─────────────────────────────
    mermaid_re = re.compile(r"(```mermaid\n)(.*?)(```)", re.DOTALL)
    mermaid_match = mermaid_re.search(content)
    if not mermaid_match:
        raise ValueError("Mermaid block not found in README.md")

    chart_prefix = mermaid_match.group(1)   # ```mermaid\n
    chart_content = mermaid_match.group(2)  # body
    chart_suffix = mermaid_match.group(3)   # ```

    # ── Find the first (most-recent) bar line ─────────────────────────────
    bar_re = re.compile(r"( +bar +\[.*?\])")
    bar_match = bar_re.search(chart_content)
    if not bar_match:
        raise ValueError("No bar lines found in mermaid block")

    first_bar_line = bar_match.group(1)

    # Does the first bar line carry a month tag?  e.g. ", 2026.04]"
    month_in_bar = re.search(r",\s*(20\d\d\.\d\d)\]$", first_bar_line)

    # ── Build the new bar line ────────────────────────────────────────────
    values_str = ", ".join(str(c) for c in counts)
    new_bar_line = f"    bar   [{values_str}, {current_month}]"

    if month_in_bar and month_in_bar.group(1) == current_month:
        # Same month → replace in place
        new_chart_content = chart_content.replace(first_bar_line, new_bar_line, 1)
    else:
        # New month → prepend before the first existing bar line
        insert_pos = chart_content.index(first_bar_line)
        new_chart_content = (
            chart_content[:insert_pos]
            + new_bar_line
            + "\n"
            + chart_content[insert_pos:]
        )

    # ── Expand y-axis ceiling if needed ──────────────────────────────────
    max_count = max(counts)
    y_axis_match = re.search(r'(y-axis "Download" 0 --> )(\d+)', new_chart_content)
    if y_axis_match:
        current_y_max = int(y_axis_match.group(2))
        if max_count >= current_y_max:
            new_y_max = int(((max_count * 1.3) // 1_000 + 1) * 1_000)
            new_chart_content = new_chart_content.replace(
                y_axis_match.group(0),
                f'{y_axis_match.group(1)}{new_y_max}',
            )

    new_content = (
        content[: mermaid_match.start()]
        + chart_prefix
        + new_chart_content
        + chart_suffix
        + content[mermaid_match.end():]
    )

    if new_content == content:
        return None

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return current_month


if __name__ == "__main__":
    readme_path = sys.argv[1] if len(sys.argv) > 1 else "profile/README.md"

    print("Fetching VIPM download counts …")
    counts: list[int] = []
    for pkg in PACKAGES:
        count = get_install_count(pkg)
        print(f"  {pkg}: {count:,}")
        counts.append(count)

    print(f"\nUpdating {readme_path} …")
    month = update_readme(readme_path, counts)
    if month is None:
        print("No changes detected. Skipping update.")
    else:
        print(f"Done – updated data for {month}: {counts}")
