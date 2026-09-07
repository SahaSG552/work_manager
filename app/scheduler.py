"""Background scheduler for auto-mode scanning of services."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.models import Source
from app.scanner import scan_yougile, scan_email
from app.processor import process_message

log = logging.getLogger(__name__)

# Default scan intervals (seconds)
_INTERVALS = {
    Source.YOUGILE: 300,   # 5 min
    Source.EMAIL: 600,     # 10 min
    Source.TELEGRAM: 0,    # TG is real-time via bot, no polling needed
}

_running = False


async def _scan_and_process(source: Source) -> int:
    """Run a scan for a specific source and create pending orders for any new findings."""
    if source == Source.YOUGILE:
        result = await scan_yougile()
    elif source == Source.EMAIL:
        result = await scan_email()
    else:
        return 0

    if result.error:
        log.error(f"{source.value} scan error: {result.error}")
        return 0

    created = 0
    for item in result.orders:
        order_code = item.get("order_code", "")
        title = item.get("title", item.get("subject", ""))

        if not order_code and not title:
            continue

        # Build text for parsing
        text = title
        if order_code:
            text = f"{order_code} {text}"

        try:
            # For YouGile: title IS the order code — pass it as override to prevent reformatting
            code_override = title if source == Source.YOUGILE and title else None
            yg_task_id = item.get("id") if source == Source.YOUGILE else None
            await process_message(text=text, source=source, auto_mode=True, order_code_override=code_override, yougile_task_id=yg_task_id)
            created += 1
        except Exception as e:
            log.error(f"Failed to process {source.value} item: {e}")

    log.info(f"{source.value} scan: found={result.found}, created={created}")
    return created


async def _scheduler_loop() -> None:
    """Main scheduler loop — runs when auto_mode is enabled."""
    global _running
    _running = True
    log.info("Auto-mode scheduler started")

    while _running:
        for source, interval in _INTERVALS.items():
            if not _running:
                break
            if interval <= 0:
                continue
            try:
                await _scan_and_process(source)
            except Exception as e:
                log.error(f"Scheduler error ({source.value}): {e}")

        # Wait before next cycle (use shortest interval)
        min_interval = min(v for v in _INTERVALS.values() if v > 0)
        for _ in range(min_interval):
            if not _running:
                break
            await asyncio.sleep(1)

    log.info("Auto-mode scheduler stopped")


def start_scheduler() -> asyncio.Task:
    """Start the background scheduler. Returns the task."""
    return asyncio.create_task(_scheduler_loop())


def stop_scheduler() -> None:
    """Stop the background scheduler."""
    global _running
    _running = False


def is_running() -> bool:
    """Check if the scheduler is running."""
    return _running
