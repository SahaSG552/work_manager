"""SQLite database for order tracking."""

from __future__ import annotations

import aiosqlite
import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

from app.models import OrderRecord, Source, OrderStatus, HistoryEntry, OrderUpdate

DB_PATH = Path(__file__).parent / "db" / "orders.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT NOT NULL,
    owner TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    source TEXT NOT NULL DEFAULT 'manual',
    raw_text TEXT NOT NULL DEFAULT '',
    thickness TEXT,
    sink_type TEXT,
    edge_type TEXT,
    stone_amount TEXT,
    urgency TEXT,
    email_reference INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '[]',
    obsidian_path TEXT,
    attachment_dir TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    changed_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

CREATE INDEX IF NOT EXISTS idx_order_code ON orders(order_code);
CREATE INDEX IF NOT EXISTS idx_owner ON orders(owner);
CREATE INDEX IF NOT EXISTS idx_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_history_order ON history(order_id);
"""

_MIGRATIONS = [
    """ALTER TABLE orders ADD COLUMN tags TEXT NOT NULL DEFAULT '[]'""",
    """ALTER TABLE orders ADD COLUMN folder_name TEXT""",
    """ALTER TABLE orders ADD COLUMN yougile_task_id TEXT""",
    """ALTER TABLE orders ADD COLUMN custom_fields TEXT""",
]


async def init_db() -> None:
    """Create database and tables if they don't exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()
        # Run migrations (ignore errors if column already exists)
        for mig in _MIGRATIONS:
            try:
                await db.execute(mig)
                await db.commit()
            except aiosqlite.OperationalError:
                pass


async def insert_order(record: OrderRecord) -> int:
    """Insert a new order record. Returns the row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """INSERT INTO orders
               (order_code, owner, description, status, source, raw_text,
                thickness, sink_type, edge_type, stone_amount, urgency,
                email_reference, tags, obsidian_path, attachment_dir, folder_name,
                yougile_task_id, custom_fields, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.order_code,
                record.owner,
                record.description,
                record.status.value,
                record.source.value,
                record.raw_text,
                record.thickness,
                record.sink_type,
                record.edge_type,
                record.stone_amount,
                record.urgency,
                int(record.email_reference),
                json.dumps(record.tags, ensure_ascii=False),
                record.obsidian_path,
                record.attachment_dir,
                record.folder_name,
                record.yougile_task_id,
                json.dumps(record.custom_fields, ensure_ascii=False) if record.custom_fields else None,
                record.created_at.isoformat(),
                record.updated_at.isoformat(),
            ),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore


async def get_orders(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
) -> list[OrderRecord]:
    """Get recent orders, newest first. Optional status filter."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if status:
            cursor = await db.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cursor = await db.execute(
                "SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [_row_to_record(r) for r in rows]


async def get_order(order_id: int) -> OrderRecord | None:
    """Get an order by its row id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        return _row_to_record(row) if row else None


async def get_order_by_code(order_code: str) -> OrderRecord | None:
    """Find an order by its code."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE order_code = ? ORDER BY id DESC LIMIT 1",
            (order_code,),
        )
        row = await cursor.fetchone()
        return _row_to_record(row) if row else None


async def update_order(order_id: int, update: OrderUpdate) -> OrderRecord | None:
    """Update an order's fields. Records changes in history."""
    existing = await get_order(order_id)
    if not existing:
        return None

    fields = update.model_dump(exclude_none=True)
    if not fields:
        return existing

    sets: list[str] = []
    vals: list = []
    now = datetime.now().isoformat()

    for key, value in fields.items():
        if key == "tags":
            value = json.dumps(value, ensure_ascii=False)
        elif key == "custom_fields":
            value = json.dumps(value, ensure_ascii=False) if value else None
        sets.append(f"{key} = ?")
        vals.append(value)

    sets.append("updated_at = ?")
    vals.append(now)
    vals.append(order_id)

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE orders SET {', '.join(sets)} WHERE id = ?",
            vals,
        )
        # Record history
        for key, new_val in fields.items():
            old_val = getattr(existing, key, None)
            old_str = json.dumps(old_val, ensure_ascii=False) if isinstance(old_val, list) else str(old_val or "")
            new_str = json.dumps(new_val, ensure_ascii=False) if isinstance(new_val, list) else str(new_val)
            if old_str != new_str:
                await db.execute(
                    """INSERT INTO history (order_id, field, old_value, new_value, changed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (order_id, key, old_str, new_str, now),
                )
        await db.commit()

    return await get_order(order_id)


async def update_order_paths(order_code: str, obsidian_path: str | None, attachment_dir: str | None) -> None:
    """Update the Obsidian path and attachment dir for an order."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE orders SET obsidian_path = ?, attachment_dir = ?, updated_at = ? WHERE order_code = ?",
            (obsidian_path, attachment_dir, datetime.now().isoformat(), order_code),
        )
        await db.commit()


