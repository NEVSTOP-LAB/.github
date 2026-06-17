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
    JOIN_DEFAULT_TEAM,
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
        # 全部通过时仍包含团队分组信息
        assert "CSM-Community" in report
        assert "CSM-Module-Author" in report
        assert "CSM-Developer" in report
        # 新增温馨提示
        assert "温馨提示" in report
        assert "项目任务看板" in report
        assert "csm-committee" in report
        assert "团队分组权限不会主动提升" in report

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
        # 未全部通过时不应出现团队分组信息
        assert "CSM-Community" not in report

    def test_none_passed(self):
        results = [
            {"name": "关注 @NEVSTOP-LAB", "icon": "👀", "passed": False, "detail": "未关注 @NEVSTOP-LAB"},
            {"name": "Star 指定仓库", "icon": "⭐", "passed": False, "detail": "缺少：Communicable-State-Machine, CSM-API-String-Arguments-Support, CSM-MassData-Parameter-Support, CSM-INI-Static-Variable-Support"},
        ]
        report = build_condition_report("testuser", False, results)
        assert "当前 0/2 项通过" in report
        assert "请再次发送申请" in report
        assert "CSM-Community" not in report

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
    assert JOIN_DEFAULT_TEAM in ("csm-community", os.getenv("JOIN_DEFAULT_TEAM", "csm-community"))


# ── _add_team_membership ──────────────────────────────────────────────────────


class TestAddTeamMembership:
    def test_add_success(self, monkeypatch):
        """PUT 返回 200 → True。"""

        def mock_rest(token, method, path):
            m = MagicMock()
            m.status = 200
            return m

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _add_team_membership

        assert _add_team_membership("fake-token", "NEVSTOP-LAB", "csm-community", "testuser") is True

    def test_already_member(self, monkeypatch):
        """409 已成员 → 视作成功。"""
        import urllib.error

        def mock_rest(token, method, path):
            raise urllib.error.HTTPError(
                url=path, code=409, msg="Conflict", hdrs={}, fp=None
            )

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _add_team_membership

        assert _add_team_membership("fake-token", "NEVSTOP-LAB", "csm-community", "testuser") is True

    def test_fail(self, monkeypatch):
        """403 → False。"""
        import urllib.error

        def mock_rest(token, method, path):
            raise urllib.error.HTTPError(
                url=path, code=403, msg="Forbidden", hdrs={}, fp=None
            )

        monkeypatch.setattr("scripts.router._rest_req", mock_rest)

        from scripts.router import _add_team_membership

        assert _add_team_membership("fake-token", "NEVSTOP-LAB", "csm-community", "testuser") is False


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


# ── classify_intent 带上下文 ─────────────────────────────────────────────────


