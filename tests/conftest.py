"""pytest 共享 fixtures。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture
def tmp_dir():
    """提供一次性临时目录。"""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)
