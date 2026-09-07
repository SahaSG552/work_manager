"""Obsidian vault writer — creates .md tasks, attachment folders, order indexing."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import yaml

from app.config import settings
from app.models import ParsedOrder, OrderRecord, OrderStatus

log = logging.getLogger(__name__)

# Moscow timezone
_MSK = timezone(timedelta(hours=4))

# Month names in English for attachment folder path
_MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}


def _sanitize_filename(name: str) -> str:
    """Remove characters invalid in Windows filenames."""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "-")
    return name.strip()


class OwnerFolderNotFoundError(Exception):
    """Raised when the owner folder doesn't exist in the vault."""


# ── Order index: maps order_code → (note_path, status) ──────────────

# Cached index for fast lookup across the entire vault (recursive)
_order_index: dict[str, dict] | None = None
# Cached prefix→folder mapping
_prefix_to_folder: dict[str, str] | None = None


def build_order_index() -> dict[str, dict]:
    """Scan the entire vault recursively and index all order .md files.

    Returns: {order_code: {"path": Path, "status": str, "owner_folder": str}}
    """
    vault = settings.vault_path
    index: dict[str, dict] = {}

    for md_file in vault.rglob("*.md"):
        rel = md_file.relative_to(vault)
        if any(part.startswith(".") or part == "__pycache__" for part in rel.parts):
            continue

        stem = md_file.stem
        code_match = re.match(r'^([А-ЯA-Z]{1,5}-\d{3,5})$', stem)
        if not code_match:
            continue

        order_code = code_match.group(1)

        status = None
        try:
            text = md_file.read_text(encoding="utf-8")
            if text.startswith("---\n"):
                end = text.find("\n---\n", 4)
                if end != -1:
                    fm_text = text[4:end]
                    fm = yaml.safe_load(fm_text) or {}
                    status = fm.get("status")
        except Exception:
            pass

        owner_folder = rel.parts[0] if len(rel.parts) > 1 else ""
        index[order_code] = {
            "path": md_file,
            "status": status,
            "owner_folder": owner_folder,
        }

    return index


def get_order_index() -> dict[str, dict]:
    """Get or build the order index."""
    global _order_index
    if _order_index is None:
        _order_index = build_order_index()
    return _order_index


def refresh_index() -> None:
    """Force rebuild the order index."""
    global _order_index
    _order_index = None


def find_order_in_vault(order_code: str) -> dict | None:
    """Find an order in the vault by code. Returns index entry or None."""
    return get_order_index().get(order_code)


def get_vault_order_status(order_code: str) -> str | None:
    """Get the status of an order from the vault (reads frontmatter)."""
    entry = find_order_in_vault(order_code)
    return entry.get("status") if entry else None


# ── Prefix→folder mapping ────────────────────────────────────────────


def _scan_prefix_map() -> dict[str, str]:
    """Scan vault subfolders and build prefix→folder mapping from existing .md files.

    Recursively scans subfolders.
    """
    vault = settings.vault_path
    mapping: dict[str, str] = {}
    for folder in vault.iterdir():
        if not folder.is_dir() or folder.name.startswith('.') or folder.name == '__pycache__':
            continue
        for f in folder.rglob("*.md"):
            name = f.stem
            prefix = ""
            if '-' in name:
                prefix = name.split('-')[0]
            else:
                m = re.match(r'^([^\d]+)', name)
                if m and len(m.group(1)) >= 2:
                    prefix = m.group(1)
            if prefix and len(prefix) >= 2:
                if prefix not in mapping:
                    mapping[prefix] = folder.name
    return mapping


