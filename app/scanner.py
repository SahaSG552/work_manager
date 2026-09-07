"""YouGile scanner — chain filter with AND/OR/NOT operators + subtasks + blacklist."""

from __future__ import annotations

import logging
from typing import Any

from app.models import Source, ServiceCheckResult

log = logging.getLogger(__name__)

# Track which YouGile task IDs we've already seen (avoid duplicates within a session)
_seen_yougile_tasks: set[str] = set()


async def _get_blacklist() -> set[str]:
    """Get YouGile task IDs that are blacklisted (should be skipped)."""
    try:
        from app.settings_db import get_setting
        bl = await get_setting("yougile_blacklist")
        return set(bl) if bl else set()
    except Exception:
        return set()


async def _resolve_stickers(task: dict, sticker_map: dict[str, dict]) -> list[str]:
    """Resolve a task's sticker hex values to readable names using sticker_map."""
    result = []
    for sticker_uuid, state_id in (task.get("stickers") or {}).items():
        if not state_id:
            continue
        info = sticker_map.get(state_id)
        if info:
            result.append(f"{info['sticker']}: {info['state']}")
        else:
            result.append(state_id)
    return result


def _check_filter(cond: dict, task: dict, board_of_task: dict | None, sticker_map: dict[str, dict]) -> bool:
    """Evaluate a single filter condition against a task."""
    field = cond.get("field", "")
    op = cond.get("op", "eq")
    value = cond.get("value", "")

    if field == "board":
        if op == "eq":
            return bool(board_of_task and board_of_task.get("id") == value)
        return True

    elif field == "column":
        if op == "any" or value == "any":
            return True
        return task.get("columnId") == value

    elif field == "title":
        keyword = str(value).lower()
        title = (task.get("title") or "").lower()
        if op == "contains":
            return keyword in title
        elif op == "eq":
            return title == keyword
        return False

    elif field == "sticker":
        stickers = task.get("stickers") or {}
        if op == "eq":
            return value in stickers.values()
        elif op == "contains":
            return any(value in v for v in stickers.values())
        return False

    elif field == "sticker_name":
        keyword = str(value).lower()
        stickers = task.get("stickers") or {}
        for state_id in stickers.values():
            if not state_id:
                continue
            info = sticker_map.get(state_id)
            if info and keyword in info["state"].lower():
                return True
        return False

    elif field == "assigned":
        task_assigned = set(task.get("assigned") or [])
        check_ids = set(value) if isinstance(value, list) else {value}
        return bool(task_assigned & check_ids)

    elif field == "done":
        task_completed = bool(task.get("completed"))
        if op == "eq":
            return task_completed == bool(value)
        return task_completed

    return True


def _evaluate_chain(chain: list[dict], task: dict, board_of_task: dict | None, sticker_map: dict[str, dict]) -> bool:
    """Evaluate a filter chain: [filter, op, filter, op, filter, ...].

    Operators: AND, OR, NOT
    - AND: result = result AND filter_result
    - OR:  result = result OR filter_result
    - NOT: result = result AND (NOT filter_result)

    Default between adjacent filters (no op) is AND.
    """
    if not chain:
        return False

    result = None
    current_op = "AND"

    for item in chain:
        if item.get("kind") == "op":
            current_op = item.get("value", "AND").upper()
            continue

        # It's a filter — evaluate it
        filter_result = _check_filter(item, task, board_of_task, sticker_map)

        if result is None:
            result = filter_result
        elif current_op == "AND":
            result = result and filter_result
        elif current_op == "OR":
            result = result or filter_result
        elif current_op == "NOT":
            result = result and (not filter_result)

    return result or False


