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
    get_client_params, get_clients,
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
    owner_override: str = Form(None),
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
        owner_override=owner_override if owner_override else None,
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


@app.get("/api/settings/client_params")
async def api_get_client_params():
    """Get client params definitions."""
    return JSONResponse(await get_client_params())


@app.get("/api/settings/clients")
async def api_get_clients():
    """Get all clients (unified config)."""
    return JSONResponse(await get_clients())


@app.get("/api/clients/list")
async def api_clients_list():
    """Get clients as simple list for dropdowns: [{code, display_name, color}]."""
    clients = await get_clients()
    return JSONResponse([
        {"code": c.get("code", k), "display_name": c.get("display_name", k), "color": c.get("color", "#6c757d")}
        for k, c in clients.items()
    ])


@app.put("/api/settings/clients/{code}")
async def api_save_client(code: str, body: dict):
    """Create or update a client. Body: {display_name, prefixes, color, description, params}"""
    clients = await get_clients()
    client_data = {
        "code": code,
        "display_name": body.get("display_name", code),
        "prefixes": body.get("prefixes", []),
        "color": body.get("color", "#6c757d"),
        "description": body.get("description", ""),
        "params": body.get("params", []),
    }
    clients[code] = client_data
    await set_setting("clients", clients)
    # Also sync owner_mapping and client_params for backward compat
    from app.settings_db import get_setting
    om = {}
    for c_code, c in clients.items():
        for prefix in c.get("prefixes", []):
            om[prefix] = c_code
    await set_setting("owner_mapping", om)
    cp = {c_code: c.get("params", []) for c_code, c in clients.items() if c.get("params")}
    await set_setting("client_params", cp)
    return JSONResponse({"ok": True, "client": client_data})


@app.delete("/api/settings/clients/{code}")
async def api_delete_client(code: str):
    """Delete a client."""
    clients = await get_clients()
    if code not in clients:
        return JSONResponse({"error": "Not found"}, status_code=404)
    del clients[code]
    await set_setting("clients", clients)
    # Sync owner_mapping and client_params
    om = {}
    for c_code, c in clients.items():
        for prefix in c.get("prefixes", []):
            om[prefix] = c_code
    await set_setting("owner_mapping", om)
    cp = {c_code: c.get("params", []) for c_code, c in clients.items() if c.get("params")}
    await set_setting("client_params", cp)
    return JSONResponse({"ok": True})


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


# ── API: MAX Bridge ───────────────────────────────────────────────────


@app.get("/api/max-bridge/status")
async def api_max_bridge_status():
    """Get MAX bridge status."""
    from app.channels.max_bridge import is_max_bridge_running
    return JSONResponse({"running": is_max_bridge_running()})


@app.post("/api/max-bridge/start")
async def api_max_bridge_start():
    """Start MAX bridge."""
    from app.channels.max_bridge import is_max_bridge_running, start_max_bridge
    if is_max_bridge_running():
        return JSONResponse({"status": "already_running"})
    start_max_bridge()
    return JSONResponse({"status": "started"})


@app.post("/api/max-bridge/stop")
async def api_max_bridge_stop():
    """Stop MAX bridge."""
    from app.channels.max_bridge import stop_max_bridge
    stop_max_bridge()
    return JSONResponse({"status": "stopped"})


# Mount static files AFTER all routes
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
