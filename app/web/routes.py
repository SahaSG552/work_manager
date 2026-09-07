"""FastAPI web application — UI and API endpoints."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import FileSystemLoader, Environment

from app.config import settings
from app.database import (
    init_db, get_orders, get_order_by_code, get_order,
    count_orders, update_order, confirm_order, get_history,
    delete_order, clear_all_orders,
)
from app.log_stream import install_log_handler, log_stream
from app.models import Source, OrderStatus, OrderUpdate, ServiceCheckResult
from app.processor import process_message, create_pending_order, confirm_and_process
from app.scanner import scan_yougile, scan_email, scan_telegram
from app.scheduler import start_scheduler, stop_scheduler, is_running
from app.settings_db import (
    get_all_settings, set_setting, set_settings_batch, reset_setting,
)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent

app = FastAPI(title="Work Manager", docs_url=None, redoc_url=None)
_jinja_env = Environment(
    loader=FileSystemLoader(str(BASE_DIR / "templates")),
    autoescape=True,
)


@app.on_event("startup")
async def startup() -> None:
    await init_db()
    install_log_handler()
    log.info("Database initialized, log streaming active")


# ── Helpers ──────────────────────────────────────────────────────────

def _render(template_name: str, **kwargs) -> HTMLResponse:
    template = _jinja_env.get_template(template_name)
    html = template.render(**kwargs)
    return HTMLResponse(html)


# ── Pages ────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def page_index(request: Request):
    """Dashboard — moderation queue + recent orders."""
    pending = await get_orders(limit=50, status="pending")
    recent = await get_orders(limit=50)
    total = await count_orders()
    pending_count = await count_orders(status="pending")

    return _render(
        "index.html",
        pending=pending,
        orders=recent,
        total=total,
        pending_count=pending_count,
        auto_mode=is_running(),
        vault_path=str(settings.vault_path),
    )


@app.get("/order/{order_id}", response_class=HTMLResponse)
async def page_order_detail(order_id: int, request: Request):
    """Order detail / edit page."""
    order = await get_order(order_id)
    if not order:
        return HTMLResponse("<h1>Заказ не найден</h1>", status_code=404)

    history = await get_history(order_id)
    return _render("order_detail.html", order=order, history=history)


@app.get("/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    """Settings page — clients, tags, parameters, scanner."""
    all_settings = await get_all_settings()
    return _render("settings.html", settings=all_settings, auto_mode=is_running())


@app.get("/new", response_class=HTMLResponse)
async def page_new_task(request: Request):
    """Manual entry form."""
    return _render("new_task.html")


# ── SSE Log Stream ────────────────────────────────────────────────────


@app.get("/api/logs/stream")
async def api_log_stream(request: Request):
    """SSE endpoint for real-time log streaming to the web UI."""
    return await log_stream(request)


@app.get("/api/logs/history")
async def api_log_history():
    """Get recent buffered log entries."""
    from app.log_stream import _log_buffer
    import json
    return JSONResponse([json.loads(e) if isinstance(e, str) else e for e in _log_buffer])


# ── API: Orders ───────────────────────────────────────────────────────


@app.post("/api/process")
async def api_process(
    text: str = Form(...),
    source: str = Form("manual"),
    files: list[UploadFile] = File(default=[]),
):
    """Process a message: parse → create pending order."""
    attachments: list[tuple[str, bytes]] = []
    for f in files:
        if f.filename:
            attachments.append((f.filename, await f.read()))

    src = Source(source)
    results = await process_message(
        text=text,
        source=src,
        attachments=attachments if attachments else None,
    )
    return JSONResponse([r.model_dump(mode="json") for r in results])


@app.post("/api/parse")
async def api_parse_only(text: str = Form(...)):
    """Parse a message without creating anything (preview)."""
    import asyncio
    from app.parser import parse_message
    parsed = await asyncio.to_thread(parse_message, text)
    return JSONResponse(parsed.model_dump(mode="json"))


@app.get("/api/orders")
async def api_orders(limit: int = 50, status: str | None = None):
    """Get recent orders as JSON."""
    orders = await get_orders(limit=limit, status=status)
    return JSONResponse([o.model_dump(mode="json") for o in orders])


@app.get("/api/orders/{order_id}")
async def api_order_detail(order_id: int):
    """Get a specific order."""
    order = await get_order(order_id)
    if not order:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(order.model_dump(mode="json"))


@app.put("/api/orders/{order_id}")
async def api_update_order(order_id: int, update: OrderUpdate):
    """Update order fields."""
    result = await update_order(order_id, update)
    if not result:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(result.model_dump(mode="json"))


@app.post("/api/orders/{order_id}/confirm")
async def api_confirm_order(order_id: int, edits: OrderUpdate | None = None):
    """Confirm a pending order — creates Obsidian note and working folders."""
    try:
        result = await confirm_and_process(order_id, edits)
        return JSONResponse(result.model_dump(mode="json"))
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/orders/{order_id}/reject")
async def api_reject_order(order_id: int):
    """Reject a pending order — set status to archived."""
    update = OrderUpdate(status=OrderStatus.ARCHIVED)
    result = await update_order(order_id, update)
    if not result:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse(result.model_dump(mode="json"))


@app.delete("/api/orders/{order_id}")
async def api_delete_order(order_id: int):
    """Delete order from DB only (no Obsidian/folder deletion)."""
    deleted = await delete_order(order_id)
    if not deleted:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return JSONResponse({"deleted": True})


@app.post("/api/clear-orders")
async def api_clear_orders():
    """Delete ALL orders from DB (no Obsidian/folder deletion)."""
    count = await clear_all_orders()
    return JSONResponse({"deleted": count})


@app.get("/api/orders/{order_id}/history")
async def api_order_history(order_id: int):
    """Get change history for an order."""
    history = await get_history(order_id)
    return JSONResponse([h.model_dump(mode="json") for h in history])


# ── API: Service scanning ────────────────────────────────────────────


@app.post("/api/scan/telegram")
async def api_scan_telegram():
    """Manual Telegram scan."""
    result = await scan_telegram()
    return JSONResponse(result.model_dump(mode="json"))


@app.post("/api/scan/email")
async def api_scan_email():
    """Manual email scan."""
    result = await scan_email()
    # Auto-create pending orders for found emails
    created = 0
    for item in result.orders:
        try:
            code = item.get("order_code", "")
            subject = item.get("subject", "")
            text = f"{code} {subject}" if code else subject
            if text.strip():
                await process_message(text=text, source=Source.EMAIL)
                created += 1
        except Exception as e:
            log.error(f"Failed to process email: {e}")
    return JSONResponse({**result.model_dump(mode="json"), "created": created})


@app.post("/api/scan/yougile")
async def api_scan_yougile():
    """Manual YouGile scan — processes all rules (column_watch + keyword_filter)."""
    result = await scan_yougile()
    # Create pending orders for found tasks + download attachments
    created = 0
    for item in result.orders:
        try:
            combined_text = item.get("combined_text", "") or item.get("title", "")
            title = item.get("title", "")
            if not combined_text.strip():
                continue

            # Download file attachments from YouGile
            attachments: list[tuple[str, bytes]] = []
            from app.yougile import download_file
            for file_url in item.get("file_urls", []):
                try:
                    downloaded = await download_file(file_url)
                    if downloaded:
                        file_bytes, filename = downloaded
                        attachments.append((filename, file_bytes))
                except Exception as e:
                    log.error(f"Failed to download YouGile file: {e}")

            results = await process_message(
                text=combined_text,
                source=Source.YOUGILE,
                attachments=attachments if attachments else None,
                order_code_override=title if title else None,
                yougile_task_id=item.get("id"),
            )
            created += len(results)
        except Exception as e:
            log.error(f"Failed to process YouGile task: {e}")
    return JSONResponse({**result.model_dump(mode="json"), "created": created})


# ── API: Auto mode ───────────────────────────────────────────────────


@app.post("/api/auto/start")
async def api_auto_start():
    """Start auto-mode scheduler."""
    if is_running():
        return JSONResponse({"status": "already_running"})
    start_scheduler()
    return JSONResponse({"status": "started"})


@app.post("/api/auto/stop")
async def api_auto_stop():
    """Stop auto-mode scheduler."""
    stop_scheduler()
    return JSONResponse({"status": "stopped"})


@app.get("/api/auto/status")
async def api_auto_status():
    """Get auto-mode status."""
    return JSONResponse({"running": is_running()})


# ── API: Email per-order ─────────────────────────────────────────────


@app.post("/api/orders/{order_id}/check-email")
async def api_check_email_for_order(order_id: int):
    """Check email for attachments related to this order."""
    order = await get_order(order_id)
    if not order:
        return JSONResponse({"error": "Not found"}, status_code=404)

    try:
        from app.channels.email_checker import EmailChecker
        checker = EmailChecker()
        import asyncio
        loop = asyncio.get_event_loop()
        attachments = await loop.run_in_executor(
            None, lambda: checker.find_and_download_by_order_code(order.order_code)
        )
        return JSONResponse({"found": len(attachments)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: Settings ───────────────────────────────────────────────────────


@app.get("/api/settings")
async def api_get_settings():
    """Get all settings."""
    return JSONResponse(await get_all_settings())


@app.put("/api/settings/{key}")
async def api_set_setting(key: str, body: dict):
    """Set a single setting value."""
    value = body.get("value")
    if value is None:
        return JSONResponse({"error": "missing 'value'"}, status_code=400)
    await set_setting(key, value)
    return JSONResponse({"ok": True})


@app.post("/api/settings/batch")
async def api_set_settings_batch(body: dict):
    """Set multiple settings at once. Body: {key: value, ...}"""
    await set_settings_batch(body)
    return JSONResponse({"ok": True})


@app.delete("/api/settings/{key}")
async def api_reset_setting(key: str):
    """Reset a setting to default (remove DB override)."""
    await reset_setting(key)
    return JSONResponse({"ok": True})


# ── API: YouGile board/column picker ────────────────────────────────────


@app.get("/api/yougile/boards")
async def api_yougile_boards():
    """List all YouGile boards with their columns."""
    try:
        from app.yougile import get_boards, get_columns
        boards = await get_boards()
        result = []
        for b in boards:
            board_id = b.get("id", "")
            board_title = b.get("title", "")
            columns = await get_columns(board_id)
            result.append({
                "id": board_id,
                "title": board_title,
                "columns": [{"id": c.get("id", ""), "title": c.get("title", "")} for c in columns],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/yougile/stickers")
async def api_yougile_stickers(board_id: str | None = None):
    """List string stickers with their states (for dropdown population)."""
    try:
        from app.yougile import get_string_stickers
        stickers = await get_string_stickers(board_id=board_id)
        result = []
        for s in stickers:
            result.append({
                "id": s.get("id", ""),
                "name": s.get("name", ""),
                "states": [
                    {"id": st.get("id", ""), "name": st.get("name", ""), "color": st.get("color", "")}
                    for st in s.get("states", [])
                ],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/yougile/users")
async def api_yougile_users():
    """List all YouGile users (for assigned filter)."""
    try:
        from app.yougile import get_users
        users = await get_users()
        return JSONResponse(users)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: YouGile blacklist ─────────────────────────────────────────────


@app.get("/api/yougile/blacklist")
async def api_yougile_blacklist():
    """Get YouGile blacklist task IDs."""
    from app.settings_db import get_yougile_blacklist
    bl = await get_yougile_blacklist()
    return JSONResponse(bl)


@app.post("/api/yougile/blacklist")
async def api_yougile_blacklist_add(body: dict):
    """Add a task ID to the YouGile blacklist. Body: {task_id: "..."}"""
    task_id = body.get("task_id", "")
    if not task_id:
        return JSONResponse({"error": "missing task_id"}, status_code=400)
    from app.settings_db import get_setting, set_setting
    bl = await get_setting("yougile_blacklist") or []
    if task_id not in bl:
        bl.append(task_id)
        await set_setting("yougile_blacklist", bl)
    return JSONResponse({"ok": True, "blacklist": bl})


@app.delete("/api/yougile/blacklist/{task_id}")
async def api_yougile_blacklist_remove(task_id: str):
    """Remove a task ID from the YouGile blacklist."""
    from app.settings_db import get_setting, set_setting
    bl = await get_setting("yougile_blacklist") or []
    if task_id in bl:
        bl.remove(task_id)
        await set_setting("yougile_blacklist", bl)
    return JSONResponse({"ok": True, "blacklist": bl})


# Mount static files AFTER all routes
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
