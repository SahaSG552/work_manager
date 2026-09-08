"""MAX → Telegram bridge.

Listens to specified MAX chats via pymax WebClient (QR auth) and forwards
new messages to a Telegram bot's group topic.

Environment variables:
    TG_BOT_TOKEN       — Telegram bot token
    TG_CHAT_ID         — Target Telegram group chat_id (negative for supergroup)
    TG_TOPIC_ID        — Topic/thread ID within the group (optional)
    MAX_SESSION_DIR    — Directory for pymax session files (default: ./sessions)
    MAX_CHATS          — Comma-separated MAX chat IDs to monitor (optional, monitors all if empty)
    MAX_BLACKLIST      — Comma-separated MAX chat IDs to ignore
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0"))
TG_TOPIC_ID = int(os.getenv("TG_TOPIC_ID", "0")) or None
MAX_SESSION_DIR = os.getenv("MAX_SESSION_DIR", "./sessions")
MAX_CHATS = [c.strip() for c in os.getenv("MAX_CHATS", "").split(",") if c.strip()]
MAX_BLACKLIST = set(c.strip() for c in os.getenv("MAX_BLACKLIST", "").split(",") if c.strip())

_MSK = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("max_bridge")

# ── Sent message IDs for dedup ──────────────────────────────────────
_sent_max_ids: set[int] = set()
_STATE_FILE = Path(MAX_SESSION_DIR) / "bridge_state.json"


def _load_state() -> None:
    global _sent_max_ids
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            _sent_max_ids = set(data.get("sent_ids", []))
            log.info(f"Loaded {len(_sent_max_ids)} sent IDs from state")
        except Exception as e:
            log.warning(f"Failed to load state: {e}")


def _save_state() -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ids = list(_sent_max_ids)[-10000:]
    _STATE_FILE.write_text(json.dumps({"sent_ids": ids}, ensure_ascii=False), encoding="utf-8")


# ── Telegram sender ──────────────────────────────────────────────────

_tg_bot = None


async def _get_tg_bot():
    global _tg_bot
    if _tg_bot is None:
        from aiogram import Bot
        _tg_bot = Bot(token=TG_BOT_TOKEN)
    return _tg_bot


async def send_to_telegram(text: str, chat_name: str = "") -> None:
    if not TG_CHAT_ID or not TG_BOT_TOKEN:
        log.error("TG_CHAT_ID or TG_BOT_TOKEN not configured")
        return

    bot = await _get_tg_bot()
    now = datetime.now(_MSK).strftime("%H:%M:%S")
    prefix = f"[MAX {now}]"
    if chat_name:
        prefix = f"[MAX {chat_name} {now}]"

    full_text = f"{prefix}\n{text}"
    if len(full_text) > 4096:
        full_text = full_text[:4090] + "..."

    try:
        kwargs: dict = {"chat_id": TG_CHAT_ID, "text": full_text}
        if TG_TOPIC_ID:
            kwargs["message_thread_id"] = TG_TOPIC_ID
        await bot.send_message(**kwargs)
        log.info(f"Sent to TG: {text[:60]}...")
    except Exception as e:
        log.error(f"Failed to send to Telegram: {e}")


# ── QR handler — sends QR link to Telegram ───────────────────────────

class TelegramQrHandler:
    """Shows QR auth link via Telegram message."""

    async def show_qr(self, qr_url: str) -> None:
        log.info(f"QR auth link: {qr_url}")
        try:
            bot = await _get_tg_bot()
            await bot.send_message(
                chat_id=TG_CHAT_ID,
                text=f"🔐 Подтвердите вход в MAX\n\nОткройте ссылку в браузере, где вы залогинены в MAX:\n\n{qr_url}",
                message_thread_id=TG_TOPIC_ID,
            )
        except Exception as e:
            log.warning(f"Failed to send QR to TG: {e}")


# ── MAX listener ────────────────────────────────────────────────────

async def run_bridge() -> None:
    _load_state()

    try:
        from pymax import WebClient
    except ImportError:
        log.error("pymax not installed. Run: pip install maxapi-python")
        return

    session_path = Path(MAX_SESSION_DIR)
    session_path.mkdir(parents=True, exist_ok=True)

    client = WebClient(
        work_dir=str(session_path),
        session_name="max_bridge.db",
        qr_provider=TelegramQrHandler(),
    )

    @client.on_start()
    async def on_start(cl: WebClient) -> None:
        log.info(f"MAX bridge started! User: {cl.me.contact.id if cl.me else 'unknown'}")

        # Discover and log available chats
        try:
            chats = await cl.fetch_chats()
            for c in chats:
                cid = getattr(c, "id", "?")
                title = getattr(c, "title", "") or getattr(c, "name", "") or str(cid)
                ctype = getattr(c, "type", "?")
                log.info(f"  MAX chat: id={cid}  type={ctype}  title={title}")
        except Exception as e:
            log.warning(f"Failed to list chats: {e}")

    @client.on_message()
    async def on_message(message, cl: WebClient) -> None:
        try:
            msg_id = getattr(message, "id", None) or getattr(message, "msg_id", None)
            if msg_id and msg_id in _sent_max_ids:
                return

            chat_id = getattr(message, "chat_id", None)
            chat_name = ""

            if MAX_CHATS and str(chat_id) not in MAX_CHATS:
                return
            if str(chat_id) in MAX_BLACKLIST:
                return

            try:
                if hasattr(message, "chat") and message.chat:
                    chat_name = getattr(message.chat, "title", "") or getattr(message.chat, "name", "") or ""
            except Exception:
                pass

            text = getattr(message, "text", "") or ""
            sender_name = ""
            try:
                if hasattr(message, "sender") and message.sender:
                    sender_name = getattr(message.sender, "name", "") or getattr(message.sender, "first_name", "") or ""
            except Exception:
                pass

            if not text:
                has_media = getattr(message, "attachments", None) or getattr(message, "media", None)
                if has_media:
                    text = "📎 [вложение]"
                else:
                    return

            parts = []
            if chat_name:
                parts.append(f"💬 {chat_name}")
            if sender_name:
                parts.append(f"👤 {sender_name}")
            parts.append(text)
            full_text = "\n".join(parts)

            await send_to_telegram(full_text, chat_name)

            if msg_id:
                _sent_max_ids.add(msg_id)
                if len(_sent_max_ids) % 50 == 0:
                    _save_state()

        except Exception as e:
            log.error(f"Error processing MAX message: {e}")

    log.info("Starting MAX bridge (WebClient + QR auth)...")
    try:
        await client.start()
    except KeyboardInterrupt:
        log.info("Bridge stopped by user")
    except Exception as e:
        log.error(f"Bridge crashed: {e}")
    finally:
        _save_state()
        if _tg_bot:
            await _tg_bot.session.close()


if __name__ == "__main__":
    asyncio.run(run_bridge())