def _resolve_owner_folder(order_code: str, owner: str) -> str:
    """Map order_code prefix to an Obsidian folder name.

    Creates the folder if it's a new owner code.
    """
    global _prefix_to_folder
    prefix = order_code.split("-")[0] if "-" in order_code else owner

    # 1. Explicit mapping from .env
    explicit = settings.owner_mapping.get(prefix)
    if explicit:
        owner_dir = settings.vault_path / explicit
        if owner_dir.exists():
            return explicit

    # 2. Auto-detect from vault scan (cached)
    if _prefix_to_folder is None:
        _prefix_to_folder = _scan_prefix_map()
    folder_name = _prefix_to_folder.get(prefix)
    if folder_name:
        return folder_name

    # 3. Direct match
    owner_dir = settings.vault_path / owner
    if owner_dir.exists() and owner_dir.is_dir():
        return owner

    # 4. Case-insensitive match
    for d in settings.vault_path.iterdir():
        if d.is_dir() and d.name.upper() == prefix.upper():
            return d.name

    # 5. NEW: Create the owner folder (new client code)
    new_dir = settings.vault_path / owner
    new_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Created new owner folder: {new_dir}")

    # Refresh caches
    if _prefix_to_folder is not None:
        _prefix_to_folder[prefix] = owner
    refresh_index()

    return owner


def _status_for_order(parsed: ParsedOrder) -> OrderStatus:
    """Determine initial status from parsed urgency field."""
    if not parsed.urgency:
        return OrderStatus.IN_PROGRESS
    text = parsed.urgency.lower()
    if "срочно" in text:
        return OrderStatus.URGENT
    if "можно" in text and ("резать" in text or "готовить" in text or "работ" in text):
        return OrderStatus.READY
    return OrderStatus.IN_PROGRESS


def _build_attachment_dir_path(parsed: ParsedOrder, folder_name: str | None = None) -> str:
    """Build the YandexDisk attachment path: ДАНАТА/{YYYY}/{MM} {MonthName_EN}/{client_code}/- {ordername}/"""
    now = datetime.now(_MSK)
    month_name = _MONTH_NAMES.get(now.month, "Unknown")
    mm = f"{now.month:02d}"
    client_code = parsed.owner
    order_name = _sanitize_filename(folder_name or parsed.order_code)
    return f"ДАНАТА/{now.year}/{mm} {month_name}/{client_code}/- {order_name}"


def _build_frontmatter(
    parsed: ParsedOrder,
    status: OrderStatus,
    tags: list[str] | None = None,
    projects: list[str] | None = None,
) -> dict:
    """Build YAML frontmatter dict for the Obsidian note."""
    now = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")
    all_tags: list[str] = ["task"]  # every order gets #task tag
    if tags:
        all_tags.extend(tags)
    elif parsed.auto_tags:
        all_tags.extend(parsed.auto_tags)

    fm: dict = {
        "Owner": parsed.owner,
        "orderCode": parsed.order_code,
        "dateCreated": now,
        "dateModified": now,
        "status": status.value,
    }
    if parsed.thickness:
        fm["thickness"] = parsed.thickness
        # "T" field — numeric thickness only (e.g. "16" not "16 мм")
        import re as _re
        t_match = _re.search(r"(\d+)", parsed.thickness)
        if t_match:
            fm["T"] = int(t_match.group(1))
    if parsed.sink_type:
        fm["sinkType"] = parsed.sink_type
    if parsed.edge_type:
        fm["edgeType"] = parsed.edge_type
    if parsed.stone_amount:
        fm["stoneAmount"] = parsed.stone_amount
    if parsed.email_reference:
        fm["emailReference"] = True
    if projects:
        fm["projects"] = projects
    fm["tags"] = all_tags
    return fm


def _build_content(parsed: ParsedOrder, attachment_dir_name: str | None) -> str:
    """Build the markdown body of the note."""
    lines = [f"# Заказ {parsed.order_code}", ""]
    lines.append(parsed.description)
    lines.append("")

    if parsed.thickness:
        lines.append(f"**Толщина:** {parsed.thickness}")
    if parsed.sink_type:
        lines.append(f"**Мойка:** {parsed.sink_type}")
    if parsed.edge_type:
        lines.append(f"**Кромка:** {parsed.edge_type}")
    if parsed.stone_amount:
        lines.append(f"**Кол-во камня:** {parsed.stone_amount}")
    if parsed.urgency:
        lines.append(f"**Срочность:** {parsed.urgency}")
    if parsed.email_reference:
        lines.append("⚠️ **Данные на почте** — нужно забрать вложения из письма")

    if attachment_dir_name:
        lines.append("")
        lines.append("## Вложения")
        lines.append(f"→ [[{attachment_dir_name}/]]")

    return "\n".join(lines)