class TestBuildHistoryText:
    """测试 _build_history_text 格式化。"""

    def test_basic_formatting(self):
        from scripts.router import _build_history_text
        history = [
            {"role": "user", "content": "我想加入组织"},
            {"role": "assistant", "content": "已收到你的申请"},
            {"role": "user", "content": "请再检查"},
        ]
        result = _build_history_text(history)
        assert "[用户]: 我想加入组织" in result
        assert "[Bot]: 已收到你的申请" in result
        assert "[用户]: 请再检查" in result

    def test_empty_history(self):
        from scripts.router import _build_history_text
        result = _build_history_text([])
        assert result == ""

    def test_empty_content_skipped(self):
        from scripts.router import _build_history_text
        history = [
            {"role": "user", "content": "有效消息"},
            {"role": "bot", "content": ""},
            {"role": "user", "content": "另一条"},
        ]
        result = _build_history_text(history)
        assert "有效消息" in result
        assert "另一条" in result
        # 空内容不应出现 Bot 标签
        assert result.count("[Bot]") == 0

    def test_long_content_truncated(self):
        from scripts.router import _build_history_text
        long_text = "A" * 600
        history = [{"role": "user", "content": long_text}]
        result = _build_history_text(history)
        assert len(result) < len(long_text) + 20  # 标签 + 截断后内容

    def test_max_entries_cap(self):
        """超过 20 条消息时，保留首条 + 最近 19 条。"""
        from scripts.router import _build_history_text
        history = [{"role": "user", "content": f"msg_{i}"} for i in range(30)]
        result = _build_history_text(history)
        # 应包含首条
        assert "msg_0" in result
        # 应包含最后一条
        assert "msg_29" in result
        # 第 2-10 条应被截掉（只保留首条 + 最近 19 条）
        assert "msg_5" not in result

    def test_total_chars_cap(self):
        """总字符超过 8000 时应截断并添加省略提示。"""
        from scripts.router import _build_history_text
        # 每条 500 字符，20 条 = 10000 字符，超过 8000 上限
        history = [
            {"role": "user", "content": "X" * 600} for _ in range(20)
        ]
        result = _build_history_text(history)
        assert len(result) <= 8100  # 8000 + 省略提示的余量
        # 应有省略标记
        assert "上文已省略" in result

    def test_unknown_role_maps_to_user(self):
        """未知角色应映射为"用户"而非"Bot"（防御性处理）。"""
        from scripts.router import _build_history_text
        history = [{"role": "system", "content": "系统消息"}]
        result = _build_history_text(history)
        assert "[用户]: 系统消息" in result
        assert "[Bot]" not in result


