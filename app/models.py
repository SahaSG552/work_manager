"""Data models for order parsing and storage."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ── Parsed order (LLM output) ──────────────────────────────────────


class ParsedOrder(BaseModel):
    """Structured data extracted from a raw message by the LLM."""

    order_code: str = Field(
        description="Код заказа в формате XXX-NNNN: ЧМ-1102, СРБ-1046, ИП-1087, МКП-1036 и т.д."
    )
    owner: str = Field(
        description="Код заказчика: КК, СРБ, ИП, МКП и т.д. — определяется по префиксу кода заказа или контексту"
    )
    description: str = Field(
        description="Краткое описание что нужно: столешка, остров, стеновая панель и т.д."
    )
    thickness: Optional[str] = Field(
        default=None, description="Толщина камня: 30 мм, 40 мм и т.д."
    )
    sink_type: Optional[str] = Field(
        default=None, description="Тип мойки: подстольная, накладная"
    )
    edge_type: Optional[str] = Field(
        default=None, description="Тип кромки: галтель, штапик вместо борта и т.д."
    )
    stone_amount: Optional[str] = Field(
        default=None, description="Количество камня: '1 + 0,5', '1' и т.д."
    )
    urgency: Optional[str] = Field(
        default=None,
        description="Срочность: 'срочно готовить в работу', 'можно резать', 'можно готовить в работу'",
    )
    email_reference: bool = Field(
        default=False,
        description="True если в сообщении есть упоминание что данные/чертежи на почте",
    )
    raw_text: str = Field(description="Полный оригинальный текст сообщения")

    @property
    def auto_tags(self) -> list[str]:
        """Auto-generate tags from parsed fields. Only meaningful info — no owner code or 'order'.

        Note: uses hardcoded rules for sync usage. For DB-configurable rules,
        use generate_tags(parsed) from settings_db instead.
        """
        tags: list[str] = []
        if self.edge_type:
            edge = self.edge_type.lower()
            if "штапик" in edge:
                tags.append("Штапик")
            if "галтель" in edge:
                tags.append("Галтель")
            if "борт" in edge and "штапик" not in edge:
                tags.append("Борт")
        if self.email_reference:
            tags.append("почта")
        return tags


async def generate_tags(parsed: "ParsedOrder") -> list[str]:
    """Generate tags using DB-configured rules. Falls back to auto_tags."""
    try:
        from app.settings_db import get_tag_rules
        rules = await get_tag_rules()
    except Exception:
        return parsed.auto_tags

    tags: list[str] = []
    text_lower = (parsed.raw_text or "").lower()
    if parsed.edge_type:
        text_lower += " " + parsed.edge_type.lower()

    for keyword, tag in rules.items():
        if keyword.lower() in text_lower:
            if tag not in tags:
                tags.append(tag)

    return tags if tags else parsed.auto_tags


# ── Internal order record (stored in SQLite) ────────────────────────


class Source(str, Enum):
    TELEGRAM = "telegram"
    EMAIL = "email"
    YOUGILE = "yougile"
    MANUAL = "manual"


class OrderStatus(str, Enum):
    PENDING = "pending"          # на модерации, ожидает подтверждения
    IN_PROGRESS = "in_progress"  # в работе
    READY = "ready"              # можно готовить / резать
    URGENT = "urgent"            # срочно
    DONE = "done"                 # выполнен
    ARCHIVED = "archived"         # архив


class OrderRecord(BaseModel):
    """An order as stored internally."""

    id: int | None = None
    order_code: str
    owner: str
    description: str
    status: OrderStatus = OrderStatus.PENDING
    source: Source = Source.MANUAL
    raw_text: str
    thickness: str | None = None
    sink_type: str | None = None
    edge_type: str | None = None
    stone_amount: str | None = None
    urgency: str | None = None
    email_reference: bool = False
    tags: list[str] = Field(default_factory=list)
    folder_name: str | None = None  # Shared folder name for multi-orders (e.g. "МСЛ-1105, 1106")
    yougile_task_id: str | None = None  # YouGile task ID (for blacklist)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    obsidian_path: str | None = None
    attachment_dir: str | None = None


# ── History ────────────────────────────────────────────────────────────


class HistoryEntry(BaseModel):
    """A single change in order history."""
    id: int | None = None
    order_id: int
    field: str          # which field changed
    old_value: str | None = None
    new_value: str | None = None
    changed_at: datetime = Field(default_factory=datetime.now)


# ── API request/response models ──────────────────────────────────────


class ManualEntryRequest(BaseModel):
    """Manual message entry from the web UI."""
    text: str
    source: Source = Source.MANUAL


class OrderUpdate(BaseModel):
    """Update fields for an existing order."""
    order_code: str | None = None
    owner: str | None = None
    description: str | None = None
    status: OrderStatus | None = None
    thickness: str | None = None
    sink_type: str | None = None
    edge_type: str | None = None
    stone_amount: str | None = None
    urgency: str | None = None
    email_reference: bool | None = None
    tags: list[str] | None = None
    folder_name: str | None = None


class ServiceCheckResult(BaseModel):
    """Result from scanning a service for new messages."""
    source: Source
    found: int = 0
    orders: list[dict] = Field(default_factory=list)
    error: str | None = None


class ProcessResponse(BaseModel):
    """Response after processing a message."""
    order: ParsedOrder
    obsidian_path: str | None = None
    attachment_dir: str | None = None
    email_attachments_found: int = 0
