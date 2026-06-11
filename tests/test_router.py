"""tests/test_router.py — router.py 单元测试."""

from __future__ import annotations

import pytest
from unittest.mock import patch, MagicMock

import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.router import (
    BOT_MARKER,
    classify_intent,
    _fallback_classify,
    build_condition_report,
    _is_org_member,
    JOIN_FOLLOW_ORG,
    JOIN_STAR_REPOS,
)


# ── classify_intent 降级正则 ─────────────────────────────────────────────────


class TestFallbackClassify:
    """测试降级正则匹配逻辑。"""

    def test_join_keywords(self):
        assert _fallback_classify("我想加入组织") == "JOIN"
        assert _fallback_classify("申请加入") == "JOIN"
        assert _fallback_classify("我想成为成员") == "JOIN"
        assert _fallback_classify("I want to join") == "JOIN"
        assert _fallback_classify("如何参与贡献") == "JOIN"

    def test_qa_keywords(self):
        assert _fallback_classify("怎么配置 CSM？") == "QA"
        assert _fallback_classify("这个框架是什么") == "QA"
        assert _fallback_classify("报错了怎么办") == "QA"
        assert _fallback_classify("How to use?") == "QA"
        assert _fallback_classify("请教一个问题") == "QA"
        assert _fallback_classify("求助！安装出错") == "QA"
        assert _fallback_classify("LabVIEW 怎么 join 数组") == "QA"
        assert _fallback_classify("出现了一个 BUG") == "QA"
        assert _fallback_classify("Error happened") == "QA"

    def test_other_fallback(self):
        assert _fallback_classify("hello") == "OTHER"
        assert _fallback_classify("谢谢") == "OTHER"
        assert _fallback_classify("") == "OTHER"

    def test_join_before_qa(self):
        """JOIN 关键词优先于 QA 关键词。"""
        assert _fallback_classify("想加入 怎么用") == "JOIN"


class TestClassifyIntent:
    """测试完整的 classify_intent（含 LLM 调用路径与降级路径）。"""

    def test_empty_body(self):
        assert classify_intent("") == "OTHER"
        assert classify_intent("   ") == "OTHER"

    def test_no_api_key_falls_back(self, monkeypatch):
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "")
        assert classify_intent("想加入") == "JOIN"
        assert classify_intent("怎么用？") == "QA"
        assert classify_intent("hello") == "OTHER"


# ── build_condition_report ───────────────────────────────────────────────────


class TestBuildConditionReport:
    def test_all_passed(self):
        results = [
            {"name": "关注 @NEVSTOP-LAB", "icon": "👀", "passed": True, "detail": "已关注"},
            {"name": "Star 指定仓库", "icon": "⭐", "passed": True, "detail": "已 Star 全部"},
        ]
        report = build_condition_report("testuser", True, results)
        assert "## 📋 @testuser" in report
        assert "全部通过 (2/2)" in report
        assert "正在发送邀请" in report
        assert "✅" in report
        assert "❌" not in report

    def test_partial_passed(self):
        results = [
            {"name": "关注 @NEVSTOP-LAB", "icon": "👀", "passed": True, "detail": "已关注"},
            {"name": "Star 指定仓库", "icon": "⭐", "passed": False, "detail": "缺少：CSM-API-String-Arguments-Support, CSM-INI-Static-Variable-Support"},
        ]
        report = build_condition_report("testuser", False, results)
        assert "当前 1/2 项通过" in report
        assert "请再次发送申请" in report
        assert "✅" in report
        assert "❌" in report

    def test_none_passed(self):
        results = [
            {"name": "关注 @NEVSTOP-LAB", "icon": "👀", "passed": False, "detail": "未关注 @NEVSTOP-LAB"},
            {"name": "Star 指定仓库", "icon": "⭐", "passed": False, "detail": "缺少：Communicable-State-Machine, CSM-API-String-Arguments-Support, CSM-MassData-Parameter-Support, CSM-INI-Static-Variable-Support"},
        ]
        report = build_condition_report("testuser", False, results)
        assert "当前 0/2 项通过" in report
        assert "请再次发送申请" in report

    def test_star_repo_list_in_report(self):
        results = [
            {"name": "关注 @NEVSTOP-LAB", "icon": "👀", "passed": True, "detail": "已关注"},
            {"name": "Star 指定仓库", "icon": "⭐", "passed": True, "detail": "已 Star 全部"},
        ]
        report = build_condition_report("testuser", True, results)
        assert "需 Star 的仓库" in report
        for repo in JOIN_STAR_REPOS:
            assert repo in report


# ── check_all_conditions ─────────────────────────────────────────────────────