class TestClassifyIntentWithHistory:
    """测试 classify_intent 带 history 参数的上下文分类路径。"""

    def test_history_with_no_api_key_falls_back(self, monkeypatch):
        """无 API Key 时即使有 history 也走降级正则。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "")
        history = [
            {"role": "user", "content": "CSM 怎么安装？"},
            {"role": "assistant", "content": "请参考文档..."},
        ]
        # "请再检查" 无关键词 → 降级为 OTHER（历史不影响正则）
        assert classify_intent("请再检查", history=history) == "OTHER"

    def test_history_passed_to_llm_prompt(self, monkeypatch):
        """有 API Key + history 时，使用带上下文的 prompt 调用 LLM。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "fake-key")

        # Mock urllib.request.urlopen 返回 QA
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "QA"}}]
        }).encode()
        # 使用 __enter__ 模拟 context manager
        mock_resp.__enter__.return_value = mock_resp

        called_payload = {}

        def mock_urlopen(req, **kwargs):
            nonlocal called_payload
            called_payload = json.loads(req.data)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        history = [
            {"role": "user", "content": "CSM 怎么安装？"},
            {"role": "assistant", "content": "请参考文档..."},
        ]
        result = classify_intent("请再检查", history=history)
        assert result == "QA"

        # 验证 prompt 包含了上下文
        prompt_content = called_payload["messages"][0]["content"]
        assert "CSM 怎么安装？" in prompt_content
        assert "请再检查" in prompt_content
        assert "请仅根据最后这条评论" in prompt_content

    def test_no_history_uses_original_prompt(self, monkeypatch):
        """无 history 时仍使用原始 prompt（向后兼容）。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "fake-key")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "JOIN"}}]
        }).encode()
        mock_resp.__enter__.return_value = mock_resp

        called_payload = {}

        def mock_urlopen(req, **kwargs):
            nonlocal called_payload
            called_payload = json.loads(req.data)
            return mock_resp

        monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

        result = classify_intent("我想加入组织")
        assert result == "JOIN"

        prompt_content = called_payload["messages"][0]["content"]
        # 原始 prompt 不包含"请仅根据最后这条评论"
        assert "请仅根据最后这条评论" not in prompt_content
        assert "我想加入组织" in prompt_content


class TestBuildClassifyHistory:
    """测试 _build_classify_history 从 Discussion 构建上下文。"""

    def test_basic_thread(self, monkeypatch):
        """基本 thread 构建：标题 + 正文 + 评论。"""
        # Mock _get_source_repo_parts
        monkeypatch.setattr(
            "scripts.router._get_source_repo_parts",
            lambda: ("test-org", ".github"),
        )
        # Mock GQL and fetch_discussion
        mock_discussion = {
            "title": "CSM 安装问题",
            "body": "我在安装时遇到错误",
            "comments": {
                "nodes": [
                    {"body": "请提供错误日志", "author": {"login": "bot"}},
                    {"body": "错误日志如下：..."},  # no BOT_MARKER → user
                    {"body": "请再检查"},  # current classify_input
                ]
            },
        }
        monkeypatch.setattr(
            "scripts.router.fetch_discussion",
            lambda *args, **kwargs: mock_discussion,
        )

        from scripts.router import _build_classify_history

        history = _build_classify_history("fake-token", 1, "请再检查")
        assert history is not None
        assert len(history) >= 3  # 至少：原帖 + 两条评论（跳过"请再检查"）
        # "请再检查" 不应在 history 中
        for entry in history:
            assert entry["content"] != "请再检查"

    def test_no_comments(self, monkeypatch):
        """新 discussion 无评论时，history 只含标题+正文。"""
        monkeypatch.setattr(
            "scripts.router._get_source_repo_parts",
            lambda: ("test-org", ".github"),
        )
        mock_discussion = {
            "title": "我想加入",
            "body": "申请加入组织",
            "comments": {"nodes": []},
        }
        monkeypatch.setattr(
            "scripts.router.fetch_discussion",
            lambda *args, **kwargs: mock_discussion,
        )

        from scripts.router import _build_classify_history

        history = _build_classify_history("fake-token", 1, "我想加入\n\n申请加入组织")
        assert history is not None
        assert len(history) == 1
        assert history[0]["role"] == "user"
        assert "我想加入" in history[0]["content"]

    def test_bot_marker_detection(self, monkeypatch):
        """通过 BOT_MARKER 识别 Bot 评论，设为 assistant 角色。"""
        from scripts.router import BOT_MARKER as marker

        monkeypatch.setattr(
            "scripts.router._get_source_repo_parts",
            lambda: ("test-org", ".github"),
        )
        mock_discussion = {
            "title": "测试",
            "body": "问题描述",
            "comments": {
                "nodes": [
                    {"body": f"自动回复内容\n{marker}"},
                    {"body": "谢谢"},  # current classify_input
                ]
            },
        }
        monkeypatch.setattr(
            "scripts.router.fetch_discussion",
            lambda *args, **kwargs: mock_discussion,
        )

        from scripts.router import _build_classify_history

        history = _build_classify_history("fake-token", 1, "谢谢")
        assert history is not None
        # Bot 评论应为 assistant 角色
        bot_entry = next(
            (e for e in history if "自动回复内容" in e["content"]), None
        )
        assert bot_entry is not None
        assert bot_entry["role"] == "assistant"

    def test_paginated_comments(self, monkeypatch):
        """fetch_discussion 返回多页时正确拼接所有评论。"""
        monkeypatch.setattr(
            "scripts.router._get_source_repo_parts",
            lambda: ("test-org", ".github"),
        )
        # 第一页 hasNextPage=True，第二页 hasNextPage=False
        mock_discussion = {
            "title": "测试分页",
            "body": "多页评论",
            "id": "D_abc",
            "comments": {
                "nodes": [
                    {"body": "评论1"},
                    {"body": "评论2"},
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "cursor_1"},
            },
        }

        fetch_calls = []

        def mock_fetch(client, owner, repo, number):
            fetch_calls.append(1)
            return mock_discussion

        monkeypatch.setattr("scripts.router.fetch_discussion", mock_fetch)

        from scripts.router import _build_classify_history

        history = _build_classify_history("fake-token", 1, "not_in_thread")
        assert history is not None
        # 即使有分页，第一页的评论也应在 history 中
        assert any("评论1" in e["content"] for e in history)


# ── classify_intent 花括号转义 ───────────────────────────────────────────────


class TestClassifyIntentBracesEscaping:
    """测试用户内容中的 { } 不会导致 str.format() 崩溃。"""

    def test_braces_in_comment_body(self, monkeypatch):
        """评论含花括号时不崩溃，正常分类。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "fake-key")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "QA"}}]
        }).encode()
        mock_resp.__enter__.return_value = mock_resp

        monkeypatch.setattr("urllib.request.urlopen", lambda req, **kw: mock_resp)

        # 用户评论含 JSON 花括号 — 不应抛 KeyError
        result = classify_intent("CSM 的配置 { \"key\": \"value\" } 怎么用？")
        assert result == "QA"

    def test_braces_in_history(self, monkeypatch):
        """history 含花括号时不崩溃，正常分类。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "fake-key")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "QA"}}]
        }).encode()
        mock_resp.__enter__.return_value = mock_resp

        monkeypatch.setattr("urllib.request.urlopen", lambda req, **kw: mock_resp)

        history = [
            {"role": "user", "content": "代码里用 {placeholder} 怎么写？"},
            {"role": "assistant", "content": "用 {{}} 转义"},
        ]
        result = classify_intent("请再检查", history=history)
        assert result == "QA"

    def test_format_call_inside_try_block(self, monkeypatch):
        """format() 在 try 块内 — 即使崩溃也降级正则而非 propagate。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "fake-key")
        # 不 mock urlopen — 即使 URL 请求也失败，format 不应先崩溃
        # 这里重点验证 classify_intent 不抛异常，返回降级结果
        # 内容含多重花括号
        result = classify_intent("测试 {{{}}} 和 {{foo}}")
        # 只要不抛异常且返回合法标签即可
        assert result in ("JOIN", "QA", "OTHER")