def create_order_note(
    parsed: ParsedOrder,
    source: str = "manual",
    tags: list[str] | None = None,
    projects: list[str] | None = None,
) -> tuple[Path, Path]:
    """Create an Obsidian .md note and attachment folder for a parsed order.

    projects: list of parent order codes for multi-order children (e.g. ["[[ИП-1115]]"])

    Returns:
        (note_path, attachment_dir_path)
    """
    vault = settings.vault_path
    owner_folder = _resolve_owner_folder(parsed.order_code, parsed.owner)
    status = _status_for_order(parsed)

    log.info(f"[obsidian] Creating note for {parsed.order_code}: owner_folder={owner_folder}, status={status.value}, tags={tags}, projects={projects}")

    # Owner folder (create if new)
    owner_dir = vault / owner_folder
    owner_dir.mkdir(parents=True, exist_ok=True)

    # Attachment folder inside vault: {owner}/{order_code}/
    attachment_dir = owner_dir / parsed.order_code
    attachment_dir.mkdir(parents=True, exist_ok=True)

    # Note path: {owner}/{order_code}.md
    note_path = owner_dir / f"{parsed.order_code}.md"

    # Build frontmatter
    fm = _build_frontmatter(parsed, status, tags, projects=projects)
    fm["source"] = source

    # Build content
    content = _build_content(parsed, parsed.order_code)

    # Write note
    fm_str = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    full_text = f"---\n{fm_str}---\n\n{content}\n"
    note_path.write_text(full_text, encoding="utf-8")
    log.info(f"[obsidian] Note written: {note_path} ({len(full_text)} bytes)")

    return note_path, attachment_dir


