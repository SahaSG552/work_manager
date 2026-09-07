"""Core processing pipeline — parse message, moderate, create notes, save attachments."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from app.config import settings
from app.database import (
    insert_order,
    update_order,
    update_order_paths,
    get_order_by_code,
    get_order,
)
from app.models import (
    ParsedOrder,
    OrderRecord,
    Source,
    OrderStatus,
    ProcessResponse,
    OrderUpdate,
    generate_tags,
)
from app.obsidian import (
    create_order_note,
    update_order_note,
    append_attachment_link,
    get_working_attachment_dir,
    find_order_in_vault,
    find_working_attachment_dir,
    save_attachment,
)
from app.parser import parse_message

log = logging.getLogger(__name__)


async def create_pending_order(
    text: str,
    source: Source = Source.MANUAL,
    attachments: list[tuple[str, bytes]] | None = None,
    order_code_override: str | None = None,
    yougile_task_id: str | None = None,
    owner_override: str | None = None,
) -> ProcessResponse:
    """Parse message and create a PENDING order (awaiting moderation).

    order_code_override: if set, use this as the order code directly (skip regex reformatting).
    yougile_task_id: YouGile task ID for blacklist integration.
    owner_override: if set, force this owner code (e.g. from TG topic name).
    """
    log.info(f"[processor] create_pending_order: source={source.value}, override={order_code_override}, owner={owner_override}, attachments={len(attachments) if attachments else 0}")
    parsed = await asyncio.to_thread(parse_message, text)
    if order_code_override:
        parsed.order_code = order_code_override
        # All YouGile orders get owner=YG regardless of prefix
        parsed.owner = "YG"
    elif owner_override:
        parsed.owner = owner_override
    log.info(f"[processor] Parsed: code={parsed.order_code}, owner={parsed.owner}, thickness={parsed.thickness}, edge={parsed.edge_type}, email_ref={parsed.email_reference}")

    tags = await generate_tags(parsed)

    record = OrderRecord(
        order_code=parsed.order_code,
        owner=parsed.owner,
        description=parsed.description,
        status=OrderStatus.PENDING,
        source=source,
        raw_text=parsed.raw_text,
        thickness=parsed.thickness,
        sink_type=parsed.sink_type,
        edge_type=parsed.edge_type,
        stone_amount=parsed.stone_amount,
        urgency=parsed.urgency,
        email_reference=parsed.email_reference,
        tags=tags,
        yougile_task_id=yougile_task_id,
    )
    row_id = await insert_order(record)
    record.id = row_id

    # Save temp attachments if any (store reference, actual save on confirm)
    if attachments:
        _save_temp_attachments(record, attachments)

    return ProcessResponse(order=parsed)


async def confirm_and_process(
    order_id: int,
    edits: OrderUpdate | None = None,
    folder_name: str | None = None,
) -> ProcessResponse:
    """Confirm a pending order: apply edits, create Obsidian note + working folders.

    folder_name: override for shared folder name (e.g. "МСЛ-1105, 1106" for multi-order messages).
    """
    record = await get_order(order_id)
    if not record:
        raise ValueError(f"Order {order_id} not found")
    if record.status != OrderStatus.PENDING:
        raise ValueError(f"Order {order_id} is not pending (status={record.status})")

    log.info(f"[confirm:{order_id}] Starting: code={record.order_code}, owner={record.owner}, email_ref={record.email_reference}, folder_name={record.folder_name}")

    # Apply user edits if any
    if edits:
        record = await update_order(order_id, edits)
        if not record:
            raise ValueError(f"Failed to update order {order_id}")

    # Build ParsedOrder from record
    parsed = ParsedOrder(
        order_code=record.order_code,
        owner=record.owner,
        description=record.description,
        thickness=record.thickness,
        sink_type=record.sink_type,
        edge_type=record.edge_type,
        stone_amount=record.stone_amount,
        urgency=record.urgency,
        email_reference=record.email_reference,
        raw_text=record.raw_text,
    )

    # Use folder_name from record if not explicitly provided
    effective_folder = folder_name or record.folder_name

    # Determine projects frontmatter for multi-order children
    projects = None
    if effective_folder and "," in effective_folder:
        # Multi-order: extract parent code (first in folder name)
        import re as _re
        first_match = _re.match(r"([А-ЯA-Z]{1,5})-?(\d{3,5}(?:/[А-ЯA-Z0-9]+|[А-ЯA-Z]\d?)*)", effective_folder)
        if first_match:
            parent_code = first_match.group(0)
            # If this order is NOT the parent, add projects link
            if record.order_code != parent_code:
                projects = [f"[[{parent_code}]]"]

    # Create Obsidian note
    try:
        note_path, obs_attachment_dir = create_order_note(
            parsed, source=record.source.value, tags=record.tags, projects=projects
        )
        log.info(f"Created note: {note_path}")
    except Exception as e:
        log.error(f"Failed to create Obsidian note: {e}")
        note_path, obs_attachment_dir = None, None

    # Create working attachment dir on YandexDisk (with folder_name override for multi-orders)
    try:
        working_dir = get_working_attachment_dir(parsed, folder_name=effective_folder)
        log.info(f"Created working dir: {working_dir}")
    except Exception as e:
        log.error(f"Failed to create working dir: {e}")
        working_dir = None

    # Move temp attachments to the working folder
    import tempfile as _tf
    temp_dir = Path(_tf.gettempdir()) / "work_manager" / "pending" / str(record.id)
    if temp_dir.exists() and working_dir:
        for f in temp_dir.iterdir():
            if f.is_file():
                save_attachment(working_dir, f.name, f.read_bytes(), note_path)
                log.info(f"Moved temp attachment: {f.name}")
        # Clean up temp dir
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Auto-download email attachments if email_reference is set
    email_attachments_found = 0
    if record.email_reference and working_dir:
        log.info(f"Auto-downloading email attachments for {record.order_code} (email_reference=True)")
        try:
            from app.channels.email_checker import fetch_email_attachments
            email_attachs = await fetch_email_attachments(record.order_code)
            log.info(f"Email lookup returned {len(email_attachs)} attachments for {record.order_code}")
            for filename, data in email_attachs:
                save_attachment(working_dir, filename, data, note_path)
                email_attachments_found += 1
            if email_attachments_found:
                log.info(f"Downloaded {email_attachments_found} email attachments for {record.order_code}")
            else:
                log.warning(f"No email attachments found for {record.order_code} — email may not have attachments or subject mismatch")
        except Exception as e:
            log.error(f"Email auto-download failed for {record.order_code}: {e}")
    elif record.email_reference and not working_dir:
        log.warning(f"Cannot download email attachments for {record.order_code} — working_dir is None")

    # Update order with paths and status
    update = OrderUpdate(status=OrderStatus.IN_PROGRESS)
    updated = await update_order(order_id, update)
    if note_path or working_dir:
        await update_order_paths(
            record.order_code,
            str(note_path) if note_path else None,
            str(working_dir) if working_dir else None,
        )

    return ProcessResponse(
        order=parsed,
        obsidian_path=str(note_path) if note_path else None,
        attachment_dir=str(working_dir) if working_dir else None,
        email_attachments_found=email_attachments_found,
    )


async def update_existing_order(
    text: str,
    source: Source = Source.MANUAL,
    attachments: list[tuple[str, bytes]] | None = None,
    email_lookup_fn=None,
    owner_override: str | None = None,
) -> ProcessResponse | None:
    """Handle an incoming message for an order that already exists.

    - In DB: update record, append info to Obsidian note
    - In vault only (not in DB): update Obsidian note silently, no DB record created
    - Nowhere: returns None (new order)
    """
    parsed = await asyncio.to_thread(parse_message, text)
    if owner_override:
        parsed.owner = owner_override

    # Check DB
    existing = await get_order_by_code(parsed.order_code)
    # Check vault
    vault_entry = find_order_in_vault(parsed.order_code)

    if not existing and not vault_entry:
        return None

    log.info(f"Found existing order {parsed.order_code}, updating")

    # --- Order is in DB: update DB + Obsidian ---
    if existing:
        current_status = existing.status

        # Revert done/archived → in_progress
        new_status = None
        if current_status in (OrderStatus.READY, OrderStatus.DONE, OrderStatus.ARCHIVED):
            new_status = OrderStatus.IN_PROGRESS

        # Update Obsidian note
        if existing.obsidian_path and Path(existing.obsidian_path).exists():
            update_order_note(Path(existing.obsidian_path), parsed, status=new_status)

        # Update DB
        update_fields = {}
        if new_status:
            update_fields["status"] = new_status
        for field in ("thickness", "sink_type", "edge_type", "stone_amount", "urgency"):
            new_val = getattr(parsed, field)
            if new_val and not getattr(existing, field):
                update_fields[field] = new_val
        if update_fields:
            await update_order(existing.id, OrderUpdate(**update_fields))

        # Attachment dir
        att_dir = None
        if existing.attachment_dir:
            att_dir = Path(existing.attachment_dir)
        elif existing.status != OrderStatus.PENDING:
            found_dir = find_working_attachment_dir(parsed.order_code, parsed.owner)
            att_dir = found_dir or get_working_attachment_dir(parsed)
            att_dir.mkdir(parents=True, exist_ok=True)

        # Save attachments
        note_path = Path(existing.obsidian_path) if existing.obsidian_path else None
        if attachments and att_dir:
            for filename, data in attachments:
                save_attachment(att_dir, filename, data, note_path)

        # Email lookup
        email_attachments_found = 0
        if parsed.email_reference and email_lookup_fn:
            try:
                email_attachs = await email_lookup_fn(parsed.order_code)
                for filename, data in email_attachs:
                    if att_dir:
                        save_attachment(att_dir, filename, data, note_path)
                    email_attachments_found += 1
            except Exception as e:
                log.error(f"Email lookup failed for {parsed.order_code}: {e}")

        return ProcessResponse(
            order=parsed,
            obsidian_path=existing.obsidian_path,
            attachment_dir=str(att_dir) if att_dir else None,
            email_attachments_found=email_attachments_found,
        )

    # --- Order is in vault only (not in DB): create DB record so it appears in web UI ---
    if vault_entry:
        note_path = vault_entry.get("path")
        new_status = None
        if note_path and Path(note_path).exists():
            # Check vault status — revert done/archived
            vault_status_str = vault_entry.get("status")
            if vault_status_str in ("done", "archived"):
                new_status = OrderStatus.IN_PROGRESS
                update_order_note(Path(note_path), parsed, status=new_status)
            else:
                # Even if status is fine, update the note with new info
                update_order_note(Path(note_path), parsed, status=None)

        # Create DB record so the order appears in web UI
        tags = await generate_tags(parsed)
        owner_folder = vault_entry.get("owner_folder", parsed.owner)
        record = OrderRecord(
            order_code=parsed.order_code,
            owner=parsed.owner,
            description=parsed.description,
            status=OrderStatus.IN_PROGRESS,
            source=source,
            raw_text=parsed.raw_text,
            thickness=parsed.thickness,
            sink_type=parsed.sink_type,
            edge_type=parsed.edge_type,
            stone_amount=parsed.stone_amount,
            urgency=parsed.urgency,
            email_reference=parsed.email_reference,
            tags=tags,
            obsidian_path=str(note_path) if note_path else None,
        )
        row_id = await insert_order(record)
        record.id = row_id

        # Try to find or create working attachment dir
        att_dir = find_working_attachment_dir(parsed.order_code, parsed.owner)
        if not att_dir:
            att_dir = get_working_attachment_dir(parsed)
            att_dir.mkdir(parents=True, exist_ok=True)

        # Save attachments if any
        if attachments:
            note_path_obj = Path(note_path) if note_path else None
            for filename, data in attachments:
                save_attachment(att_dir, filename, data, note_path_obj)

        # Auto-download email attachments
        email_attachments_found = 0
        if parsed.email_reference and email_lookup_fn:
            try:
                email_attachs = await email_lookup_fn(parsed.order_code)
                for filename, data in email_attachs:
                    if att_dir:
                        save_attachment(att_dir, filename, data, Path(note_path) if note_path else None)
                    email_attachments_found += 1
            except Exception as e:
                log.error(f"Email lookup failed for {parsed.order_code}: {e}")

        log.info(f"Created DB record for vault-only order {parsed.order_code}")
        # Update paths in DB
        if note_path or att_dir:
            await update_order_paths(
                parsed.order_code,
                str(note_path) if note_path else None,
                str(att_dir) if att_dir else None,
            )
        return ProcessResponse(
            order=parsed,
            obsidian_path=str(note_path) if note_path else None,
            attachment_dir=str(att_dir) if att_dir else None,
            email_attachments_found=email_attachments_found,
        )

    return None


async def process_message(
    text: str,
    source: Source = Source.MANUAL,
    attachments: list[tuple[str, bytes]] | None = None,
    email_lookup_fn=None,
    auto_mode: bool = False,
    order_code_override: str | None = None,
    yougile_task_id: str | None = None,
    owner_override: str | None = None,
) -> list[ProcessResponse]:
    """Smart pipeline: if order exists → update it; if not → create pending.

    order_code_override: if set, use this as the order code directly (skip parsing).
    yougile_task_id: YouGile task ID for blacklist integration.
    owner_override: if set, force this owner code (e.g. from TG topic name).
    """
    log.info(f"[processor] process_message: source={source.value}, override={order_code_override}, owner={owner_override}, attachments={len(attachments) if attachments else 0}, text={text[:80]}...")
    if order_code_override:
        # Bypass parser — use the provided code as-is (e.g. YouGile title "ЛГКФ340С1")
        import re as _re
        from app.parser import _parse_with_regex
        parsed = await asyncio.to_thread(_parse_with_regex, text)
        parsed.order_code = order_code_override
        # All YouGile orders get owner=YG regardless of prefix
        parsed.owner = "YG"
        orders = [parsed]
        folder_name = ""
    else:
        from app.parser import parse_message_multi
        orders, folder_name = await asyncio.to_thread(parse_message_multi, text)
        # Apply owner_override to all parsed orders
        if owner_override:
            for o in orders:
                o.owner = owner_override

    if len(orders) == 1:
        # Single order — check existing first (unless override provided, then check by override code)
        if order_code_override:
            existing = await get_order_by_code(order_code_override)
            vault_entry = find_order_in_vault(order_code_override)
            if existing or vault_entry:
                # Build a ParsedOrder with the override code for the update
                if existing:
                    # Update existing DB record
                    current_status = existing.status
                    new_status = None
                    if current_status in (OrderStatus.READY, OrderStatus.DONE, OrderStatus.ARCHIVED):
                        new_status = OrderStatus.IN_PROGRESS
                    update_fields: dict = {}
                    if new_status:
                        update_fields["status"] = new_status
                    for field in ("thickness", "sink_type", "edge_type", "stone_amount", "urgency"):
                        new_val = getattr(orders[0], field)
                        if new_val and not getattr(existing, field):
                            update_fields[field] = new_val
                    if update_fields:
                        await update_order(existing.id, OrderUpdate(**update_fields))

                    # Save attachments to working folder
                    att_dir = None
                    if existing.attachment_dir:
                        att_dir = Path(existing.attachment_dir)
                    else:
                        att_dir = find_working_attachment_dir(order_code_override, existing.owner)
                        if not att_dir:
                            att_dir = get_working_attachment_dir(orders[0])
                        att_dir.mkdir(parents=True, exist_ok=True)
                    note_path_obj = Path(existing.obsidian_path) if existing.obsidian_path else None
                    if attachments and att_dir:
                        for filename, data in attachments:
                            save_attachment(att_dir, filename, data, note_path_obj)
                        # Update attachment_dir in DB if it was missing
                        if not existing.attachment_dir:
                            await update_order_paths(order_code_override, existing.obsidian_path, str(att_dir))

                    log.info(f"Updated existing YouGile order {order_code_override}")
                    return [ProcessResponse(order=orders[0], obsidian_path=existing.obsidian_path, attachment_dir=str(att_dir) if att_dir else existing.attachment_dir)]
                # Vault-only: create DB record so it appears in web UI
                tags = await generate_tags(orders[0])
                record = OrderRecord(
                    order_code=order_code_override,
                    owner="YG",
                    description=orders[0].description,
                    status=OrderStatus.IN_PROGRESS,
                    source=source,
                    raw_text=orders[0].raw_text,
                    thickness=orders[0].thickness,
                    sink_type=orders[0].sink_type,
                    edge_type=orders[0].edge_type,
                    stone_amount=orders[0].stone_amount,
                    urgency=orders[0].urgency,
                    email_reference=orders[0].email_reference,
                    tags=tags,
                    obsidian_path=str(vault_entry.get("path")) if vault_entry.get("path") else None,
                    yougile_task_id=yougile_task_id,
                )
                row_id = await insert_order(record)
                record.id = row_id

                # Save attachments to working folder
                att_dir = find_working_attachment_dir(order_code_override, "YG")
                if not att_dir:
                    att_dir = get_working_attachment_dir(orders[0])
                att_dir.mkdir(parents=True, exist_ok=True)
                note_path_obj = Path(vault_entry.get("path")) if vault_entry.get("path") else None
                if attachments:
                    for filename, data in attachments:
                        save_attachment(att_dir, filename, data, note_path_obj)
                # Update DB with attachment dir
                await update_order_paths(order_code_override, str(vault_entry.get("path")) if vault_entry.get("path") else None, str(att_dir))

                log.info(f"Created DB record for vault-only YouGile order {order_code_override}")
                return [ProcessResponse(order=orders[0], obsidian_path=record.obsidian_path, attachment_dir=str(att_dir))]
        else:
            existing_result = await update_existing_order(text, source, attachments, email_lookup_fn, owner_override=owner_override)
            if existing_result:
                return [existing_result]
        return [await create_pending_order(text, source, attachments, order_code_override=order_code_override, yougile_task_id=yougile_task_id, owner_override=owner_override)]

    # Multi-order: each code gets its own DB record and Obsidian note,
    # but they share the same working folder (folder_name like "ИП-1115, 1116")
    results: list[ProcessResponse] = []
    for parsed in orders:
        # Check if this specific code already exists
        existing = await get_order_by_code(parsed.order_code)
        vault_entry = find_order_in_vault(parsed.order_code)

        if existing:
            # Update existing DB record
            current_status = existing.status
            new_status = None
            if current_status in (OrderStatus.READY, OrderStatus.DONE, OrderStatus.ARCHIVED):
                new_status = OrderStatus.IN_PROGRESS
            update_fields: dict = {}
            if new_status:
                update_fields["status"] = new_status
            for field in ("thickness", "sink_type", "edge_type", "stone_amount", "urgency"):
                new_val = getattr(parsed, field)
                if new_val and not getattr(existing, field):
                    update_fields[field] = new_val
            if update_fields:
                await update_order(existing.id, OrderUpdate(**update_fields))

            # Update Obsidian note
            if existing.obsidian_path and Path(existing.obsidian_path).exists():
                update_order_note(Path(existing.obsidian_path), parsed, status=new_status)

            # Auto-download email attachments
            email_attachments_found = 0
            att_dir = Path(existing.attachment_dir) if existing.attachment_dir else None
            if parsed.email_reference and att_dir:
                try:
                    from app.channels.email_checker import fetch_email_attachments
                    email_attachs = await fetch_email_attachments(parsed.order_code)
                    note_path_obj = Path(existing.obsidian_path) if existing.obsidian_path else None
                    for filename, data in email_attachs:
                        save_attachment(att_dir, filename, data, note_path_obj)
                        email_attachments_found += 1
                except Exception as e:
                    log.error(f"Email lookup failed for {parsed.order_code}: {e}")

            results.append(ProcessResponse(
                order=parsed,
                obsidian_path=existing.obsidian_path,
                attachment_dir=existing.attachment_dir,
                email_attachments_found=email_attachments_found,
            ))
            continue

        if vault_entry:
            # Vault-only: create DB record so it appears in web UI
            note_path = vault_entry.get("path")
            new_status = None
            if note_path and Path(note_path).exists():
                vault_status_str = vault_entry.get("status")
                if vault_status_str in ("done", "archived"):
                    new_status = OrderStatus.IN_PROGRESS
                    update_order_note(Path(note_path), parsed, status=new_status)
                else:
                    update_order_note(Path(note_path), parsed, status=None)

            tags = await generate_tags(parsed)
            record = OrderRecord(
                order_code=parsed.order_code,
                owner=parsed.owner,
                description=parsed.description,
                status=OrderStatus.IN_PROGRESS,
                source=source,
                raw_text=parsed.raw_text,
                thickness=parsed.thickness,
                sink_type=parsed.sink_type,
                edge_type=parsed.edge_type,
                stone_amount=parsed.stone_amount,
                urgency=parsed.urgency,
                email_reference=parsed.email_reference,
                tags=tags,
                obsidian_path=str(note_path) if note_path else None,
                folder_name=folder_name if folder_name else None,
            )
            row_id = await insert_order(record)
            record.id = row_id

            # Find attachment dir
            att_dir = find_working_attachment_dir(parsed.order_code, parsed.owner)

            # Auto-download email attachments
            email_attachments_found = 0
            if parsed.email_reference and att_dir:
                try:
                    from app.channels.email_checker import fetch_email_attachments
                    email_attachs = await fetch_email_attachments(parsed.order_code)
                    for filename, data in email_attachs:
                        save_attachment(att_dir, filename, data, Path(note_path) if note_path else None)
                        email_attachments_found += 1
                except Exception as e:
                    log.error(f"Email lookup failed for {parsed.order_code}: {e}")

            log.info(f"Created DB record for vault-only order {parsed.order_code}")
            results.append(ProcessResponse(
                order=parsed,
                obsidian_path=str(note_path) if note_path else None,
                attachment_dir=str(att_dir) if att_dir else None,
                email_attachments_found=email_attachments_found,
            ))
            continue

        # New order — create as PENDING
        tags = await generate_tags(parsed)
        record = OrderRecord(
            order_code=parsed.order_code,
            owner=parsed.owner,
            description=parsed.description,
            status=OrderStatus.PENDING,
            source=source,
            raw_text=parsed.raw_text,
            thickness=parsed.thickness,
            sink_type=parsed.sink_type,
            edge_type=parsed.edge_type,
            stone_amount=parsed.stone_amount,
            urgency=parsed.urgency,
            email_reference=parsed.email_reference,
            tags=tags,
            folder_name=folder_name if folder_name else None,
            yougile_task_id=yougile_task_id,
        )
        row_id = await insert_order(record)
        record.id = row_id

        # Save temp attachments
        if attachments:
            _save_temp_attachments(record, attachments)

        results.append(ProcessResponse(order=parsed))

    return results


# ── Helpers ──────────────────────────────────────────────────────────


def _save_attachment(
    attachment_dir: Path,
    filename: str,
    data: bytes,
    note_path: Path | None,
) -> Path:
    """Save an attachment file. Delegates to obsidian.save_attachment (date-suffix on collision)."""
    return save_attachment(attachment_dir, filename, data, note_path)


def _save_temp_attachments(record: OrderRecord, attachments: list[tuple[str, bytes]]) -> None:
    """Save attachments to a temp dir for pending orders. Will be moved on confirm."""
    import tempfile
    temp_dir = Path(tempfile.gettempdir()) / "work_manager" / "pending" / str(record.id)
    temp_dir.mkdir(parents=True, exist_ok=True)
    for filename, data in attachments:
        safe_name = filename
        for ch in '<>:"/\\|?*':
            safe_name = safe_name.replace(ch, "_")
        (temp_dir / safe_name).write_bytes(data)
    log.info(f"Saved {len(attachments)} temp attachments for pending order {record.id}")


def save_uploaded_file(attachment_dir: Path, src_path: Path) -> Path:
    """Copy an uploaded file to the attachment directory."""
    target = attachment_dir / src_path.name
    shutil.copy2(src_path, target)
    return target
