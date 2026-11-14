#!/usr/bin/env python3
"""Update VIPM download counts in the mermaid chart inside profile/README.md.

Region layout (everything between the ``VIPM_DOWNLOADS`` markers is owned by
this script)::

    xychart-beta chart with the **current** download counts
    <details> history table with the most recent N months

Logic:
- Fetch install counts from VIPM badge SVGs for the 6 CSM packages.
- Read the history table (or migrate legacy ``bar`` lines that carried the
  month as an extra value) to get previous monthly snapshots.
- If the newest snapshot already belongs to the current month, replace its
  counts; otherwise prepend a new month.
- Render the newest ``--visible-months`` snapshots (default 24) as a table;
  older snapshots stay in the file inside an HTML comment so no data is lost.
- Chart ``bar`` always has exactly one value per ``x-axis`` category; the
  current month is shown in the chart title, not as a data value.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from datetime import datetime

import requests

# ── 确保包根目录在 sys.path（直接运行 scripts/ 时使用）──────────────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts._utils import BEIJING_TZ, marker_start, marker_end  # noqa: E402

# ── Region marker ─────────────────────────────────────────────────────────────
DEFAULT_REGION = "VIPM_DOWNLOADS"

DEFAULT_VISIBLE_MONTHS = 24

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

CATEGORIES = ["Core", "API String", "MassData", "INI-Variable", "DAQ-Example", "TCP-Example"]

BADGE_URL = "https://www.vipm.io/package/{}/badge.svg?metric=installs"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

MONTH_RE = re.compile(r"^20\d\d\.\d\d$")
TABLE_ROW_RE = re.compile(r"^\|\s*(20\d\d\.\d\d)\s*\|(.+)\|\s*$")
LEGACY_BAR_RE = re.compile(r"bar\s+\[([^\]]*)\]")
MERMAID_RE = re.compile(r"(```mermaid\n)(.*?)(```)", re.DOTALL)


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


def parse_history(block: str) -> list[tuple[str, list[int]]]:
    """Return ``[(month, counts), ...]`` in README order (newest first).

    Reads the history table when present, otherwise migrates legacy ``bar``
    lines that appended the month as a seventh value.
    """
    history: list[tuple[str, list[int]]] = []
    for line in block.splitlines():
        row = TABLE_ROW_RE.match(line.strip())
        if not row:
            continue
        cells = [c.strip() for c in row.group(2).split("|")]
        counts: list[int] = []
        for cell in cells[: len(CATEGORIES)]:
            try:
                counts.append(int(cell.replace(",", "")))
            except ValueError:
                counts = []
                break
        if len(counts) == len(CATEGORIES):
            history.append((row.group(1), counts))
    if history:
        return history

    for values in LEGACY_BAR_RE.findall(block):
        items = [v.strip() for v in values.split(",") if v.strip()]
        if len(items) != len(CATEGORIES) + 1 or not MONTH_RE.match(items[-1]):
            continue
        history.append((items[-1], [int(float(v)) for v in items[:-1]]))
    return history


def _y_axis_max(counts: list[int]) -> int:
    """Round the y-axis ceiling up so the tallest bar keeps ~20% headroom."""
    return max(1_000, math.ceil(max(counts) * 1.2 / 1_000) * 1_000)


def render_region(
    history: list[tuple[str, list[int]]],
    *,
    current_month: str,
    visible_months: int,
) -> str:
    """Build the marker-region body for *history* (newest first).

    Only the newest ``visible_months`` snapshots are rendered as a table; older
    ones are kept verbatim inside an HTML comment so the data survives while the
    page stays short.
    """
    visible = history[:visible_months]
    hidden = history[visible_months:]
    current = dict(visible)[current_month]
    x_axis = ", ".join(f'"{c}"' for c in CATEGORIES)
    values = ", ".join(str(c) for c in current)

    lines = [
        "```mermaid",
        "    xychart-beta",
        f'    title "Communicable State Machine(CSM) Framework VIPM Download ({current_month})"',
        f"    x-axis [{x_axis}]",
        f'    y-axis "Download" 0 --> {_y_axis_max(current)}',
        f"    bar   [{values}]",
        "```",
        "",
        "<details>",
        f"<summary>最近 {len(visible)} 个月历史数据（VIPM 安装量）</summary>",
        "",
        "| 月份 | " + " | ".join(CATEGORIES) + " | 合计 |",
        "| --- | " + " | ".join("---:" for _ in CATEGORIES) + " | ---: |",
    ]
    for month, counts in visible:
        cells = " | ".join(str(c) for c in counts)
        lines.append(f"| {month} | {cells} | {sum(counts)} |")
    lines.append("")
    lines.append("</details>")

    if hidden:
        lines.append("")
        lines.append(
            f"<!-- 更早的 {len(hidden)} 个月快照（数据保留，默认不显示）："
            f"{hidden[0][0]} 到 {hidden[-1][0]}"
        )
        for month, counts in hidden:
            cells = " | ".join(str(c) for c in counts)
            lines.append(f"| {month} | {cells} | {sum(counts)} |")
        lines.append("-->")

    return "\n".join(lines)


def update_readme(
    readme_path: str,
    counts: list[int],
    *,
    region: str = DEFAULT_REGION,
    visible_months: int = DEFAULT_VISIBLE_MONTHS,
    now: datetime | None = None,
) -> str | None:
    """Refresh the chart and history table within *region* markers.

    Returns the current month string (e.g. ``'2026.11'``) when the file was
    updated, or ``None`` when the content was already up-to-date.
    """
    if len(counts) != len(CATEGORIES):
        raise ValueError(f"Expected {len(CATEGORIES)} counts, got {len(counts)}")
    if visible_months < 1:
        raise ValueError("visible_months must be >= 1")

    with open(readme_path, encoding="utf-8") as f:
        content = f.read()

    now = now or datetime.now(BEIJING_TZ)
    current_month = f"{now.year}.{now.month:02d}"

    marker_s = marker_start(region)
    marker_e = marker_end(region)

    start_pos = content.find(marker_s)
    end_pos = content.find(marker_e)
    if start_pos == -1 or end_pos == -1 or end_pos <= start_pos:
        raise ValueError(f"Markers for region {region!r} not found in {readme_path}")

    before = content[:start_pos]
    block = content[start_pos:end_pos + len(marker_e)]
    after = content[end_pos + len(marker_e):]

    if not MERMAID_RE.search(block):
        raise ValueError("Mermaid block not found within markers")

    history = parse_history(block)
    by_month = {month: values for month, values in history}
    by_month[current_month] = list(counts)

    ordered = sorted(by_month.items(), key=lambda item: item[0], reverse=True)
    new_region = render_region(
        ordered, current_month=current_month, visible_months=visible_months
    )

    new_block = marker_s + "\n" + new_region + "\n" + marker_e
    new_content = before + new_block + after

    if new_content == content:
        return None

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return current_month


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update VIPM download counts in profile/README.md")
    parser.add_argument(
        "readme_path", nargs="?", default="profile/README.md",
        help="Path to profile/README.md (default: %(default)s)",
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION,
        help="Region marker name (default: %(default)s)",
    )
    parser.add_argument(
        "--visible-months", type=int, default=DEFAULT_VISIBLE_MONTHS,
        help="How many monthly snapshots to show (default: %(default)s); older ones stay in an HTML comment",
    )
    args = parser.parse_args()

    print("Fetching VIPM download counts …")
    counts: list[int] = []
    for pkg in PACKAGES:
        count = get_install_count(pkg)
        print(f"  {pkg}: {count:,}")
        counts.append(count)

    print(f"\nUpdating {args.readme_path} …")
    month = update_readme(
        args.readme_path, counts, region=args.region, visible_months=args.visible_months
    )
    if month is None:
        print("No changes detected. Skipping update.")
    else:
        print(f"Done – updated data for {month}: {counts}")