def update_order_note(
    note_path: Path,
    parsed: ParsedOrder,
    status: OrderStatus | None = None,
    tags: list[str] | None = None,
) -> None:
    """Update an existing Obsidian note with new information.

    Reads existing frontmatter, merges in new fields, updates status and content.
    """
    if not note_path.exists():
        return

    text = note_path.read_text(encoding="utf-8")

    # Parse existing frontmatter
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            fm_text = text[4:end]
            existing_fm = yaml.safe_load(fm_text) or {}
            content_text = text[end + 5:].strip()
        else:
            existing_fm = {}
            content_text = text
    else:
        existing_fm = {}
        content_text = text

    now = datetime.now(_MSK).strftime("%Y-%m-%d %H:%M:%S")
    existing_fm["dateModified"] = now

    # Update status — if existing was done/archived, revert to in_progress
    if status:
        existing_fm["status"] = status.value
    elif existing_fm.get("status") in ("done", "archived"):
        existing_fm["status"] = OrderStatus.IN_PROGRESS.value

    # Update optional fields — ONLY if they're missing in existing (preserve Price etc)
    if parsed.thickness and not existing_fm.get("thickness"):
        existing_fm["thickness"] = parsed.thickness
    if parsed.sink_type and not existing_fm.get("sinkType"):
        existing_fm["sinkType"] = parsed.sink_type
    if parsed.edge_type and not existing_fm.get("edgeType"):
        existing_fm["edgeType"] = parsed.edge_type
    if parsed.stone_amount and not existing_fm.get("stoneAmount"):
        existing_fm["stoneAmount"] = parsed.stone_amount
    if parsed.email_reference and not existing_fm.get("emailReference"):
        existing_fm["emailReference"] = True

    # Merge tags
    if tags:
        existing_tags = existing_fm.get("tags", [])
        for t in tags:
            if t not in existing_tags:
                existing_tags.append(t)
        existing_fm["tags"] = existing_tags

    # Append update note to content
    update_block = f"\n\n## Обновление ({now})\n\n{parsed.raw_text}"
    content_text += update_block

    # Write back
    fm_str = yaml.dump(existing_fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    full_text = f"---\n{fm_str}---\n\n{content_text}\n"
    note_path.write_text(full_text, encoding="utf-8")

    # Refresh index
    refresh_index()


def get_working_attachment_dir(parsed: ParsedOrder, folder_name: str | None = None) -> Path:
    """Build and create the YandexDisk working attachment directory.

    Path: E:/Работа/YandexDisk/ДАНАТА/{YYYY}/{MM} {MonthName_EN}/{client_code}/- {ordername}/
    folder_name: override for the order folder name (e.g. "МСЛ-1105, 1106")
    """
    base = Path(settings.working_base_path)
    rel = _build_attachment_dir_path(parsed, folder_name)
    full_path = base / rel
    full_path.mkdir(parents=True, exist_ok=True)
    log.info(f"[obsidian] Working dir: {full_path}" + (f" (folder_name={folder_name})" if folder_name else ""))
    return full_path


def find_working_attachment_dir(order_code: str, owner: str) -> Path | None:
    """Find existing working attachment dir for an order in YandexDisk.

    Scans all year/month folders for a matching client_code/order_name combo.
    """
    base = Path(settings.working_base_path) / "ДАНАТА"
    if not base.exists():
        return None

    order_name = "- " + _sanitize_filename(order_code)
    order_name_alt = _sanitize_filename(order_code)

    for year_dir in base.iterdir():
        if not year_dir.is_dir() or not year_dir.name.isdigit():
            continue
        for month_dir in year_dir.iterdir():
            if not month_dir.is_dir():
                continue
            client_dir = month_dir / owner
            if client_dir.exists():
                target = client_dir / order_name
                if target.exists():
                    return target
                alt_target = client_dir / order_name_alt
                if alt_target.exists():
                    return alt_target

    return None


def save_attachment(
    attachment_dir: Path,
    filename: str,
    data: bytes,
    note_path: Path | None = None,
) -> Path:
    """Save an attachment file. If file exists, adds date suffix instead of overwriting.

    Returns the actual path the file was saved to.
    """
    safe_name = filename
    for ch in '<>:"/\\|?*':
        safe_name = safe_name.replace(ch, "_")

    target = attachment_dir / safe_name

    # If file already exists, add date suffix
    if target.exists():
        now = datetime.now(_MSK)
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix
        date_suffix = now.strftime("_%Y%m%d_%H%M%S")
        safe_name = f"{stem}{date_suffix}{suffix}"
        target = attachment_dir / safe_name

    target.write_bytes(data)
    log.info(f"[obsidian] Saved attachment: {target} ({len(data)} bytes)" + (" [collision-suffix]" if target.name != safe_name else ""))

    if note_path:
        append_attachment_link(note_path, safe_name)

    return target


def append_attachment_link(note_path: Path, filename: str) -> None:
    """Append a link to an attachment in the note's ## Вложения section."""
    if not note_path.exists():
        return
    text = note_path.read_text(encoding="utf-8")
    order_code = note_path.stem
    link = f"- [[{order_code}/{filename}|{filename}]]"

    if "## Вложения" in text:
        lines = text.split("\n")
        inserted = False
        for i, line in enumerate(lines):
            if line.strip() == "## Вложения":
                insert_at = i + 1
                while insert_at < len(lines) and lines[insert_at].startswith("- "):
                    insert_at += 1
                lines.insert(insert_at, link)
                inserted = True
                break
        if inserted:
            note_path.write_text("\n".join(lines), encoding="utf-8")
    else:
        text += f"\n\n## Вложения\n{link}\n"
        note_path.write_text(text, encoding="utf-8")
