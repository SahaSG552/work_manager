"""Persistent settings stored in SQLite — overrides .env defaults via web UI."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

_DB_PATH = Path(__file__).parent / "db" / "orders.db"

log = logging.getLogger(__name__)

# ── Default values (used when DB has no override) ────────────────────

DEFAULTS: dict[str, Any] = {
    # Owner mapping: prefix → folder name
    "owner_mapping": {
        "ЧМ": "KK", "СРБ": "KK", "ИП": "KK", "МКП": "KK", "МСЛ": "KK",
        "КМ": "KH", "АР": "AM", "YG": "YG", "БЕЗ": "БЕЗ",
    },
    # Tag rules: keyword → tag name
    "tag_rules": {
        "штапик": "Штапик",
        "галтель": "Галтель",
        "борт": "Борт",
        "почта": "почта",
        "текстурн": "Текстурный",
    },
    # Predefined edge types (for UI dropdown)
    "edge_types": ["борт-штапик", "штапик", "галтель", "борт стандарт", "борт 35", "борт 40"],
    # Predefined sink types
    "sink_types": ["подстольная", "накладная"],
    # Predefined thicknesses
    "thicknesses": ["30 мм", "40 мм"],
    # Scanner settings
    "email_since_days": 7,
    # YouGile rules — chain filters with AND/OR/NOT operators
    # Each rule: {type: "chain", chain: [{kind:"filter", field, op, value}, {kind:"op", value:"AND"|"OR"|"NOT"}, ...]}
    # Default between adjacent filters (no op) is AND.
    "yougile_rules": [
        {
            "type": "chain",
            "chain": [
                {"kind": "filter", "field": "board", "op": "eq", "value": "3fd8e940-b20a-4105-8ac0-67fbf54d9d24"},
                {"kind": "op", "value": "AND"},
                {"kind": "filter", "field": "column", "op": "eq", "value": "f33b585a-590e-4630-bf37-05d989bfd4ff"},
            ],
        },
        {
            "type": "chain",
            "chain": [
                {"kind": "filter", "field": "title", "op": "contains", "value": "фасад"},
                {"kind": "op", "value": "OR"},
                {"kind": "filter", "field": "sticker_name", "op": "contains", "value": "ORWOOD"},
            ],
        },
    ],
    # YouGile blacklist — task IDs to skip during scanning
    "yougile_blacklist": [],
    # Auto-mode intervals (seconds)
    "interval_yougile": 300,
    "interval_email": 600,
}


async def _ensure_table() -> None:
    """Create settings table if missing."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await db.commit()


async def get_setting(key: str) -> Any:
    """Get a setting value. Returns DEFAULTS[key] if not in DB."""
    await _ensure_table()
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        if row:
            return json.loads(row[0])
    return DEFAULTS.get(key)


async def get_all_settings() -> dict[str, Any]:
    """Get all settings (DB overrides merged with defaults)."""
    await _ensure_table()
    result = dict(DEFAULTS)
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        for key, value in rows:
            result[key] = json.loads(value)
    return result


async def set_setting(key: str, value: Any) -> None:
    """Set a setting value (overwrites)."""
    await _ensure_table()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        await db.commit()
    log.info(f"Setting updated: {key}")


async def set_settings_batch(updates: dict[str, Any]) -> None:
    """Set multiple settings at once."""
    await _ensure_table()
    async with aiosqlite.connect(_DB_PATH) as db:
        for key, value in updates.items():
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        await db.commit()
    log.info(f"Settings batch updated: {list(updates.keys())}")


async def reset_setting(key: str) -> None:
    """Remove a DB override (reverts to default)."""
    await _ensure_table()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await db.commit()


# ── Convenience getters (used by parser, scanner, etc.) ───────────────


async def get_owner_mapping() -> dict[str, str]:
    """Get prefix → folder mapping (DB override → .env → defaults)."""
    db_val = await get_setting("owner_mapping")
    if db_val:
        return db_val
    # Fallback to .env
    from app.config import settings
    env_map = settings.owner_mapping
    return env_map if env_map else DEFAULTS["owner_mapping"]


async def get_tag_rules() -> dict[str, str]:
    """Get keyword → tag mapping."""
    return await get_setting("tag_rules")


async def get_edge_types() -> list[str]:
    return await get_setting("edge_types")


async def get_sink_types() -> list[str]:
    return await get_setting("sink_types")


async def get_thicknesses() -> list[str]:
    return await get_setting("thicknesses")


async def get_email_since_days() -> int:
    return await get_setting("email_since_days")


async def get_yougile_rules() -> list[dict]:
    """Get all YouGile scanning rules."""
    return await get_setting("yougile_rules")


async def get_yougile_blacklist() -> list[str]:
    """Get YouGile task IDs blacklist."""
    return await get_setting("yougile_blacklist")


async def get_scan_intervals() -> dict[str, int]:
    """Returns {source: seconds}."""
    return {
        "yougile": await get_setting("interval_yougile"),
        "email": await get_setting("interval_email"),
    }
