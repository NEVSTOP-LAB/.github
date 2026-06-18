"""tests/test_org_membership_cleanup.py — org_membership_cleanup.py 单元测试."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import requests

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts.org_membership_cleanup import (
    discover_team_chain,
    get_user_level,
    query_last_contribution_time,
    downgrade_user,
    _user_in_other_teams,
    load_state,
    save_state,
    list_org_members,
    run,
    ORG,
    ANCHOR_TEAM,
    CHECK_INTERVAL_DAYS,
    STATE_FILE,
    _parse_iso_datetime,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

FAKE_TOKEN = "fake-token"

CHAIN = ["csm-community", "csm-module-author", "csm-developer"]


@pytest.fixture(autouse=True)
def _mock_time_sleep(monkeypatch):
    """全局 mock time.sleep，避免搜索 API 延迟拖慢单元测试。"""
    monkeypatch.setattr("time.sleep", lambda _: None)


@pytest.fixture
def mock_team_api(monkeypatch):
    """Mock team GET API: return a chain of parent-linked teams."""
    team_data = {
        "csm-developer": {
            "slug": "csm-developer",
            "parent": {"slug": "csm-module-author"},
        },
        "csm-module-author": {
            "slug": "csm-module-author",
            "parent": {"slug": "csm-community"},
        },
        "csm-community": {
            "slug": "csm-community",
            "parent": None,
        },
    }

    def mock_rest_get(token, path, **params):
        for slug, data in team_data.items():
            if path == f"/orgs/{ORG}/teams/{slug}":
                return data
        raise requests.HTTPError(response=MagicMock(status_code=404))

    monkeypatch.setattr(
        "scripts.org_membership_cleanup._rest_get", mock_rest_get
    )


@pytest.fixture
def mock_membership_api(monkeypatch):
    """Mock team membership check API."""
    memberships = {
        "alice": "csm-developer",
        "bob": "csm-module-author",
        "charlie": "csm-community",
    }

    def mock_get(url, headers, timeout=30):
        resp = MagicMock()
        for user, team in memberships.items():
            if f"/teams/{team}/memberships/{user}" in url:
                resp.status_code = 200
                return resp
        resp.status_code = 404
        http_error = requests.HTTPError(response=resp)
        http_error.response = resp
        raise http_error

    monkeypatch.setattr("requests.get", mock_get)


@pytest.fixture
def mock_list_members(monkeypatch):
    """Mock org member listing."""
    def mock_paginate(url, headers, extra_params=None):
        return [
            {"login": "alice"},
            {"login": "bob"},
            {"login": "charlie"},
            {"login": "dave"},  # not in any CSM team
        ]

    monkeypatch.setattr(
        "scripts.org_membership_cleanup.paginate", mock_paginate
    )


@pytest.fixture
def temp_state_file(tmp_path, monkeypatch):
    """Use a temporary state file."""
    import scripts.org_membership_cleanup as _mod
    state_path = tmp_path / "member_check_state.json"
    monkeypatch.setattr(_mod, "STATE_FILE", str(state_path))
    return state_path


# ── Team Chain Discovery ──────────────────────────────────────────────────────


class TestDiscoverTeamChain:
    def test_basic_chain(self, mock_team_api):
        chain = discover_team_chain(FAKE_TOKEN, ORG, ANCHOR_TEAM)
        assert chain == CHAIN

    def test_single_team(self, monkeypatch):
        """If only anchor exists with no parent."""
        def mock_get(token, path, **params):
            if path == f"/orgs/{ORG}/teams/csm-developer":
                return {"slug": "csm-developer", "parent": None}
            raise requests.HTTPError(response=MagicMock(status_code=404))

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_get", mock_get
        )
        chain = discover_team_chain(FAKE_TOKEN, ORG, ANCHOR_TEAM)
        assert chain == ["csm-developer"]


# ── User Level Detection ─────────────────────────────────────────────────────


class TestGetUserLevel:
    def test_developer(self, monkeypatch):
        """Developer returns highest index."""
        def mock_get(url, headers, timeout=30):
            resp = MagicMock()
            if "csm-developer/memberships/alice" in url:
                resp.status_code = 200
                return resp
            resp.status_code = 404
            http_error = requests.HTTPError(response=resp)
            http_error.response = resp
            raise http_error

        monkeypatch.setattr("requests.get", mock_get)
        level = get_user_level(FAKE_TOKEN, ORG, "alice", CHAIN)
        assert level == 2

    def test_community(self, monkeypatch):
        """Community returns index 0."""
        def mock_get(url, headers, timeout=30):
            resp = MagicMock()
            if "csm-community/memberships/bob" in url:
                resp.status_code = 200
                return resp
            resp.status_code = 404
            http_error = requests.HTTPError(response=resp)
            http_error.response = resp
            raise http_error

        monkeypatch.setattr("requests.get", mock_get)
        level = get_user_level(FAKE_TOKEN, ORG, "bob", CHAIN)
        assert level == 0

    def test_not_in_any(self, monkeypatch):
        """Returns -1 when not in any team."""
        def mock_get(url, headers, timeout=30):
            resp = MagicMock()
            resp.status_code = 404
            http_error = requests.HTTPError(response=resp)
            http_error.response = resp
            raise http_error

        monkeypatch.setattr("requests.get", mock_get)
        level = get_user_level(FAKE_TOKEN, ORG, "dave", CHAIN)
        assert level == -1

    def test_user_in_multiple_teams_returns_highest(self, monkeypatch):
        """User in both csm-community and csm-module-author → returns higher index (2)."""
        call_count = [0]

        def mock_get(url, headers, timeout=30):
            resp = MagicMock()
            call_count[0] += 1
            # First call (anchor → root): csm-developer? return 404
            if "csm-developer/memberships/eve" in url and call_count[0] == 1:
                resp.status_code = 404
                http_error = requests.HTTPError(response=resp)
                http_error.response = resp
                raise http_error
            # Second call: csm-module-author? return 200
            if "csm-module-author/memberships/eve" in url and call_count[0] == 2:
                resp.status_code = 200
                return resp
            resp.status_code = 404
            http_error = requests.HTTPError(response=resp)
            http_error.response = resp
            raise http_error

        monkeypatch.setattr("requests.get", mock_get)
        level = get_user_level(FAKE_TOKEN, ORG, "eve", CHAIN)
        assert level == 1  # csm-module-author (index 1), not csm-community (index 0)


# ── Contribution Query ────────────────────────────────────────────────────────


class TestQueryLastContributionTime:
    def test_has_contribution(self, monkeypatch):
        """User has a recent closed issue."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=14)

        def mock_rest_get(token, path, **params):
            if "/search/issues" in path:
                q = params.get("q", "")
                if "created:>=" in q:
                    return {
                        "items": [
                            {"created_at": (now - timedelta(days=3)).isoformat()}
                        ]
                    }
                if "closed:>=" in q:
                    return {
                        "items": [
                            {"closed_at": (now - timedelta(days=3)).isoformat()}
                        ]
                    }
            return {"items": []}

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_get", mock_rest_get
        )

        # Also mock commits search
        def mock_commits_get(url, headers, params, timeout=30):
            resp = MagicMock()
            resp.json.return_value = {"items": []}
            resp.status_code = 200
            return resp

        monkeypatch.setattr("requests.get", mock_commits_get)

        result = query_last_contribution_time(FAKE_TOKEN, ORG, "alice", since)
        assert result is not None

    def test_no_contribution(self, monkeypatch):
        """User has no contributions."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=14)

        def mock_rest_get(token, path, **params):
            return {"items": []}

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_get", mock_rest_get
        )

        def mock_commits_get(url, headers, params, timeout=30):
            resp = MagicMock()
            resp.json.return_value = {"items": []}
            resp.status_code = 200
            return resp

        monkeypatch.setattr("requests.get", mock_commits_get)

        result = query_last_contribution_time(FAKE_TOKEN, ORG, "alice", since)
        assert result is None

    def test_commit_contribution(self, monkeypatch):
        """User has a recent commit."""
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=14)

        def mock_rest_get(token, path, **params):
            return {"items": []}

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_get", mock_rest_get
        )

        def mock_commits_get(url, headers, params, timeout=30):
            resp = MagicMock()
            resp.json.return_value = {
                "items": [
                    {
                        "commit": {
                            "committer": {
                                "date": (now - timedelta(days=5)).isoformat()
                            }
                        }
                    }
                ]
            }
            resp.status_code = 200
            return resp

        monkeypatch.setattr("requests.get", mock_commits_get)

        result = query_last_contribution_time(FAKE_TOKEN, ORG, "alice", since)
        assert result is not None


# ── Downgrade ─────────────────────────────────────────────────────────────────


class TestDowngradeUser:
    def test_downgrade_module_author(self, monkeypatch):
        """Module-Author → Community."""
        calls = []

        def mock_delete(token, path):
            calls.append(path)
            return True

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_delete", mock_delete
        )

        result = downgrade_user(FAKE_TOKEN, ORG, "bob", 1, CHAIN)
        assert result == "csm-community"
        assert f"/teams/csm-module-author/memberships/bob" in calls[0]

    def test_remove_from_org(self, monkeypatch):
        """Community, no other teams → removed from org."""
        calls = []

        def mock_delete(token, path):
            calls.append(path)
            return True

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_delete", mock_delete
        )

        # Mock paginate for _user_in_other_teams: only CSM-chain teams (no others)
        def mock_paginate(url, headers, extra_params=None):
            return [{"slug": s} for s in CHAIN]

        monkeypatch.setattr(
            "scripts.org_membership_cleanup.paginate", mock_paginate
        )

        # Mock membership check for non-CSM teams (none exist, so no calls expected)
        monkeypatch.setattr("requests.get", lambda *a, **kw: MagicMock(status_code=404))

        result = downgrade_user(FAKE_TOKEN, ORG, "charlie", 0, CHAIN)
        assert result is None
        # First: remove from CSM-Community team
        assert f"/teams/csm-community/memberships/charlie" in calls[0]
        # Second: remove from org (no other teams)
        assert f"/orgs/{ORG}/memberships/charlie" in calls[1]

    def test_community_with_other_teams(self, monkeypatch):
        """Community + other team → only removed from CSM-Community, kept in org."""
        calls = []

        def mock_delete(token, path):
            calls.append(path)
            return True

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_delete", mock_delete
        )

        # Mock paginate: CSM-chain teams + one extra team
        def mock_paginate(url, headers, extra_params=None):
            return [{"slug": s} for s in CHAIN] + [{"slug": "project-x"}]

        monkeypatch.setattr(
            "scripts.org_membership_cleanup.paginate", mock_paginate
        )

        # Mock membership check: user IS in project-x
        def mock_get(url, headers, timeout=30):
            resp = MagicMock()
            if "project-x/memberships/charlie" in url:
                resp.status_code = 200
                return resp
            resp.status_code = 404
            http_error = requests.HTTPError(response=resp)
            http_error.response = resp
            raise http_error

        monkeypatch.setattr("requests.get", mock_get)

        result = downgrade_user(FAKE_TOKEN, ORG, "charlie", 0, CHAIN)
        assert result == "kept"
        # Only one call: remove from CSM-Community team
        assert len(calls) == 1
        assert f"/teams/csm-community/memberships/charlie" in calls[0]
        # No org removal call

    def test_already_removed_404(self, monkeypatch):
        """404 on team delete + no other teams → second delete also 404 → still succeeds."""
        def mock_delete(token, path):
            resp = MagicMock()
            resp.status_code = 404
            raise requests.HTTPError(response=resp)

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_delete", mock_delete
        )

        # Mock paginate: only CSM teams (no others)
        def mock_paginate(url, headers, extra_params=None):
            return [{"slug": s} for s in CHAIN]

        monkeypatch.setattr(
            "scripts.org_membership_cleanup.paginate", mock_paginate
        )

        # Should not raise — both deletes return 404
        result = downgrade_user(FAKE_TOKEN, ORG, "charlie", 0, CHAIN)
        assert result is None


# ── State File ────────────────────────────────────────────────────────────────


class TestStateFile:
    def test_load_nonexistent(self, tmp_path, monkeypatch):
        import scripts.org_membership_cleanup as _mod
        state_path = tmp_path / "nonexistent.json"
        monkeypatch.setattr(_mod, "STATE_FILE", str(state_path))
        state = load_state()
        assert "users" in state
        assert state["users"] == {}

    def test_save_and_load(self, tmp_path, monkeypatch):
        import scripts.org_membership_cleanup as _mod
        state_path = tmp_path / "test_state.json"
        monkeypatch.setattr(_mod, "STATE_FILE", str(state_path))
        state = {
            "_comment": "test",
            "users": {
                "alice": {"last_check": "2025-01-01T00:00:00+00:00", "team": "csm-developer"},
            },
        }
        save_state(state)
        loaded = load_state()
        assert loaded == state

    def test_load_legacy_format(self, tmp_path, monkeypatch):
        """Legacy format without team field should not crash."""
        import scripts.org_membership_cleanup as _mod
        state_path = tmp_path / "legacy.json"
        state_path.write_text(json.dumps({
            "_comment": "old",
            "users": {"bob": {"last_check": "2025-01-01T00:00:00+00:00"}},
        }))
        monkeypatch.setattr(_mod, "STATE_FILE", str(state_path))
        state = load_state()
        assert "bob" in state["users"]
        # Missing 'team' key is tolerated — handled in run() logic


# ── Org Members List ──────────────────────────────────────────────────────────


class TestListOrgMembers:
    def test_paginated(self, monkeypatch):
        def mock_paginate(url, headers, extra_params=None):
            return [
                {"login": "alice"},
                {"login": "bob"},
            ]

        monkeypatch.setattr(
            "scripts.org_membership_cleanup.paginate", mock_paginate
        )

        members = list_org_members(FAKE_TOKEN, ORG)
        assert members == ["alice", "bob"]


# ── ISO datetime parsing ─────────────────────────────────────────────────────


class TestParseIsoDatetime:
    def test_z_suffix(self):
        dt = _parse_iso_datetime("2025-06-15T08:30:00Z")
        assert dt.tzinfo is not None
        assert dt.hour == 8

    def test_offset(self):
        dt = _parse_iso_datetime("2025-06-15T08:30:00+00:00")
        assert dt.tzinfo is not None


# ── Main Run Loop (integration-level) ────────────────────────────────────────


class TestRun:
    def _setup_mocks(self, monkeypatch, temp_state_file, members, memberships,
                     contributions=None):
        """Shared setup for run() tests.

        Args:
            memberships: dict of username -> team_slug for membership mock.
            contributions: dict of username -> bool for contribution search mock.
        """
        # Token
        monkeypatch.setenv("SYNC_GITHUB_TOKEN", FAKE_TOKEN)

        # Team chain
        def mock_rest_get(token, path, **params):
            team_map = {
                f"/orgs/{ORG}/teams/csm-developer": {
                    "slug": "csm-developer",
                    "parent": {"slug": "csm-module-author"},
                },
                f"/orgs/{ORG}/teams/csm-module-author": {
                    "slug": "csm-module-author",
                    "parent": {"slug": "csm-community"},
                },
                f"/orgs/{ORG}/teams/csm-community": {
                    "slug": "csm-community",
                    "parent": None,
                },
            }
            if path in team_map:
                return team_map[path]

            # Contribution search — controlled by contributions dict
            if "/search/issues" in path:
                q = params.get("q", "")
                for user, has_contrib in (contributions or {}).items():
                    if f"author:{user}" in q or f"assignee:{user}" in q:
                        if has_contrib:
                            return {"items": [{"created_at": "2025-06-20T08:00:00Z"}]}
                        return {"items": []}
                return {"items": []}

            raise requests.HTTPError(response=MagicMock(status_code=404))

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_get", mock_rest_get
        )

        # Unified HTTP GET mock — handles both membership checks and commit search
        def mock_requests_get(url, headers=None, params=None, timeout=30, **kwargs):
            resp = MagicMock()
            # Membership check?
            for user, team in (memberships or {}).items():
                if f"/teams/{team}/memberships/{user}" in url:
                    resp.status_code = 200
                    return resp
            # Any CSM membership URL that doesn't match → 404
            if "/memberships/" in url:
                resp.status_code = 404
                http_error = requests.HTTPError(response=resp)
                http_error.response = resp
                raise http_error
            # Commit search → empty result
            resp.json.return_value = {"items": []}
            resp.status_code = 200
            return resp

        monkeypatch.setattr("requests.get", mock_requests_get)

        # Org members / teams listing (paginate handles both)
        def mock_paginate(url, headers, extra_params=None):
            if "/teams" in url:
                # Return only CSM-chain teams, so _user_in_other_teams finds nothing
                return [{"slug": s} for s in CHAIN]
            return [{"login": m} for m in members]

        monkeypatch.setattr(
            "scripts.org_membership_cleanup.paginate", mock_paginate
        )

        # Delete (downgrade)
        delete_calls = []

        def mock_delete(token, path):
            delete_calls.append(path)
            return True

        monkeypatch.setattr(
            "scripts.org_membership_cleanup._rest_delete", mock_delete
        )

        return delete_calls

    def test_dry_run_no_actions(self, monkeypatch, temp_state_file):
        """Dry-run should log but not call delete."""
        now = datetime.now(timezone.utc)
        monkeypatch.setenv("SYNC_GITHUB_TOKEN", FAKE_TOKEN)
        import scripts.org_membership_cleanup as _mod
        monkeypatch.setattr(_mod, "STATE_FILE", str(temp_state_file))

        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["bob"],
            memberships={"bob": "csm-module-author"},
            contributions={"bob": False},
        )

        run(dry_run=True)
        assert len(delete_calls) == 0  # No real deletions in dry-run

    def test_anchor_skipped(self, monkeypatch, temp_state_file):
        """CSM-Developer should always be skipped."""
        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["alice"],
            memberships={"alice": "csm-developer"},
        )

        run(dry_run=False)
        assert len(delete_calls) == 0

    def test_user_not_in_csm_team_skipped(self, monkeypatch, temp_state_file):
        """User not in any CSM team should be skipped."""
        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["dave"],
            memberships={},  # dave not in any CSM team
        )

        run(dry_run=False)
        assert len(delete_calls) == 0

    def test_first_time_user_gets_grace_period(self, monkeypatch, temp_state_file):
        """New user with no state should get a grace period, not be checked immediately."""
        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["charlie"],
            memberships={"charlie": "csm-community"},
            contributions={"charlie": False},
        )

        run(dry_run=False)
        # New user gets last_check=now → days_since=0 < 14 → skipped (grace period)
        assert len(delete_calls) == 0, (
            f"Expected charlie to be SKIPPED (grace period), got calls: {delete_calls}"
        )

        # State file should record charlie with last_check ≈ now
        state = json.loads(temp_state_file.read_text())
        charlie_state = state["users"].get("charlie", {})
        assert charlie_state.get("team") == "csm-community", (
            f"Expected charlie to be recorded in state, got: {charlie_state}"
        )
        # last_check should be close to now (within a few seconds)
        last_check_dt = datetime.fromisoformat(charlie_state["last_check"])
        assert abs((datetime.now(timezone.utc) - last_check_dt).total_seconds()) < 30, (
            f"Expected last_check ≈ now, got: {charlie_state['last_check']}"
        )

    def test_corrupt_state_gets_grace_period_and_repairs(self, monkeypatch, temp_state_file):
        """User with unparseable last_check should get grace period and state repaired."""
        # Pre-populate state file with a corrupt entry
        state = {
            "_comment": "test",
            "users": {
                "bob": {"last_check": "NOT-A-DATE", "team": "csm-module-author"},
            },
        }
        temp_state_file.write_text(json.dumps(state))

        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["bob"],
            memberships={"bob": "csm-module-author"},
        )

        run(dry_run=False)
        # Corrupt state → grace period → skipped
        assert len(delete_calls) == 0, (
            f"Expected bob to be SKIPPED (grace period for corrupt state), got calls: {delete_calls}"
        )

        # State file should have repaired the corrupt entry
        repaired_state = json.loads(temp_state_file.read_text())
        bob_state = repaired_state["users"].get("bob", {})
        assert bob_state.get("team") == "csm-module-author"
        # last_check should now be a valid ISO datetime close to now
        last_check_dt = datetime.fromisoformat(bob_state["last_check"])
        assert abs((datetime.now(timezone.utc) - last_check_dt).total_seconds()) < 30, (
            f"Expected repaired last_check ≈ now, got: {bob_state['last_check']}"
        )

    def test_removed_user_rejoin_gets_grace_period(self, monkeypatch, temp_state_file):
        """User previously removed (team=removed) who rejoins should get fresh grace period."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=60)).isoformat()

        # Pre-populate state: user was removed 60 days ago
        state = {
            "_comment": "test",
            "users": {
                "charlie": {"last_check": old.isoformat(), "team": "removed"},
            },
        }
        temp_state_file.write_text(json.dumps(state))

        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["charlie"],
            memberships={"charlie": "csm-community"},
            contributions={"charlie": False},
        )

        run(dry_run=False)
        # Should be skipped — rejoin triggers grace period reset
        assert len(delete_calls) == 0, (
            f"Expected charlie to be SKIPPED (rejoin grace period), got calls: {delete_calls}"
        )

        # State should be updated: team no longer "removed", last_check ≈ now
        updated_state = json.loads(temp_state_file.read_text())
        charlie_state = updated_state["users"].get("charlie", {})
        assert charlie_state.get("team") == "csm-community", (
            f"Expected team to be updated to csm-community, got: {charlie_state}"
        )
        last_check_dt = datetime.fromisoformat(charlie_state["last_check"])
        assert abs((datetime.now(timezone.utc) - last_check_dt).total_seconds()) < 30, (
            f"Expected last_check ≈ now, got: {charlie_state['last_check']}"
        )

    def test_within_window_skipped(self, monkeypatch, temp_state_file):
        """User checked recently should be skipped."""
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(days=3)).isoformat()

        # Pre-populate state file
        state = {
            "_comment": "test",
            "users": {
                "bob": {"last_check": recent, "team": "csm-module-author"},
            },
        }
        temp_state_file.write_text(json.dumps(state))

        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["bob"],
            memberships={"bob": "csm-module-author"},
        )

        run(dry_run=False)
        # Should be skipped — within 14 days
        assert len(delete_calls) == 0

    def test_window_expired_no_contrib_downgrade(self, monkeypatch, temp_state_file):
        """User past 14 days with no contributions → downgrade."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=20)).isoformat()

        state = {
            "_comment": "test",
            "users": {
                "bob": {"last_check": old, "team": "csm-module-author"},
            },
        }
        temp_state_file.write_text(json.dumps(state))

        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["bob"],
            memberships={"bob": "csm-module-author"},
            contributions={"bob": False},
        )

        run(dry_run=False)
        # Bob should be removed from module-author
        downgraded = any("csm-module-author/memberships/bob" in c for c in delete_calls)
        assert downgraded, f"Expected bob downgrade, got calls: {delete_calls}"

    def test_window_expired_with_contrib_updates_check_time(self, monkeypatch, temp_state_file):
        """User past 14 days WITH contributions → last_check updated to contribution time, no downgrade."""
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=20)).isoformat()

        state = {
            "_comment": "test",
            "users": {
                "bob": {"last_check": old, "team": "csm-module-author"},
            },
        }
        temp_state_file.write_text(json.dumps(state))

        delete_calls = self._setup_mocks(
            monkeypatch, temp_state_file,
            members=["bob"],
            memberships={"bob": "csm-module-author"},
            contributions={"bob": True},  # has contribution!
        )

        run(dry_run=False)
        # No downgrade should happen
        assert len(delete_calls) == 0

        # State file should have updated last_check to the contribution time (2025-06-20)
        state = json.loads(temp_state_file.read_text())
        bob_state = state["users"].get("bob", {})
        assert "last_check" in bob_state
        # last_check should be set to the contribution time, not now
        assert "2025-06-20" in bob_state["last_check"], (
            f"Expected last_check around contribution time, got: {bob_state['last_check']}"
        )


# ── Constants ─────────────────────────────────────────────────────────────────


class TestConstants:
    def test_org(self):
        assert ORG == "NEVSTOP-LAB"

    def test_anchor(self):
        assert ANCHOR_TEAM == "csm-developer"

    def test_interval(self):
        assert CHECK_INTERVAL_DAYS == 14
