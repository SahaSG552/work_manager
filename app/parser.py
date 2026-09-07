"""LLM-based message parser using instructor + OpenAI-compatible API."""

from __future__ import annotations

import logging

import instructor
from openai import OpenAI

from app.config import settings
from app.models import ParsedOrder

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты парсишь сообщения о заказах на изготовление столешниц и других изделий из камня.

Извлеки структурированные данные из текста сообщения.

Правила:
- Код заказа обычно в формате XXX-NNNN (например ЧМ-1102, СРБ-1046, ИП-1087, МКП-1036).
- Если указано несколько кодов через запятую (например "МСЛ-1105, 1106" или "ЧМ-1102, 1103") — это РАЗНЫЕ заказы с одной базой. Выдели КАЖДЫЙ код отдельно.
- Если в сообщении НЕТ кода заказа (нет паттерна XXX-NNNN) — сгенерируй код: используй префикс "БЕЗ-" + первые значимые слова из сообщения (например "БЕЗ-Дима" или "БЕЗ-Заказ").
- Префикс в квадратных скобках в начале текста (например "[KK]") — это код заказчика (owner), обязательно используй его.
- Owner — код заказчика. Определяй по префиксу кода заказа или из скобок []:
  ЧМ → KK, СРБ → KK, ИП → KK, МКП → KK, МСЛ → KK, КМ → KH, АР → AM.
  Если в тексте есть [XX] — owner = XX.
  Если код БЕЗ-... то owner = "БЕЗ".
- Если написано "на почте", "письмо", "почта" или похожее — email_reference = True.
- description — краткое описание изделий (столешка, остров, стеновая и т.д.) или сути сообщения.
- urgency — текст про срочность/готовность: "срочно готовить", "можно резать" и т.д.
- edge_type — тип кромки/борта:
  - "вместо борта штапик" → "борт-штапик"
  - "штапик" без упоминания борта → "штапик"
  - "галтель" → "галтель"
  - "борт 35" → "борт 35"
  - "борт стандарт" или "борт стандарт (30)" → "борт стандарт"
