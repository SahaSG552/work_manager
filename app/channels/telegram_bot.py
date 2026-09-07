"""Telegram bot handler using aiogram v3."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, ChatMemberUpdatedFilter
from aiogram.types import FSInputFile, Message, Document, PhotoSize
from aiogram.client.default import DefaultBotProperties

from app.config import settings
from app.models import Source
from app.processor import process_message, create_pending_order

log = logging.getLogger(__name__)

# ── Bot instance ─────────────────────────────────────────────────────

bot = Bot(
    token=settings.tg_bot_token,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()

# ── Temp storage for attachments pending reconciliation ──────────────

# {chat_id: {order_code: [(filename, path)]}}
_pending_attachments: dict[int, dict[str, list[tuple[str, Path]]]] = {}

# ── Topic name cache: {chat_id: {thread_id: topic_name}} ──────────────

_topic_names: dict[int, dict[int, str]] = {}


def _get_topic_owner(message: Message) -> str | None:
    """Get owner code from the topic name in a forum group.

    Topics are named after client codes (KK, KH, AM, etc).
    Returns the topic name if in a forum topic, None otherwise.
    """
    if not message.message_thread_id:
        return None
    chat_id = message.chat.id
    names = _topic_names.get(chat_id, {})
    return names.get(message.message_thread_id)


def _is_allowed(message: Message) -> bool:
    """Check if message should be processed.

    In private chat: only from admin.
    In group/supergroup: all messages (bot is admin, privacy mode off).
    """
    if message.chat.type == "private":
        return message.from_user and message.from_user.id == settings.tg_admin_id
    return True  # group chats — process all


def _get_order_code_from_text(text: str) -> str | None:
    """Quick regex to find order code in message text."""
    import re
    match = re.search(r"([А-ЯA-Z]{1,5}-\d{3,5})", text)
    return match.group(1) if match else None


def _reply_in_topic(message: Message, text: str) -> types.FSInputFile | None:
    """Helper to reply in the same topic/thread if applicable."""
    # Returns kwargs for message.reply — aiogram handles topic auto-reply
    return None


async def _download_and_process(
    message: Message,
    text: str,
    file_paths: list[tuple[str, Path]],
    source: Source = Source.TELEGRAM,
    owner_override: str | None = None,
) -> None:
    """Download any pending files, process the message, and reply."""
    attachments: list[tuple[str, bytes]] = []
    for filename, path in file_paths:
        attachments.append((filename, path.read_bytes()))

    # If owner comes from topic name, pass it as owner_override to processor
    log.info(f"[telegram] Processing: text={text[:60]}... owner={owner_override}, attachments={len(attachments)}, chat_id={message.chat.id}")

    try:
        from app.channels.email_checker import fetch_email_attachments
        results = await process_message(
            text=text,
            source=source,
            attachments=attachments if attachments else None,
            email_lookup_fn=fetch_email_attachments if "почт" in text.lower() else None,
            owner_override=owner_override,
        )

        # process_message returns list[ProcessResponse] (multi-order support)
        for result in results:
            log.info(f"[telegram] Result: code={result.order.order_code}, owner={result.order.owner}, obsidian={'✓' if result.obsidian_path else '✗'}, att_dir={'✓' if result.attachment_dir else 'pending'}, email_att={result.email_attachments_found}")
            reply = (
                f"✅ <b>Заказ {result.order.order_code}</b>\n"
                f"📁 Заказчик: {result.order.owner}\n"
                f"📝 {result.order.description[:100]}\n"
            )
            if result.order.thickness:
                reply += f"📏 Толщина: {result.order.thickness}\n"
            if result.order.sink_type:
                reply += f"🚰 Мойка: {result.order.sink_type}\n"
            if result.email_attachments_found:
                reply += f"📧 Вложений из почты: {result.email_attachments_found}\n"
            if result.obsidian_path:
                reply += f"📋 Obsidian: создано\n"
            else:
                reply += f"⏳ На модерации — подтвердите в веб-интерфейсе\n"

            await message.reply(reply)
    except Exception as e:
        log.error(f"Failed to process message: {e}", exc_info=True)
        await message.reply(f"❌ Ошибка обработки: {e}")


# ── Handlers ─────────────────────────────────────────────────────────


@dp.message(F.forum_topic_created)
async def handle_topic_created(message: Message) -> None:
    """Cache topic names when they're created or seen."""
    if not message.message_thread_id or not message.forum_topic_created:
        return
    chat_id = message.chat.id
    name = message.forum_topic_created.name
    if chat_id not in _topic_names:
        _topic_names[chat_id] = {}
    _topic_names[chat_id][message.message_thread_id] = name
    log.info(f"Cached topic: chat={chat_id} thread={message.message_thread_id} name={name}")


