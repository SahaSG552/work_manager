"""MAX → Telegram bridge channel.

Listens to MAX chats via pymax WebClient and forwards messages
(media included) to a Telegram group topic.

Settings (from settings_db, key "max_bridge"):
    enabled: bool           — start bridge on boot
    chats: list[str]        — MAX chat IDs to monitor (empty = all)
    blacklist_users: list   — MAX user IDs to ignore (e.g. own account)
    tg_chat_id: int         — target Telegram group
    tg_topic_id: int|None   — topic/thread ID within the group
    session_token: str      — browser token for auth (injected into session DB)
    session_dir: str        — directory for pymax session files
"""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import aiohttp
from aiogram import Bot
from aiogram.types import FSInputFile

from app.config import settings as app_settings
from app.settings_db import get_setting

log = logging.getLogger(__name__)

_MSK = timezone(timedelta(hours=3))


# ── Dedup state ──────────────────────────────────────────────────────

_sent_ids: set[int] = set()
_STATE_FILE = Path(tempfile.gettempdir()) / "max_bridge_state.json"


def _load_state() -> None:
    global _sent_ids
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            _sent_ids = set(data.get("sent_ids", []))
            log.info("Loaded %d sent IDs from state", len(_sent_ids))
        except Exception as e:
            log.warning("Failed to load bridge state: %s", e)


def _save_state() -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ids = list(_sent_ids)[-10000:]
    _STATE_FILE.write_text(json.dumps({"sent_ids": ids}), encoding="utf-8")


# ── Cached config ────────────────────────────────────────────────────

_cached_cfg: dict | None = None


async def _cfg() -> dict:
    """Load bridge config once per run (cached)."""
    global _cached_cfg
    if _cached_cfg is not None:
        return _cached_cfg
    from app.settings_db import DEFAULTS
    base = dict(DEFAULTS.get("max_bridge", {}))
    db_val = await get_setting("max_bridge")
    if db_val:
        base.update(db_val)
    _cached_cfg = base
    return _cached_cfg


def invalidate_cfg() -> None:
    """Clear cached config so next read picks up DB changes."""
    global _cached_cfg
    _cached_cfg = None


# ── Telegram sender ──────────────────────────────────────────────────

_tg_bot: Bot | None = None


async def _get_bot() -> Bot:
    global _tg_bot
    if _tg_bot is None:
        _tg_bot = Bot(token=app_settings.tg_bot_token)
    return _tg_bot


async def _tg_kwargs() -> dict:
    """Common kwargs for all TG sends: chat_id + optional topic."""
    c = await _cfg()
    kwargs: dict = {"chat_id": c["tg_chat_id"]}
    if c.get("tg_topic_id"):
        kwargs["message_thread_id"] = c["tg_topic_id"]
    return kwargs


async def _send_text(text: str, chat_name: str = "") -> None:
    c = await _cfg()
    if not c.get("tg_chat_id"):
        return
    bot = await _get_bot()
    now = datetime.now(_MSK).strftime("%H:%M:%S")
    prefix = f"[MAX {chat_name} {now}]" if chat_name else f"[MAX {now}]"
    full_text = f"{prefix}\n{text}"
    if len(full_text) > 4096:
        full_text = full_text[:4090] + "..."
    kwargs = await _tg_kwargs()
    kwargs["text"] = full_text
    await bot.send_message(**kwargs)


async def _send_media(
    data: bytes, filename: str, caption: str = "", *, is_photo: bool = False,
) -> None:
    """Send a file or photo to TG. Writes to temp file for aiogram FSInputFile."""
    c = await _cfg()
    if not c.get("tg_chat_id"):
        return
    bot = await _get_bot()
    with tempfile.NamedTemporaryFile(suffix=f"_{filename}", delete=False) as f:
        f.write(data)
        path = f.name
    try:
        media = FSInputFile(path, filename=filename)
        kwargs = await _tg_kwargs()
        if is_photo:
            kwargs["photo"] = media
            if caption:
                kwargs["caption"] = caption[:1024]
            await bot.send_photo(**kwargs)
        else:
            kwargs["document"] = media
            if caption:
                kwargs["caption"] = caption[:1024]
            await bot.send_document(**kwargs)
    finally:
        Path(path).unlink(missing_ok=True)


# ── Media download ────────────────────────────────────────────────────