def _rule_to_chain(rule: dict) -> list[dict]:
    """Convert any rule format to chain format.

    Supports:
    - type="chain" with chain field (native)
    - type="composite" with operator + conditions (legacy)
    - type="column_watch" (legacy)
    - type="keyword_filter" (legacy)
    """
    rule_type = rule.get("type", "")

    if rule_type == "chain":
        return rule.get("chain", [])

    if rule_type == "composite":
        operator = rule.get("operator", "AND").upper()
        conditions = rule.get("conditions", [])
        if not conditions:
            return []
        chain: list[dict] = []
        for i, cond in enumerate(conditions):
            if i > 0:
                chain.append({"kind": "op", "value": operator})
            chain.append({**cond, "kind": "filter"})
        return chain

    if rule_type == "column_watch":
        chain = []
        if rule.get("board_id"):
            chain.append({"kind": "filter", "field": "board", "op": "eq", "value": rule["board_id"]})
            chain.append({"kind": "op", "value": "AND"})
        chain.append({"kind": "filter", "field": "column", "op": "eq", "value": rule.get("column_id", "")})
        return chain

    if rule_type == "keyword_filter":
        chain = []
        first = True
        if rule.get("board_id"):
            chain.append({"kind": "filter", "field": "board", "op": "eq", "value": rule["board_id"]})
            first = False
        for kw in rule.get("title_keywords", []):
            if not first:
                chain.append({"kind": "op", "value": "OR"})
            chain.append({"kind": "filter", "field": "title", "op": "contains", "value": kw})
            first = False
        for kw in rule.get("sticker_keywords", []):
            if not first:
                chain.append({"kind": "op", "value": "OR"})
            chain.append({"kind": "filter", "field": "sticker_name", "op": "contains", "value": kw})
            first = False
        return chain

    return []


async def _collect_tasks_for_board(board_id: str) -> list[dict]:
    """Collect all tasks from all columns of a board (including subtasks)."""
    from app.yougile import get_columns, get_tasks, get_task

    tasks: list[dict] = []
    seen: set[str] = set()

    columns = await get_columns(board_id)
    for col in columns:
        col_id = col.get("id", "")
        if not col_id:
            continue
        offset = 0
        while offset < 500:
            data = await get_tasks(column_id=col_id, limit=100, offset=offset)
            content = data.get("content", [])
            if not content:
                break
            for t in content:
                tid = t.get("id", "")
                if tid and tid not in seen:
                    seen.add(tid)
                    tasks.append(t)
                    # Fetch subtasks
                    for sub_id in t.get("subtasks") or []:
                        if sub_id not in seen:
                            seen.add(sub_id)
                            try:
                                sub = await get_task(sub_id)
                                if isinstance(sub, dict) and sub.get("id"):
                                    tasks.append(sub)
                            except Exception:
                                pass  # subtask may be deleted
            if len(content) < 100:
                break
            offset += 100

    return tasks


async def _collect_all_tasks_with_subtasks(limit: int = 300) -> list[dict]:
    """Collect tasks across all boards with pagination. Fetch subtasks for matched ones only."""
    from app.yougile import get_tasks, get_task

    tasks: list[dict] = []
    seen: set[str] = set()

    offset = 0
    while offset < limit:
        data = await get_tasks(limit=100, offset=offset)
        content = data.get("content", [])
        if not content:
            break
        for t in content:
            tid = t.get("id", "")
            if tid and tid not in seen:
                seen.add(tid)
                tasks.append(t)
        paging = data.get("paging", {})
        if not paging.get("next"):
            break
        offset += 100

    # Collect subtask IDs from all tasks
    subtask_ids: list[str] = []
    for t in tasks:
        for sub_id in t.get("subtasks") or []:
            if sub_id not in seen:
                seen.add(sub_id)
                subtask_ids.append(sub_id)

    # Fetch subtasks in batches (but don't fail on 404s)
    for sub_id in subtask_ids[:50]:  # limit to prevent too many API calls
        try:
            sub = await get_task(sub_id)
            if isinstance(sub, dict) and sub.get("id"):
                tasks.append(sub)
        except Exception:
            pass

    return tasks