# ── main() classify 上下文获取 fallback ──────────────────────────────────────


class TestMainClassifyContextFallback:
    """测试 main() 中 _build_classify_history 异常时的降级处理。"""

    def test_build_history_failure_falls_back(self, monkeypatch, capsys):
        """_build_classify_history 抛异常时仍调用 classify_intent 并输出结果。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "")
        monkeypatch.setenv("CSM_QA_GH_TOKEN", "fake-token")

        def mock_build(*args, **kwargs):
            raise RuntimeError("GraphQL 连接失败")

        monkeypatch.setattr("scripts.router._build_classify_history", mock_build)
        monkeypatch.setattr("sys.argv", [
            "router.py", "--classify-only",
            "--discussion-number", "42",
            "--comment-body", "请再检查",
            "--event-type", "discussion_comment",
            "--category-name", "Q&A",
        ])

        from scripts.router import main

        exit_code = main()
        assert exit_code == 0

        captured = capsys.readouterr()
        # 应输出意图（降级正则判定）
        assert captured.out.strip() in ("JOIN", "QA", "OTHER")

    def test_no_token_no_history(self, monkeypatch, capsys):
        """CSM_QA_GH_TOKEN 未配置时不尝试获取上下文。"""
        monkeypatch.setattr("scripts.router.LLM_API_KEY", "")
        monkeypatch.delenv("CSM_QA_GH_TOKEN", raising=False)

        # 确保 _build_classify_history 不被调用
        call_count = [0]
        orig = getattr(
            __import__("scripts.router", fromlist=["_build_classify_history"]),
            "_build_classify_history",
            None,
        )

        def counting_mock(*args, **kwargs):
            call_count[0] += 1
            return None

        monkeypatch.setattr("scripts.router._build_classify_history", counting_mock)
        monkeypatch.setattr("sys.argv", [
            "router.py", "--classify-only",
            "--discussion-number", "42",
            "--comment-body", "hello",
            "--event-type", "discussion_comment",
            "--category-name", "Q&A",
        ])

        from scripts.router import main

        exit_code = main()
        assert exit_code == 0
        # token 未配置 → 不应调用 _build_classify_history
        assert call_count[0] == 0
