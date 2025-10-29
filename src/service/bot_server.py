import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel
from aiogram import Bot
import aiosqlite
from src.config import settings

@dataclass
class AlertConfig:
    id: int
    name: str
    enabled: bool
    threshold_count: int
    threshold_window_seconds: int

logger = logging.getLogger(__name__)

bot: Bot | None = None
api_app = FastAPI()
_alert_windows: Dict[Tuple[int, str | None], Deque[float]] = {}

class LogAlert(BaseModel):
    vm_id: str
    vm_name: str | None
    alert_name: str
    alert_id: int
    log_line: str

class AlertOut(BaseModel):
    id: int
    name: str
    pattern: str

async def _load_alert_config(alert_id: int) -> AlertConfig | None:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                name,
                enabled,
                threshold_count,
                threshold_window_seconds
            FROM alerts
            WHERE id = ?
            """,
            (alert_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()

    if row is None:
        return None

    return AlertConfig(
        id=row[0],
        name=row[1],
        enabled=bool(row[2]),
        threshold_count=row[3] or 1,
        threshold_window_seconds=row[4] or 60,
    )

def verify_alert_token(token: str | None = Header(default=None, alias="X-Alert-Token")) -> None:
    expected = settings.ALERT_TOKEN
    if expected and token != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid alert token")

@api_app.get("/alerts", response_model=list[AlertOut])
async def get_alerts(_: None = Depends(verify_alert_token)):
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute("SELECT id, name, pattern FROM alerts WHERE enabled = 1")
        rows = await cursor.fetchall()
        await cursor.close()
    return [{"id": r[0], "name": r[1], "pattern": r[2]} for r in rows]

@api_app.post("/log_alert")
async def log_alert(alert: LogAlert, _: None = Depends(verify_alert_token)):
    if bot is None:
        logger.error("Bot not initialized. Cannot send alert.")
        return {"error": "Bot not initialized"}, 503

    config = await _load_alert_config(alert.alert_id)
    if config is None:
        logger.warning("Received alert for unknown id=%s", alert.alert_id)
        return {"status": "ignored", "reason": "unknown_alert"}

    if not config.enabled:
        logger.info("Alert id=%s disabled. Skipping notification.", alert.alert_id)
        return {"status": "ignored", "reason": "disabled"}

    threshold_count = max(1, config.threshold_count)
    window_seconds = max(1, config.threshold_window_seconds)
    key = (config.id, alert.vm_id)
    bucket = _alert_windows.setdefault(key, deque())

    now = time.monotonic()

    while bucket and now - bucket[0] >= window_seconds:
        bucket.popleft()

    bucket.append(now)

    if len(bucket) < threshold_count:
        logger.debug(
            "Buffering alert id=%s for vm=%s (%s/%s within %ss)",
            alert.alert_id,
            alert.vm_id,
            len(bucket),
            threshold_count,
            window_seconds,
        )
        return {
            "status": "buffering",
            "count": len(bucket),
            "threshold": threshold_count,
            "window_seconds": window_seconds,
        }

    current_count = len(bucket)
    bucket.clear()

    alert_name = config.name or alert.alert_name
    msg = (
        f"🚨 Alert: {alert_name} (#{config.id})\n"
        f"VM: {alert.vm_name or alert.vm_id}\n"
        f"Порог достигнут: {threshold_count} события(ий) за {window_seconds} сек.\n"
        f"Совпадений подряд: {current_count}\n"
        f"Последняя строка:\n```{alert.log_line}```"
    )

    primary_targets: list[int] = [settings.ALERT_CHAT_ID] if settings.ALERT_CHAT_ID else []
    fallback_targets: list[int] = settings.ADMIN_IDS if settings.ADMIN_IDS else []

    if not primary_targets and not fallback_targets:
        logger.warning("No target chat IDs configured for alerts.")
        return {"warning": "No target chat IDs configured"}

    sent = False

    for chat_id in primary_targets:
        try:
            await bot.send_message(chat_id, msg)
            sent = True
        except Exception as e:
            logger.error(f"Failed to send alert to primary chat {chat_id}: {e}")

    if not sent:
        for admin_id in fallback_targets:
            try:
                await bot.send_message(admin_id, f"[Fallback] {msg}")
            except Exception as e:
                logger.error(f"Failed to send alert to admin {admin_id}: {e}")

    return {"status": "sent"}