- Если информация отсутствует — оставляй поле null/None.
"""


def _get_client() -> instructor.Instructor:
    """Create an instructor-patched OpenAI-compatible client.

    Works with any OpenAI-compatible endpoint:
    - NVIDIA NIM (https://integrate.api.nvidia.com/v1)
    - OpenAI (https://api.openai.com/v1)
    - DeepSeek (https://api.deepseek.com)
    - Groq (https://api.groq.com/openai/v1)
    - Google Gemini (via OpenAI compat)
    """
    base = OpenAI(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
    )
    return instructor.from_openai(base)


def parse_message(text: str) -> ParsedOrder:
    """Parse a raw message text into a structured ParsedOrder.

    Falls back to a basic regex-based extraction if LLM is unavailable.
    """
    if settings.llm_api_key:
        try:
            return _parse_with_llm(text)
        except Exception as e:
            log.error(f"LLM parsing failed, falling back to regex: {e}")
            return _parse_with_regex(text)
    else:
        log.warning("LLM API key not set, using regex fallback parser")
        return _parse_with_regex(text)


def parse_message_multi(text: str) -> tuple[list[ParsedOrder], str]:
    """Parse a message that may contain multiple order codes.

    Returns:
        (orders, folder_name) where folder_name is the combined name for the shared folder.
        e.g. "МСЛ-1105, 1106" → ([ParsedOrder(МСЛ-1105), ParsedOrder(МСЛ-1106)], "МСЛ-1105, 1106")
        Order codes are preserved as-is from text — no reformatting.
    """
    import re

    codes: list[str] = []

    # Match the first full code (PREFIX-NNNN, PREFIXNNNN, PREFIXNNNN/А1, PREFIXNNNNД1)
    first_match = re.search(r"([А-ЯA-Z]{1,5})-?(\d{3,5}(?:/[А-ЯA-Z0-9]+|[А-ЯA-Z]\d?)*)", text)
    if not first_match:
        # Single order or no-code message
        return [parse_message(text)], ""

    prefix = first_match.group(1)
    first_code = first_match.group(0)  # as-is: "МСЛ-1105", "МСЛ1105", "ЛММФ306/А1"
    codes.append(first_code)

    # Detect hyphen presence for additional codes
    has_hyphen = "-" in first_code.split("/")[0]  # check before any slash

    # Look for additional numbers after the first code
    remainder = text[first_match.end():]
    additional_nums = re.findall(r",\s*(\d{3,5})", remainder)
    sep = "-" if has_hyphen else ""
    for num in additional_nums:
        codes.append(f"{prefix}{sep}{num}")

    if len(codes) == 1:
        # Single order
        return [parse_message(text)], ""

    # Multiple orders — parse each with the same text, override order_code
    base = parse_message(text)

    # Build folder name: "ИП-1115, 1116" (prefix shown once for first, just numbers for rest)
    if len(codes) > 1:
        # Extract number part from each code (everything after prefix, stripping the dash)
        parts = [codes[0]]  # first code stays full: "ИП-1115"
        for code in codes[1:]:
            # Strip prefix and optional dash to get just the number/suffix
            rest = code[len(prefix):]
            if rest.startswith("-"):
                rest = rest[1:]
            parts.append(rest)  # just "1116"
        folder_name = ", ".join(parts)
    else:
        folder_name = codes[0]

    orders = []
    for code in codes:
        order = base.model_copy(update={"order_code": code})
        orders.append(order)

    return orders, folder_name


def _parse_with_llm(text: str) -> ParsedOrder:
    """Parse using LLM via instructor."""
    client = _get_client()
    result = client.chat.completions.create(
        model=settings.llm_model,
        response_model=ParsedOrder,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        temperature=0.0,
    )
    # Ensure raw_text is populated
    if not result.raw_text:
        result.raw_text = text
    return result


def _parse_with_regex(text: str) -> ParsedOrder:
    """Basic regex fallback when no LLM is available."""
    import re
    from datetime import datetime

    # Check for [OWNER] prefix from topic name
    owner_override = None
    owner_match = re.match(r"^\[([A-Za-zА-Яа-я]{1,5})\]\s*", text)
    if owner_match:
        owner_override = owner_match.group(1)
        text = text[owner_match.end():]  # strip the prefix for further parsing

    # Extract order code — as-is from text, no reformatting
    # Supports: ЧМ-1102, АР236, ЛММФ306/А1, ЛТШФ6239Д1
    # Pattern: PREFIX + optional dash + NUMBER + optional /SUFFIX or Cyrillic suffix
    code_match = re.search(r"([А-ЯA-Z]{1,5})-?(\d{3,5}(?:/[А-ЯA-Z0-9]+|[А-ЯA-Z]\d?)*)", text)
    
    if code_match:
        order_code = code_match.group(0)  # preserve original: "АР236", "ЛММФ306/А1", "ЧМ-1102"
        prefix = code_match.group(1)
        owner = owner_override or settings.owner_mapping.get(prefix, prefix)
        description = text[:200]
    else:
        # No code found — create named order from text
        clean = re.sub(r'[^\w\s-]', '', text).strip()
        name_part = clean[:30].strip() or "Заказ"
        ts = datetime.now().strftime("%H%M")
        order_code = f"БЕЗ-{ts}"
        owner = owner_override or "БЕЗ"
        description = text[:200]

    # Thickness — supports "толщина/толщиной 16мм", "16 мм", "толщина 16"
    thick_match = re.search(r"толщин[аеой]?\s*(\d+)\s*(?:мм|мм\.?)?", text, re.IGNORECASE)
    if not thick_match:
        thick_match = re.search(r"(\d+)\s*(?:мм|мм\.?)\b", text, re.IGNORECASE)
    thickness = f"{thick_match.group(1)} мм" if thick_match else None

    # Sink type
    sink = None
    if "подстольн" in text.lower():
        sink = "подстольная"
    elif "накладн" in text.lower():
        sink = "накладная"

    # Edge type — "вместо борта штапик" = "борт-штапик", "борт 35", "борт стандарт"
    edge = None
    if re.search(r"вместо\s+борта\s+штапик", text, re.IGNORECASE):
        edge = "борт-штапик"
    elif "штапик" in text.lower():
        edge = "штапик"
    elif "галтель" in text.lower():
        edge = "галтель"
    else:
        bord_match = re.search(r"борт\s+(стандарт(?:\s*\(\d+\))?|\d+)", text, re.IGNORECASE)
        if bord_match:
            edge = f"борт {bord_match.group(1).strip()}"

    # Email reference
    email_ref = any(
        w in text.lower() for w in ["на почте", "письмо", "почте", "почта"]
    )

    return ParsedOrder(
        order_code=order_code,
        owner=owner,
        description=description,
        thickness=thickness,
        sink_type=sink,
        edge_type=edge,
        stone_amount=None,
        urgency=None,
        email_reference=email_ref,
        raw_text=text,
    )