async def _build_board_lookup(tasks: list[dict], known_board: dict | None = None) -> dict[str, dict]:
    """Build task_id → board dict for board-filtered conditions.

    If known_board is provided (when tasks were collected for a specific board),
    all tasks map to that board without extra API calls.
    """
    if known_board:
        return {t["id"]: known_board for t in tasks if t.get("id")}

    from app.yougile import get_boards, get_columns

    # Build column_id → board mapping
    col_to_board: dict[str, dict] = {}
    boards = await get_boards()
    for b in boards:
        board_id = b.get("id", "")
        if not board_id:
            continue
        try:
            columns = await get_columns(board_id)
            for c in columns:
                col_to_board[c.get("id", "")] = b
        except Exception:
            pass

    result: dict[str, dict] = {}
    for t in tasks:
        col_id = t.get("columnId", "")
        if col_id and col_id in col_to_board:
            result[t["id"]] = col_to_board[col_id]
    return result


async def _enrich_task(task: dict, board_name: str = "", column_name: str = "", sticker_names: list[str] | None = None) -> dict:
    """Fetch chat messages + file URLs for a YouGile task."""
    from app.yougile import get_task_comments

    task_id = task.get("id", "")
    title = task.get("title", "")
    task_common_id = task.get("idTaskCommon", "")
    description = task.get("description", "")

    messages_text: list[str] = []
    file_urls: list[str] = []
    try:
        comments = await get_task_comments(task_id, limit=20)
        for msg in comments:
            text = msg.get("text", "")
            if not text:
                continue
            if text.startswith("/root/#file:"):
                file_urls.append(text)
            else:
                messages_text.append(text.strip())
    except Exception as e:
        log.warning(f"Failed to get comments for task {task_id}: {e}")

    # Combine: title + description + chat messages
    parts = [title]
    if description:
        # Strip HTML tags crudely
        import re
        clean_desc = re.sub(r"<[^>]+>", " ", description).strip()
        if clean_desc:
            parts.append(clean_desc)
    if messages_text:
        parts.append("\n".join(messages_text))
    combined_text = "\n".join(parts)

    return {
        "id": task_id,
        "title": title,
        "task_common_id": task_common_id,
        "board": board_name,
        "column": column_name,
        "sticker_names": sticker_names or [],
        "messages": messages_text,
        "has_files": len(file_urls) > 0,
        "file_urls": file_urls,
        "combined_text": combined_text,
    }


