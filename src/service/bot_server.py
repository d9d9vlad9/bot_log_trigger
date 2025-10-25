import logging

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel
from aiogram import Bot
from src.config import settings
import aiosqlite

logger = logging.getLogger(__name__)

bot: Bot | None = None
api_app = FastAPI()

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

    msg = (
        f"🚨 Alert: {alert.alert_name} (#{alert.alert_id})\n"
        f"VM: {alert.vm_name or alert.vm_id}\n"
        f"```{alert.log_line}```"
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

    return {"status": "ok"}
