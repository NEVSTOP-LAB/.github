"""Shared utilities for NEVSTOP-LAB GitHub Actions scripts.

Provides common logging configuration, timezone constant, and GitHub API
helpers that are reused across the scripts/ directory.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import timedelta, timezone
from typing import Optional

import requests

# ── Timezone ──────────────────────────────────────────────────────────────────

BEIJING_TZ = timezone(timedelta(hours=8))

# ── Logging ───────────────────────────────────────────────────────────────────


def configure_logging() -> None:
    """Configure root logging with a consistent format for all scripts.

    Uses ISO-8601 timestamps and ``levelname`` / ``name`` fields for easy
    filtering in GitHub Actions logs.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ── GitHub API helpers ────────────────────────────────────────────────────────


def github_token() -> Optional[str]:
    """Return the GitHub token from environment variables.

    Checks ``GITHUB_TOKEN`` first, then falls back to ``GH_TOKEN``.
    Returns ``None`` when neither is set.
    """
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def api_headers(
    token: Optional[str] = None,
    *,
    extra_accept: Optional[str] = None,
    user_agent: str = "NEVSTOP-LAB-scripts",
) -> dict[str, str]:
    """Build standard GitHub REST API v3 request headers.

    Args:
        token: Optional Bearer token.  When ``None``, reads from the
            environment (``GITHUB_TOKEN`` / ``GH_TOKEN``).
        extra_accept: Override the default ``Accept`` header value (e.g.
            ``"application/vnd.github.star+v3+json"``).
        user_agent: Value for the ``User-Agent`` header.
    """
    if token is None:
        token = github_token()
    headers: dict[str, str] = {
        "Accept": extra_accept or "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": user_agent,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def paginate(
    url: str,
    headers: dict[str, str],
    extra_params: dict | None = None,
) -> list[dict]:
    """Fetch all items from a paginated GitHub REST API endpoint.

    Uses ``per_page=100`` and increments ``page`` until an empty or
    incomplete page is returned.  A short sleep between pages avoids
    secondary rate limits.

    Returns:
        A flat list of all items (each a ``dict``) from every page.
        Returns an empty list when the first page is empty.
    """
    params: dict = {"per_page": 100, **(extra_params or {})}
    page = 1
    all_items: list[dict] = []
    while True:
        params["page"] = page
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 403:
            raise requests.HTTPError(
                "GitHub API rate limit reached (HTTP 403). "
                "Set GITHUB_TOKEN or GH_TOKEN for higher limits.",
                response=resp,
            )
        if resp.status_code == 404:
            raise requests.HTTPError(
                f"GitHub API endpoint not found or inaccessible: {resp.url}",
                response=resp,
            )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        all_items.extend(data)
        if len(data) < 100:
            break
        page += 1
        time.sleep(0.05)
    return all_items


def paginate_generator(
    url: str,
    headers: dict[str, str],
    extra_params: dict | None = None,
):
    """Generator variant of :func:`paginate` — yields items one by one.

    Prefer this when processing large result sets to keep memory low.
    """
    params = {"per_page": 100, **(extra_params or {})}
    page = 1
    while True:
        params["page"] = page
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 403:
            raise requests.HTTPError(
                "GitHub API rate limit reached (HTTP 403). "
                "Set GITHUB_TOKEN or GH_TOKEN for higher limits.",
                response=resp,
            )
        if resp.status_code == 404:
            raise requests.HTTPError(
                f"GitHub API endpoint not found or inaccessible: {resp.url}",
                response=resp,
            )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return
        yield from data
        if len(data) < 100:
            return
        page += 1
        time.sleep(0.05)


# ── Marker helpers ────────────────────────────────────────────────────────────


def marker_start(region: str) -> str:
    """Build a start marker string from a region name.

    >>> marker_start("VIPM_DOWNLOADS")
    '<!-- VIPM_DOWNLOADS_START -->'
    """
    return f"<!-- {region}_START -->"


def marker_end(region: str) -> str:
    """Build an end marker string from a region name.

    >>> marker_end("VIPM_DOWNLOADS")
    '<!-- VIPM_DOWNLOADS_END -->'
    """
    return f"<!-- {region}_END -->"