async def scan_yougile() -> ServiceCheckResult:
    """Scan YouGile using chain filter rules.

    Rule structure (chain format):
      {
        "type": "chain",
        "chain": [
          {"kind": "filter", "field": "board", "op": "eq", "value": "..."},
          {"kind": "op", "value": "AND"},
          {"kind": "filter", "field": "title", "op": "contains", "value": "фасад"},
          {"kind": "op", "value": "NOT"},
          {"kind": "filter", "field": "column", "op": "eq", "value": "..."},
        ]
      }

    Legacy formats (composite, column_watch, keyword_filter) are auto-converted.
    """
    try:
        from app.settings_db import get_yougile_rules
        from app.yougile import build_sticker_map
    except ImportError:
        return ServiceCheckResult(source=Source.YOUGILE, error="Import failed")

    rules = await get_yougile_rules()
    if not rules:
        return ServiceCheckResult(source=Source.YOUGILE, found=0, orders=[])

    sticker_map = await build_sticker_map()
    blacklist = await _get_blacklist()

    matched: list[dict] = []
    seen_ids: set[str] = set()

    for rule in rules:
        chain = _rule_to_chain(rule)
        if not chain:
            continue

        # Determine task scope from chain
        filters = [c for c in chain if c.get("kind") == "filter"]
        board_cond = next((f for f in filters if f.get("field") == "board" and f.get("op") == "eq"), None)
        column_cond = next((f for f in filters if f.get("field") == "column" and f.get("op") == "eq" and f.get("value") != "any"), None)

        known_board = None
        if board_cond:
            scope_board_id = board_cond["value"]
            try:
                tasks = await _collect_tasks_for_board(scope_board_id)
                from app.yougile import get_board
                known_board = await get_board(scope_board_id)
            except Exception as e:
                log.error(f"Failed to collect tasks for board {scope_board_id}: {e}")
                continue
        elif column_cond:
            from app.yougile import get_tasks as _get_tasks
            try:
                data = await _get_tasks(column_id=column_cond["value"], limit=50)
                tasks = data.get("content", [])
            except Exception as e:
                log.error(f"Failed to get tasks for column: {e}")
                continue
        else:
            try:
                tasks = await _collect_all_tasks_with_subtasks()
            except Exception as e:
                log.error(f"Failed to collect all tasks: {e}")
                continue

        need_lookup = not known_board and any(f.get("field") == "board" for f in filters)
        board_lookup = await _build_board_lookup(tasks, known_board=known_board) if need_lookup else ({t["id"]: known_board for t in tasks if t.get("id")} if known_board else {})

        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id", "")
            if not task_id or task_id in blacklist or task.get("archived"):
                continue
            if task_id in _seen_yougile_tasks or task_id in seen_ids:
                continue

            board_of_task = board_lookup.get(task_id)
            if not _evaluate_chain(chain, task, board_of_task, sticker_map):
                continue

            _seen_yougile_tasks.add(task_id)
            seen_ids.add(task_id)

            bname = (board_of_task or {}).get("title", "")
            sticker_names = await _resolve_stickers(task, sticker_map)
            enriched = await _enrich_task(task, board_name=bname, sticker_names=sticker_names)
            log.info(f"[scanner] Matched task: id={task_id}, title={task.get('title','')}, board={bname}, column={task.get('columnId','')}, done={task.get('completed',False)}, files={len(enriched.get('file_urls',[]))}")
            matched.append(enriched)

    log.info(f"YouGile scan: found {len(matched)} new tasks across {len(rules)} rules")
    return ServiceCheckResult(source=Source.YOUGILE, found=len(matched), orders=matched)


async def scan_telegram() -> ServiceCheckResult:
    """Show recent Telegram-sourced orders."""
    try:
        from app.database import get_orders
        orders = await get_orders(limit=100)
        tg_orders = [o for o in orders if o.source.value == "telegram"]
        recent = [
            {
                "order_code": o.order_code,
                "description": o.description[:80],
                "status": o.status.value,
                "date": o.created_at.strftime("%d.%m %H:%M"),
            }
            for o in tg_orders[:10]
        ]
        return ServiceCheckResult(source=Source.TELEGRAM, found=len(tg_orders), orders=recent)
    except Exception as e:
        log.error(f"Telegram scan failed: {e}")
        return ServiceCheckResult(source=Source.TELEGRAM, error=str(e))


async def scan_email(since_days: int | None = None) -> ServiceCheckResult:
    """Check email for messages with order codes."""
    if since_days is None:
        try:
            from app.settings_db import get_email_since_days
            since_days = await get_email_since_days()
        except Exception:
            since_days = 7
    try:
        from app.channels.email_checker import EmailChecker
        checker = EmailChecker()
        import asyncio
        loop = asyncio.get_event_loop()
        emails = await loop.run_in_executor(None, lambda: checker.check_new_emails(since_days=since_days))

        orders = []
        for mail in emails:
            orders.append({
                "subject": mail.get("subject", ""),
                "order_code": mail.get("order_code", ""),
                "date": mail.get("date", ""),
                "from": mail.get("from", ""),
                "uid": mail.get("uid", ""),
            })

        return ServiceCheckResult(source=Source.EMAIL, found=len(orders), orders=orders)
    except Exception as e:
        log.error(f"Email scan failed: {e}")
        return ServiceCheckResult(source=Source.EMAIL, error=str(e))
