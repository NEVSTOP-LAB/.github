"""Lightweight GitHub GraphQL client (stdlib only) shared by discussion bot & router.

Provides a single :class:`GitHubGraphQL` class that both
:file:`discussion_bot.py` and :file:`router.py` can import, eliminating the
duplicate ``GQL`` / ``GitHubGraphQL`` implementations.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"


class GitHubGraphQL:
    """Minimal GitHub GraphQL client using only stdlib :mod:`urllib`.

    Usage::

        client = GitHubGraphQL("ghp_xxxx")
        data = client.query("query { viewer { login } }")
    """

    # Accept: GitHub recommends this preview header so the API includes
    # ``isAnswerable`` etc. on DiscussionCategory nodes.
    _DEFAULT_ACCEPT = "application/vnd.github.v3+json"

    def __init__(
        self,
        token: str,
        *,
        user_agent: str = "github-graphql/1.0",
    ) -> None:
        if not token:
            raise ValueError("GitHub token (CSM_QA_GH_TOKEN) 未配置")
        self._token = token
        self._user_agent = user_agent

    def query(self, gql: str, variables: Optional[dict] = None) -> dict:
        """Execute a GraphQL query and return the ``data`` node.

        Raises:
            RuntimeError: On HTTP errors or when the response contains
                a non-empty ``errors`` field.
        """
        payload = json.dumps({"query": gql, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            GITHUB_GRAPHQL_URL,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": self._DEFAULT_ACCEPT,
                "User-Agent": self._user_agent,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub GraphQL HTTP {exc.code}: {body[:400]}"
            ) from exc

        result: dict = json.loads(raw)
        if result.get("errors"):
            messages = "; ".join(e.get("message", "") for e in result["errors"])
            raise RuntimeError(f"GitHub GraphQL errors: {messages}")
        return result.get("data", {})

    def rest_get(self, path: str, *, timeout: int = 30) -> list | dict:
        """Make a GitHub REST API GET request and return the parsed JSON body.

        Args:
            path: API path starting with ``/`` (e.g. ``/orgs/NEVSTOP-LAB/teams/csm-committee/members``).
            timeout: Request timeout in seconds.

        Returns:
            Parsed JSON response (list or dict, depending on the endpoint).

        Raises:
            RuntimeError: On HTTP errors (non-2xx status).
        """
        url = f"https://api.github.com{path}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": self._user_agent,
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"GitHub REST GET {path} HTTP {exc.code}: {body[:400]}"
            ) from exc
