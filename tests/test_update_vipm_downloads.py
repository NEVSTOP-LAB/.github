"""update_vipm_downloads.py 的单元测试。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts._utils import BEIJING_TZ, marker_end, marker_start  # noqa: E402
from scripts.update_vipm_downloads import (  # noqa: E402
    CATEGORIES,
    DEFAULT_VISIBLE_MONTHS,
    parse_history,
    render_region,
    update_readme,
)

REGION = "VIPM_DOWNLOADS"


def _legacy_block(rows: list[tuple[str, list[int]]]) -> str:
    """构造旧格式区域：每条 bar 把月份作为第 7 个值。"""
    bar_lines = "\n".join(
        f"    bar   [{', '.join(str(c) for c in counts)}, {month}]" for month, counts in rows
    )
    return "\n".join(
        [
            marker_start(REGION),
            "```mermaid",
            "    xychart-beta",
            '    title "Communicable State Machine(CSM) Framework VIPM Download"',
            f"    x-axis [{', '.join(CATEGORIES)}]",
            '    y-axis "Download" 0 --> 11000',
            bar_lines,
            "```",
            marker_end(REGION),
        ]
    )


def _readme(tmp_dir: Path, region_body: str) -> Path:
    path = tmp_dir / "README.md"
    path.write_text(f"# head\n\n{region_body}\n\nfooter\n", encoding="utf-8")
    return path


def _counts(*values: int) -> list[int]:
    return list(values)


def _months_back(newest: tuple[int, int], count: int) -> list[str]:
    """从 newest=(year, month) 往回取 count 个月，返回新到旧的 YYYY.MM 列表。"""
    year, month = newest
    months: list[str] = []
    for _ in range(count):
        months.append(f"{year}.{month:02d}")
        month -= 1
        if month == 0:
            month, year = 12, year - 1
    return months


def test_migrates_legacy_bar_lines(tmp_dir):
    legacy = _legacy_block(
        [
            ("2026.11", [8646, 6574, 5768, 6191, 5350, 3597]),
            ("2026.10", [8442, 6436, 5667, 6074, 5239, 3524]),
            ("2026.09", [8080, 6134, 5449, 5787, 4979, 3377]),
        ]
    )
    path = _readme(tmp_dir, legacy)

    month = update_readme(
        str(path),
        _counts(8700, 6600, 5800, 6200, 5400, 3600),
        now=datetime(2026, 11, 14, tzinfo=BEIJING_TZ),
    )

    text = path.read_text(encoding="utf-8")
    assert month == "2026.11"
    assert "2026.11 | 8700 | 6600 | 5800 | 6200 | 5400 | 3600" in text
    assert "2026.10 | 8442" in text and "2026.09 | 8080" in text
    assert "bar   [8700, 6600, 5800, 6200, 5400, 3600]" in text
    # 旧格式的月份不再残留在 bar 里
    assert "3597, 2026.11]" not in text
    assert text.count("bar   [") == 1


def test_bar_values_match_x_axis_categories(tmp_dir):
    path = _readme(tmp_dir, _legacy_block([("2026.11", [1, 2, 3, 4, 5, 6])]))

    update_readme(str(path), _counts(11, 22, 33, 44, 55, 66), now=datetime(2026, 11, 1, tzinfo=BEIJING_TZ))
    text = path.read_text(encoding="utf-8")

    bar = [line for line in text.splitlines() if line.strip().startswith("bar")][0]
    values = bar.split("[")[1].split("]")[0].split(",")
    assert len(values) == len(CATEGORIES)


def test_same_month_updates_in_place_and_is_idempotent(tmp_dir):
    path = _readme(tmp_dir, _legacy_block([("2026.11", [1, 2, 3, 4, 5, 6])]))
    now = datetime(2026, 11, 14, tzinfo=BEIJING_TZ)

    assert update_readme(str(path), _counts(11, 22, 33, 44, 55, 66), now=now) == "2026.11"
    first = path.read_text(encoding="utf-8")
    assert "2026.11" in first

    # 同月、同样数据 → 无变化
    assert update_readme(str(path), _counts(11, 22, 33, 44, 55, 66), now=now) is None
    assert path.read_text(encoding="utf-8") == first

    # 同月、数据变化 → 覆盖该月快照，不新增行
    update_readme(str(path), _counts(12, 23, 34, 45, 56, 67), now=now)
    text = path.read_text(encoding="utf-8")
    assert text.count("| 2026.11 |") == 1
    assert "| 2026.11 | 12 | 23 | 34 | 45 | 56 | 67 |" in text


def test_new_month_is_prepended_and_old_months_are_kept_commented(tmp_dir):
    # 已有 24 个月（2026.11 往回），再来 2027.01 后显示 24 个月，最旧的 2024.12 进注释
    rows = [(month, [1] * 6) for month in _months_back((2026, 11), 24)]

    path = _readme(tmp_dir, _legacy_block(rows))
    now = datetime(2027, 1, 5, tzinfo=BEIJING_TZ)
    update_readme(str(path), _counts(1, 2, 3, 4, 5, 6), now=now)

    text = path.read_text(encoding="utf-8")
    history = parse_history(text)
    assert len(history) == 25  # 数据全保留
    assert history[0][0] == "2027.01"
    assert history[-1][0] == "2024.12"

    start = text.index("<!-- 更早的")
    comment = text[start : text.index("-->", start)]
    assert "| 2024.12 |" in comment            # 超出的月份被注释
    assert "最近 24 个月历史数据" in text      # 只显示 24 个月

    # 同月重跑不产生变化
    assert update_readme(str(path), _counts(1, 2, 3, 4, 5, 6), now=now) is None


def test_y_axis_has_headroom_above_max_count(tmp_dir):
    path = _readme(tmp_dir, _legacy_block([("2026.11", [1, 1, 1, 1, 1, 1])]))
    update_readme(str(path), _counts(10_500, 0, 0, 0, 0, 0), now=datetime(2026, 11, 2, tzinfo=BEIJING_TZ))

    text = path.read_text(encoding="utf-8")
    y_axis = [line for line in text.splitlines() if "y-axis" in line][0]
    ceiling = int(y_axis.split("-->")[1].strip())
    assert ceiling >= 10_500
    assert ceiling % 1_000 == 0


def test_parse_history_prefers_table_over_legacy_bars():
    block = "\n".join(
        [
            marker_start(REGION),
            "    bar   [1, 2, 3, 4, 5, 6, 2026.01]",
            "| 月份 | Core | API String | MassData | INI-Variable | DAQ-Example | TCP-Example | 合计 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
            "| 2026.11 | 10 | 20 | 30 | 40 | 50 | 60 | 210 |",
            marker_end(REGION),
        ]
    )
    assert parse_history(block) == [("2026.11", [10, 20, 30, 40, 50, 60])]


def test_render_region_hides_overflow_but_keeps_it_parseable():
    history = [(month, [1] * 6) for month in _months_back((2026, 12), 24)]
    region = render_region(history, current_month="2026.12", visible_months=12)

    assert "最近 12 个月历史数据" in region
    visible_rows = region[: region.index("<!--")].count("| 2026.")
    assert visible_rows == 12
    assert "<!-- 更早的 12 个月快照" in region
    assert len(parse_history(region)) == 24


def test_missing_markers_raise(tmp_dir):
    path = tmp_dir / "README.md"
    path.write_text("no markers here\n", encoding="utf-8")
    try:
        update_readme(str(path), _counts(1, 2, 3, 4, 5, 6), now=datetime(2026, 11, 1, tzinfo=BEIJING_TZ))
    except ValueError as exc:
        assert "not found" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