@dp.message(F.forum_topic_edited)
async def handle_topic_edited(message: Message) -> None:
    """Update cached topic names when renamed."""
    if not message.message_thread_id or not message.forum_topic_edited:
        return
    chat_id = message.chat.id
    name = message.forum_topic_edited.name
    if chat_id not in _topic_names:
        _topic_names[chat_id] = {}
    if name:
        _topic_names[chat_id][message.message_thread_id] = name
        log.info(f"Updated topic: chat={chat_id} thread={message.message_thread_id} name={name}")


@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    if not _is_allowed(message):
        return
    await message.reply(
        "👋 Привет! Я бот для учёта заказов.\n\n"
        "Просто отправь или перешли мне сообщение с заказом — я его распарсю "
        "и создам задачу на модерации.\n\n"
        "Можно отправлять текст, фото и документы."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not _is_allowed(message):
        return
    await message.reply(
        "📋 <b>Как пользоваться:</b>\n\n"
        "1. Перешли сообщение с заказом\n"
        "2. Или напиши текст заказа прямо тут\n"
        "3. Прикрепи фото/чертежи — они сохранятся в папку заказа\n\n"
        "Я распарсю код заказа, параметры и создам заказ на модерации."
    )


@dp.message(F.document)
async def handle_document(message: Message) -> None:
    """Handle document attachments (PDF, DWG, etc.)."""
    if not _is_allowed(message):
        return

    doc: Document = message.document
    text = message.caption or message.text or ""
    owner = _get_topic_owner(message)

    # Download file
    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = Path(tmp_dir) / (doc.file_name or f"file_{doc.file_id}")
        await bot.download(file=doc.file_id, destination=file_path)

        # If there's text with order code — process immediately
        if text and _get_order_code_from_text(text):
            await _download_and_process(message, text, [(doc.file_name or "file", file_path)], owner_override=owner)
        else:
            # Store as pending attachment
            order_code = _get_order_code_from_text(doc.file_name or "")
            if order_code:
                chat_id = message.chat.id
                if chat_id not in _pending_attachments:
                    _pending_attachments[chat_id] = {}
                if order_code not in _pending_attachments[chat_id]:
                    _pending_attachments[chat_id][order_code] = []
                # Copy to a persistent temp location
                persist_dir = Path(tempfile.gettempdir()) / "work_manager" / str(message.message_id)
                persist_dir.mkdir(parents=True, exist_ok=True)
                persist_path = persist_dir / (doc.file_name or "file")
                persist_path.write_bytes(file_path.read_bytes())
                _pending_attachments[chat_id][order_code].append((doc.file_name or "file", persist_path))
                await message.reply(f"📎 Файл «{doc.file_name}» привязан к заказу {order_code}")
            else:
                await message.reply(
                    "📎 Файл получен. Отправь текст с кодом заказа, чтобы я привязал вложение."
                )


@dp.message(F.photo)
async def handle_photo(message: Message) -> None:
    """Handle photo attachments."""
    if not _is_allowed(message):
        return

    text = message.caption or ""
    photo: PhotoSize = message.photo[-1]  # highest resolution
    owner = _get_topic_owner(message)

    # Generate a meaningful filename
    ts = message.date.strftime("%Y%m%d_%H%M%S") if message.date else "unknown"
    filename = f"photo_{ts}.jpg"

    log.info(f"[telegram] Photo received: from={message.from_user.id if message.from_user else '?'}, chat={message.chat.id}, topic={message.message_thread_id}, caption={'yes' if text.strip() else 'no'}, owner={owner}")

    with tempfile.TemporaryDirectory() as tmp_dir:
        file_path = Path(tmp_dir) / filename
        await bot.download(file=photo.file_id, destination=file_path)

        # If there's text (caption) — process text + photo together
        if text.strip():
            await _download_and_process(message, text, [(filename, file_path)], owner_override=owner)
        else:
            # Photo without caption — just acknowledge
            await message.reply("📷 Фото получено без подписи. Отправь текст с кодом заказа для привязки.")


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    """Handle text messages with order information (including forwarded)."""
    if not _is_allowed(message):
        return

    text = message.text or ""
    if not text or text.startswith("/"):
        return

    owner = _get_topic_owner(message)

    # Check if we have pending attachments for this order
    chat_id = message.chat.id
    order_code = _get_order_code_from_text(text)
    pending_files: list[tuple[str, Path]] = []

    if order_code and chat_id in _pending_attachments:
        pending = _pending_attachments[chat_id].pop(order_code, [])
        if not _pending_attachments[chat_id]:
            del _pending_attachments[chat_id]
        pending_files = pending

    await _download_and_process(message, text, pending_files, owner_override=owner)


# ── Start bot ────────────────────────────────────────────────────────


async def start_telegram_bot() -> None:
    """Start the Telegram bot with long polling."""
    # Clear any stale webhook
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Starting Telegram bot (long polling)...")
    # Explicitly request message updates (needed for forum/topic groups)
    await dp.start_polling(bot, allowed_updates=["message"])
