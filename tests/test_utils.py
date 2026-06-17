"""Tests for scripts._utils shared utilities."""

from __future__ import annotations

import pytest

from scripts._utils import marker_start, marker_end


class TestMarkerHelpers:
    """Tests for marker_start / marker_end region marker builders."""

    def test_marker_start_basic(self) -> None:
        assert marker_start("VIPM_DOWNLOADS") == "<!-- VIPM_DOWNLOADS_START -->"

    def test_marker_end_basic(self) -> None:
        assert marker_end("VIPM_DOWNLOADS") == "<!-- VIPM_DOWNLOADS_END -->"

    def test_marker_start_custom_region(self) -> None:
        assert marker_start("MY_REGION") == "<!-- MY_REGION_START -->"

    def test_marker_end_custom_region(self) -> None:
        assert marker_end("MY_REGION") == "<!-- MY_REGION_END -->"

    def test_marker_start_empty_region(self) -> None:
        assert marker_start("") == "<!-- _START -->"

    def test_marker_end_empty_region(self) -> None:
        assert marker_end("") == "<!-- _END -->"

    def test_marker_start_with_spaces(self) -> None:
        assert marker_start("A B") == "<!-- A B_START -->"

    def test_marker_pair_consistency(self) -> None:
        """Start and end markers should only differ by _START vs _END suffix."""
        for region in ("VIPM_DOWNLOADS", "SORTED_TAGS", "CSM_MODSETS", "X"):
            s = marker_start(region)
            e = marker_end(region)
            assert s.replace("_START", "_END") == e
            assert s.startswith("<!-- ")
            assert s.endswith(" -->")
            assert e.startswith("<!-- ")
            assert e.endswith(" -->")

    def test_marker_start_default_regions_match_readme(self) -> None:
        """All three default regions should produce markers that exist in README."""
        import os

        readme_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "profile", "README.md",
        )
        with open(readme_path, encoding="utf-8") as f:
            content = f.read()

        for region in ("VIPM_DOWNLOADS", "SORTED_TAGS", "CSM_MODSETS"):
            assert marker_start(region) in content, (
                f"marker_start({region!r}) not found in README"
            )
            assert marker_end(region) in content, (
                f"marker_end({region!r}) not found in README"
            )
