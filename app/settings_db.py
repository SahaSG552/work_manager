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
    # Owner mapping: prefix → folder name (kept for backward compat, derived from clients)
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
    # MAX bridge settings
    "max_bridge": {
        "enabled": True,
        "chats": ["-78265973371088"],  # GO-Stone
        "blacklist_users": [233143504],  # own account
        "tg_chat_id": -1003914208415,
        "tg_topic_id": 3,  # KK topic
        "session_token": "",
        "session_dir": "./max_sessions",
    },
    # Auto-mode intervals (seconds)
    "interval_yougile": 300,
    "interval_email": 600,
    # Per-client custom parameters (kept for backward compat, derived from clients)
    "client_params": {
        "KK": [
            {"name": "bort", "label": "Борт", "parse_type": "keyword", "parse_rules": ["борт", "борта", "бортом"], "sort_order": 1},
            {"name": "torec", "label": "Торец", "parse_type": "keyword", "parse_rules": ["торец", "торцом", "торцы"], "sort_order": 2},
            {"name": "galtel", "label": "Галтель", "parse_type": "keyword", "parse_rules": ["галтель", "галтелью", "галтелью"], "sort_order": 3},
            {"name": "stenovaya", "label": "Стеновая", "parse_type": "keyword", "parse_rules": ["стеновая", "стеновой", "стеновые"], "sort_order": 4},
            {"name": "moika", "label": "Мойка", "parse_type": "keyword", "parse_rules": ["мойка", "подстольн", "накладн"], "sort_order": 5},
            {"name": "varochnaya", "label": "Варочная панель", "parse_type": "keyword", "parse_rules": ["варочн", "варочная", "панель"], "sort_order": 6},
        ],
        "AM": [
            {"name": "thickness", "label": "Толщина", "parse_type": "keyword", "parse_rules": ["толщин", "толщина"], "sort_order": 1},
            {"name": "model", "label": "Модель", "parse_type": "keyword", "parse_rules": ["модель"], "sort_order": 2},
            {"name": "s1", "label": "S1", "parse_type": "manual", "parse_rules": [], "sort_order": 3},
            {"name": "s2", "label": "S2", "parse_type": "manual", "parse_rules": [], "sort_order": 4},
            {"name": "s3", "label": "S3", "parse_type": "manual", "parse_rules": [], "sort_order": 5},
        ],
    },
    # Unified clients config — single source of truth
    # Each client: {code, display_name, prefixes, color, description, params: [...]}
    # owner_mapping and client_params are auto-derived from this on first access
    "clients": {
        "KK": {
            "code": "KK",
            "display_name": "Керамический камень",
            "prefixes": ["ЧМ", "СРБ", "ИП", "МКП", "МСЛ"],
            "color": "#0d6efd",
            "description": "Столешницы из акрилового камня",
            "params": [
                {"name": "bort", "label": "Борт", "frontmatter_key": "bort", "parse_type": "keyword", "parse_rules": ["борт", "борта", "бортом"], "sort_order": 1},
                {"name": "torec", "label": "Торец", "frontmatter_key": "torec", "parse_type": "keyword", "parse_rules": ["торец", "торцом", "торцы"], "sort_order": 2},
                {"name": "galtel", "label": "Галтель", "frontmatter_key": "galtel", "parse_type": "keyword", "parse_rules": ["галтель", "галтелью", "галтелью"], "sort_order": 3},
                {"name": "stenovaya", "label": "Стеновая", "frontmatter_key": "stenovaya", "parse_type": "keyword", "parse_rules": ["стеновая", "стеновой", "стеновые"], "sort_order": 4},
                {"name": "moika", "label": "Мойка", "frontmatter_key": "moika", "parse_type": "keyword", "parse_rules": ["мойка", "подстольн", "накладн"], "sort_order": 5},
                {"name": "varochnaya", "label": "Варочная панель", "frontmatter_key": "varochnaya", "parse_type": "keyword", "parse_rules": ["варочн", "варочная", "панель"], "sort_order": 6},
            ],
        },
        "AM": {
            "code": "AM",
            "display_name": "МДФ в эмали",
            "prefixes": ["АР"],
            "color": "#198754",
            "description": "Фасады из МДФ в эмали",
            "params": [
                {"name": "thickness", "label": "Толщина", "frontmatter_key": "thickness", "parse_type": "keyword", "parse_rules": ["толщин", "толщина"], "sort_order": 1},
                {"name": "model", "label": "Модель", "frontmatter_key": "model", "parse_type": "keyword", "parse_rules": ["модель"], "sort_order": 2},
                {"name": "s1", "label": "S1", "frontmatter_key": "S1", "parse_type": "manual", "parse_rules": [], "sort_order": 3},
                {"name": "s2", "label": "S2", "frontmatter_key": "S2", "parse_type": "manual", "parse_rules": [], "sort_order": 4},
                {"name": "s3", "label": "S3", "frontmatter_key": "S3", "parse_type": "manual", "parse_rules": [], "sort_order": 5},
            ],
        },
        "KH": {
            "code": "KH",
            "display_name": "Каменные изделия",
            "prefixes": ["КМ"],
            "color": "#ffc107",
            "description": "",
            "params": [],
        },
        "YG": {
            "code": "YG",
            "display_name": "YouGile",
            "prefixes": [],
            "color": "#6c757d",
            "description": "Все заказы из YouGile",
            "params": [],
        },
        "БЕЗ": {
            "code": "БЕЗ",
            "display_name": "Без кода",
            "prefixes": [],
            "color": "#495057",
            "description": "Заказы без определённого кода",
            "params": [],
        },
    },
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


async def get_client_params(owner: str | None = None) -> dict[str, list[dict]] | list[dict] | None:
    """Get per-client parameter definitions.

    Reads from unified `clients` setting first, falls back to `client_params`.
    If owner is given, returns just that client's params (or None).
    If owner is None, returns the full {owner: [params]} dict.
    """
    clients = await get_setting("clients")
    if clients:
        # Derive from unified clients
        cp = {code: c.get("params", []) for code, c in clients.items() if c.get("params")}
        if owner:
            return cp.get(owner)
        return cp
    # Fallback to old client_params
    all_params = await get_setting("client_params")
    if not all_params:
        return None if owner else {}
    if owner:
        return all_params.get(owner)
    return all_params


async def get_clients() -> dict[str, dict]:
    """Get the unified clients config."""
    clients = await get_setting("clients")
    if clients:
        return clients
    # Migrate from old owner_mapping + client_params
    om = await get_setting("owner_mapping") or {}
    cp = await get_setting("client_params") or {}
    result: dict[str, dict] = {}
    # Collect all owner codes
    owner_codes: set[str] = set()
    for folder in om.values():
        owner_codes.add(folder)
    for key in cp:
        owner_codes.add(key)
    for code in sorted(owner_codes):
        prefixes = [p for p, f in om.items() if f == code]
        result[code] = {
            "code": code,
            "display_name": code,
            "prefixes": prefixes,
            "color": "#6c757d",
            "description": "",
            "params": cp.get(code, []),
        }
    return result


async def get_owner_mapping_from_clients() -> dict[str, str]:
    """Derive owner_mapping from unified clients config."""
    clients = await get_setting("clients")
    if clients:
        mapping: dict[str, str] = {}
        for code, c in clients.items():
            for prefix in c.get("prefixes", []):
                mapping[prefix] = code
        return mapping
    return await get_setting("owner_mapping") or {}
