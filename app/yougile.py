"""YouGile API client for task tracking and notifications."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import unquote

import httpx

from app.config import settings

log = logging.getLogger(__name__)

_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {settings.yougile_api_key}",
}


async def _request(
    method: str, endpoint: str, json_data: dict | None = None, *, params: dict | None = None,
) -> Any:
    """Make an authenticated request to YouGile API."""
    url = f"{settings.yougile_base_url}{endpoint}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.request(
            method, url, headers=_HEADERS, json=json_data, params=params,
        )
        response.raise_for_status()
        if response.status_code == 204:
            return None
        return response.json()


# ── Projects ─────────────────────────────────────────────────────────


async def get_projects() -> list[dict]:
    """List all projects in the company."""
    data = await _request("GET", "/projects")
    return data if isinstance(data, list) else data.get("content", [])


# ── Boards ───────────────────────────────────────────────────────────


async def get_boards(project_id: str | None = None) -> list[dict]:
    """List boards, optionally filtered by project."""
    p = {"projectId": project_id} if project_id else None
    data = await _request("GET", "/boards", params=p)
    return data if isinstance(data, list) else data.get("content", [])


async def get_board(board_id: str) -> dict:
    """Get a single board by ID."""
    return await _request("GET", f"/boards/{board_id}")


async def get_columns(board_id: str) -> list[dict]:
    """List columns for a board."""
    data = await _request("GET", "/columns", params={"boardId": board_id})
    return data.get("content", []) if isinstance(data, dict) else data


# ── Tasks ────────────────────────────────────────────────────────────


async def get_tasks(
    project_id: str | None = None,
    column_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """List tasks with optional filters. Returns {content: [...], paging: {...}}."""
    p: dict = {"limit": limit, "offset": offset}
    if project_id:
        p["projectId"] = project_id
    if column_id:
        p["columnId"] = column_id
    data = await _request("GET", "/tasks", params=p)
    if isinstance(data, list):
        return {"content": data, "paging": {}}
    return data


async def get_task(task_id: str) -> dict:
    """Get a single task by ID."""
    return await _request("GET", f"/tasks/{task_id}")


async def create_task(title: str, column_id: str, description: str = "") -> dict:
    """Create a new task in YouGile."""
    payload = {
        "title": title,
        "columnId": column_id,
        "description": description,
        "companyId": settings.yougile_company_id,
    }
    return await _request("POST", "/tasks", payload)


# ── Users ───────────────────────────────────────────────────────────


async def get_users() -> list[dict]:
    """List all users in the company. Returns [{id, realName, email}, ...]."""
    data = await _request("GET", "/users")
    content = data.get("content", []) if isinstance(data, dict) else data
    return [
        {"id": u.get("id", ""), "realName": u.get("realName", ""), "email": u.get("email", "")}
        for u in content
    ]


# ── Stickers ─────────────────────────────────────────────────────────


async def get_string_stickers(board_id: str | None = None) -> list[dict]:
    """Get string stickers (custom fields with named states).

    Returns list of {id, name, states: [{id, name, color}, ...]}.
    Each sticker UUID maps to a "name" (e.g. "Фасады", "Статус"),
    and each state {id, name} maps the hex value on a task to a readable label.
    """
    p: dict = {"limit": 100}
    if board_id:
        p["boardId"] = board_id
    data = await _request("GET", "/string-stickers", params=p)
    return data.get("content", []) if isinstance(data, dict) else data


async def build_sticker_map(board_id: str | None = None) -> dict[str, dict]:
    """Build a mapping: sticker_value_id → {sticker_name, state_name}.

    Example: {"def4c8841b9e": {"sticker": "Фасады", "state": "ORWOOD"}}
    Used by scanner to resolve opaque hex sticker values to readable names.
    """
    stickers = await get_string_stickers(board_id)
    mapping: dict[str, dict] = {}
    for sticker in stickers:
        sticker_name = sticker.get("name", "")
        for state in sticker.get("states", []):
            state_id = state.get("id", "")
            state_name = state.get("name", "")
            if state_id:
                mapping[state_id] = {"sticker": sticker_name, "state": state_name}
    return mapping


# ── Webhooks ─────────────────────────────────────────────────────────


async def create_webhook(url: str, event: str = "task-*") -> dict:
    payload = {"url": url, "event": event}
    return await _request("POST", "/webhooks", payload)


async def list_webhooks() -> list[dict]:
    data = await _request("GET", "/webhooks")
    return data if isinstance(data, list) else data.get("content", [])


async def delete_webhook(webhook_id: str) -> None:
    await _request("DELETE", f"/webhooks/{webhook_id}")


# ── Chat messages ────────────────────────────────────────────────────


async def get_task_comments(task_id: str, limit: int = 20) -> list[dict]:
    """Get recent comments/messages in a task's chat."""
    data = await _request("GET", f"/chats/{task_id}/messages", params={"limit": limit})
    return data.get("content", []) if isinstance(data, dict) else data


async def download_file(file_url: str) -> tuple[bytes, str] | None:
    """Download a file from YouGile.

    Returns (file_bytes, filename) or None on failure.
    """
    clean = file_url
    if clean.startswith("/root/#file:"):
        clean = clean[len("/root/#file:"):]
    clean = unquote(unquote(clean))

    url = "https://yougile.com" + clean
    filename = clean.rsplit("/", 1)[-1].split("?")[0] or "file"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            r = await client.get(url, headers=_HEADERS, follow_redirects=True)
            if r.status_code == 200:
                log.info(f"[yougile] Downloaded file: {filename} ({len(r.content)} bytes) from {url[:80]}")
                return r.content, filename
            log.warning(f"[yougile] File download failed: {r.status_code} for {url[:80]}")
            return None
        except Exception as e:
            log.error(f"File download error: {e}")
            return None


# ── Utility ──────────────────────────────────────────────────────────


async def test_connection() -> dict | None:
    """Test the YouGile API connection."""
    try:
        return await _request("GET", "/companies")
    except Exception as e:
        log.error(f"YouGile connection test failed: {e}")
        return None