class TestCheckAllConditions:
    def test_all_pass(self, monkeypatch):
        """Mock 关注通过 + Star 列表包含全部 4 个仓库。"""
        import urllib.error

        def mock_rest(token, method, path):
            m = MagicMock()
            if "/starred?" in path:
                # 返回全部 4 个仓库的 Star 列表
                m.read.return_value = json.dumps([
                    {"full_name": f"NEVSTOP-LAB/{r}"} for r in JOIN_STAR_REPOS
                ]).encode()
                return m
            m.status = 204
            return m

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import check_all_conditions

        all_met, results = check_all_conditions("fake-token", "testuser")
        assert all_met is True
        assert len(results) == 2
        for r in results:
            assert r["passed"] is True

    def test_follow_fail(self, monkeypatch):
        """Mock 关注返回 404，Star 全部在列表中。"""
        import urllib.error

        def mock_rest(token, method, path):
            m = MagicMock()
            if "/following/" in path:
                raise urllib.error.HTTPError(
                    url=path, code=404, msg="Not Found", hdrs={}, fp=None
                )
            if "/starred?" in path:
                m.read.return_value = json.dumps([
                    {"full_name": f"NEVSTOP-LAB/{r}"} for r in JOIN_STAR_REPOS
                ]).encode()
                return m
            m.status = 204
            return m

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import check_all_conditions

        all_met, results = check_all_conditions("fake-token", "testuser")
        assert all_met is False
        assert results[0]["passed"] is False  # 关注
        assert results[1]["passed"] is True    # Star

    def test_star_partial_fail(self, monkeypatch):
        """Mock 关注通过，Star 列表仅含 1 个仓库。"""
        import urllib.error

        def mock_rest(token, method, path):
            m = MagicMock()
            if "/following/" in path:
                m.status = 204
                return m
            if "/starred?" in path:
                # 只返回第一个仓库
                m.read.return_value = json.dumps([
                    {"full_name": f"NEVSTOP-LAB/{JOIN_STAR_REPOS[0]}"}
                ]).encode()
                return m
            m.status = 204
            return m

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import check_all_conditions

        all_met, results = check_all_conditions("fake-token", "testuser")
        assert all_met is False
        assert results[0]["passed"] is True   # 关注
        assert results[1]["passed"] is False  # Star
        assert "缺少" in results[1]["detail"]


# ── _resolve_user_id ─────────────────────────────────────────────────────────


class TestResolveUserId:
    def test_resolve_success(self, monkeypatch):
        def mock_rest(token, method, path):
            m = MagicMock()
            m.read.return_value = b'{"id": 12345, "login": "testuser"}'
            return m

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _resolve_user_id

        user_id = _resolve_user_id("fake-token", "testuser")
        assert user_id == 12345

    def test_resolve_no_id_field(self, monkeypatch):
        def mock_rest(token, method, path):
            m = MagicMock()
            m.read.return_value = b'{"login": "testuser"}'
            return m

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _resolve_user_id

        with pytest.raises(RuntimeError, match="未返回 id 字段"):
            _resolve_user_id("fake-token", "testuser")

    def test_resolve_http_error(self, monkeypatch):
        import urllib.error

        def mock_rest(token, method, path):
            raise urllib.error.HTTPError(
                url=path, code=404, msg="Not Found", hdrs={}, fp=None
            )

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _resolve_user_id

        with pytest.raises(RuntimeError, match="404"):
            _resolve_user_id("fake-token", "nonexistent")


# ── 常量检查 ─────────────────────────────────────────────────────────────────


def test_bot_marker_is_csm_qa_bot():
    """确保 BOT_MARKER 复用现有 csm-qa-bot 标记。"""
    assert BOT_MARKER == "<!-- csm-qa-bot -->"


def test_join_defaults():
    """确保 JOIN 默认值与 plan 一致。"""
    assert JOIN_FOLLOW_ORG == os.getenv("JOIN_FOLLOW_ORG", "NEVSTOP-LAB")
    assert len(JOIN_STAR_REPOS) >= 4
    assert "Communicable-State-Machine" in JOIN_STAR_REPOS


# ── _is_org_member ────────────────────────────────────────────────────────────


class TestIsOrgMember:
    def test_is_member(self, monkeypatch):
        """204 = 成员。"""

        def mock_rest(token, method, path):
            m = MagicMock()
            m.status = 204
            return m

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _is_org_member

        assert _is_org_member("fake-token", "NEVSTOP-LAB", "testuser") is True

    def test_not_member(self, monkeypatch):
        """404 = 非成员。"""
        import urllib.error

        def mock_rest(token, method, path):
            raise urllib.error.HTTPError(
                url=path, code=404, msg="Not Found", hdrs={}, fp=None
            )

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _is_org_member

        assert _is_org_member("fake-token", "NEVSTOP-LAB", "testuser") is False