async def confirm_order(order_id: int) -> OrderRecord | None:
    """Move order from pending to confirmed (create Obsidian note + folders)."""
    existing = await get_order(order_id)
    if not existing or existing.status != OrderStatus.PENDING:
        return None

    update = OrderUpdate(status=OrderStatus.IN_PROGRESS)
    return await update_order(order_id, update)


async def get_history(order_id: int) -> list[HistoryEntry]:
    """Get change history for an order."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM history WHERE order_id = ? ORDER BY changed_at DESC",
            (order_id,),
        )
        rows = await cursor.fetchall()
        return [
            HistoryEntry(
                id=r["id"],
                order_id=r["order_id"],
                field=r["field"],
                old_value=r["old_value"],
                new_value=r["new_value"],
                changed_at=datetime.fromisoformat(r["changed_at"]),
            )
            for r in rows
        ]


async def count_orders(status: str | None = None) -> int:
    """Count orders, optionally filtered by status."""
    async with aiosqlite.connect(DB_PATH) as db:
        if status:
            cursor = await db.execute("SELECT COUNT(*) FROM orders WHERE status = ?", (status,))
        else:
            cursor = await db.execute("SELECT COUNT(*) FROM orders")
        row = await cursor.fetchone()
        return row[0]


async def delete_order(order_id: int) -> bool:
    """Delete an order from DB only (no Obsidian/folder deletion). Returns True if deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        await db.execute("DELETE FROM history WHERE order_id = ?", (order_id,))
        await db.commit()
        return cursor.rowcount > 0


async def clear_all_orders() -> int:
    """Delete all orders from DB (no Obsidian/folder deletion). Returns count deleted."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM orders")
        await db.execute("DELETE FROM history")
        await db.commit()
        return cursor.rowcount


def _row_to_record(row: aiosqlite.Row) -> OrderRecord:
    tags_raw = row["tags"] if "tags" in row.keys() else "[]"
    tags = json.loads(tags_raw) if isinstance(tags_raw, str) else []
    folder_name = row["folder_name"] if "folder_name" in row.keys() else None
    yougile_task_id = row["yougile_task_id"] if "yougile_task_id" in row.keys() else None
    custom_fields_raw = row["custom_fields"] if "custom_fields" in row.keys() else None
    custom_fields = json.loads(custom_fields_raw) if custom_fields_raw else None
    return OrderRecord(
        id=row["id"],
        order_code=row["order_code"],
        owner=row["owner"],
        description=row["description"],
        status=OrderStatus(row["status"]),
        source=Source(row["source"]),
        raw_text=row["raw_text"],
        thickness=row["thickness"],
        sink_type=row["sink_type"],
        edge_type=row["edge_type"],
        stone_amount=row["stone_amount"],
        urgency=row["urgency"],
        email_reference=bool(row["email_reference"]),
        tags=tags,
        folder_name=folder_name,
        yougile_task_id=yougile_task_id,
        custom_fields=custom_fields,
        obsidian_path=row["obsidian_path"],
        attachment_dir=row["attachment_dir"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