async def _download_url(url: str) -> bytes | None:
    """Download bytes from a URL."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                return await resp.read() if resp.status == 200 else None
    except Exception as e:
        log.warning("Download failed %s: %s", url[:80], e)
        return None


async def _download_photo(photo_att) -> bytes | None:
    url = getattr(photo_att, "base_url", "")
    token = getattr(photo_att, "photo_token", "")
    if not url or not token:
        return None
    return await _download_url(f"{url}{token}")


async def _download_file(client, message, file_att) -> tuple[bytes, str] | None:
    chat_id = getattr(message, "chat_id", None)
    msg_id = getattr(message, "id", None)
    file_id = getattr(file_att, "file_id", None)
    if not all([chat_id, msg_id, file_id]):
        return None
    file_info = await client.get_file_by_id(chat_id, msg_id, file_id)
    if not file_info:
        return None
    url = getattr(file_info, "url", "")
    if not url:
        return None
    data = await _download_url(url)
    return (data, getattr(file_att, "name", "file")) if data else None


async def _download_video(client, message, video_att) -> tuple[bytes, str] | None:
    chat_id = getattr(message, "chat_id", None)
    msg_id = getattr(message, "id", None)
    video_id = getattr(video_att, "video_id", None)
    if not all([chat_id, msg_id, video_id]):
        return None
    video_info = await client.get_video_by_id(chat_id, msg_id, video_id)
    if not video_info:
        return None
    url = getattr(video_info, "url", "")
    if not url:
        return None
    data = await _download_url(url)
    return (data, "video.mp4") if data else None


# ── Bridge runner ────────────────────────────────────────────────────

_bridge_task: asyncio.Task | None = None
_running = False


async def _run_bridge() -> None:
    global _running
    _running = True
    _load_state()

    try:
        from pymax import WebClient
    except ImportError:
        log.error("pymax not installed. Run: pip install maxapi-python")
        _running = False
        return

    cfg = await _cfg()
    session_dir = cfg.get("session_dir", "./max_sessions")
    session_token = cfg.get("session_token", "")
    chat_whitelist = set(str(c) for c in cfg.get("chats", []))
    blacklist_users = set(str(u) for u in cfg.get("blacklist_users", []))

    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)

    # Inject browser token into session DB if provided and no session exists
    if session_token:
        try:
            from pymax.session.store import SessionStore
            from pymax.session.models import SessionInfo
            from pymax.types.domain.sync import SyncState, DEFAULT_CONFIG_HASH

            store = SessionStore(str(session_path), "max_bridge.db")
            existing = await store.load_session()
            if not existing:
                await store.save_session(SessionInfo(
                    token=session_token,
                    device_id="web_browser",
                    phone="+995555386066",
                    mt_instance_id="",
                    user_agent=None,
                    sync=SyncState(
                        chats_sync=-1, contacts_sync=-1, drafts_sync=-1,
                        presence_sync=-1, config_hash=DEFAULT_CONFIG_HASH,
                    ),
                ))
                log.info("Injected browser token into session DB")
            await store.close()
        except Exception as e:
            log.warning("Failed to inject session token: %s", e)

    client = WebClient(
        work_dir=str(session_path),
        session_name="max_bridge.db",
    )

    @client.on_start()
    async def on_start(cl: WebClient) -> None:
        me_id = cl.me.contact.id if cl.me else "unknown"
        log.info("MAX bridge started! User: %s", me_id)
        try:
            chats = await cl.fetch_chats()
            for c in chats:
                cid = getattr(c, "id", "?")
                title = getattr(c, "title", "") or getattr(c, "name", "") or str(cid)
                ctype = getattr(c, "type", "?")
                log.info("  MAX chat: id=%s  type=%s  title=%s", cid, ctype, title)
        except Exception as e:
            log.warning("Failed to list chats: %s", e)

    @client.on_message()
    async def on_message(message, cl: WebClient) -> None:
        try:
            msg_id = getattr(message, "id", None)
            if msg_id and msg_id in _sent_ids:
                return

            chat_id = getattr(message, "chat_id", None)
            if chat_whitelist and str(chat_id) not in chat_whitelist:
                return

            sender_id = getattr(message, "sender", None)
            if sender_id and str(sender_id) in blacklist_users:
                return

            text = getattr(message, "text", "") or ""

            # Process attachments — send media with text as caption
            attaches = getattr(message, "attaches", None) or []
            for att in attaches:
                att_type = getattr(att, "type", "")

                if att_type == "PHOTO":
                    photo_bytes = await _download_photo(att)
                    if photo_bytes:
                        await _send_media(photo_bytes, "photo.jpg", caption=text, is_photo=True)
                        text = ""
                    elif not text:
                        text = "📷 [фото]"

                elif att_type == "FILE":
                    result = await _download_file(cl, message, att)
                    if result:
                        file_bytes, filename = result
                        await _send_media(file_bytes, filename, caption=text)
                        text = ""
                    elif not text:
                        text = f"📎 [{getattr(att, 'name', 'файл')}]"

                elif att_type == "VIDEO":
                    result = await _download_video(cl, message, att)
                    if result:
                        video_bytes, filename = result
                        await _send_media(video_bytes, filename, caption=text)
                        text = ""
                    elif not text:
                        text = "🎬 [видео]"

            if text.strip():
                await _send_text(text)

            if msg_id:
                _sent_ids.add(msg_id)
                if len(_sent_ids) % 50 == 0:
                    _save_state()

        except Exception as e:
            log.error("Error processing MAX message: %s", e)

    log.info("Starting MAX bridge (WebClient)...")
    try:
        await client.start()
    except Exception as e:
        log.error("MAX bridge crashed: %s", e)
    finally:
        _save_state()
        _running = False


def start_max_bridge() -> asyncio.Task:
    global _bridge_task
    invalidate_cfg()  # pick up fresh settings on each start
    _bridge_task = asyncio.create_task(_run_bridge())
    log.info("MAX bridge task created")
    return _bridge_task


def stop_max_bridge() -> None:
    global _running
    _running = False
    if _bridge_task and not _bridge_task.done():
        _bridge_task.cancel()
    log.info("MAX bridge stopped")


def is_max_bridge_running() -> bool:
    return _running
