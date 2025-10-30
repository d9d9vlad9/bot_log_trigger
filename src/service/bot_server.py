import datetime as dt
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple

import aiosqlite
from aiogram import Bot
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from src.config import settings
from src.db.db_alerts import init_db
from src.db.db_progressions import (
    Scenario,
    ScenarioRuntime,
    init_progressions_db,
)
from src.service.notifications import notify_completion
from src.service.progressions import (
    AlertHandlingResult,
    ScenarioRuntimeError,
    ScenarioRuntimeService,
)


@dataclass
class AlertConfig:
    id: int
    name: str
    enabled: bool
    threshold_count: int
    threshold_window_seconds: int
    is_scenario_trigger: bool


logger = logging.getLogger(__name__)

bot: Bot | None = None
api_app = FastAPI()
_alert_windows: Dict[Tuple[int, str | None], Deque[float]] = {}
runtime_service = ScenarioRuntimeService(settings.DB_PATH)


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
    is_scenario_trigger: bool


@api_app.on_event("startup")
async def ensure_database_initialized() -> None:
    await init_db(settings.DB_PATH)
    await init_progressions_db(settings.DB_PATH)


def verify_alert_token(
    token: str | None = Header(default=None, alias="X-Alert-Token"),
) -> None:
    expected = settings.ALERT_TOKEN
    if expected and token != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid alert token",
        )


def _serialize_datetime(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _serialize_scenario(obj: Scenario | None) -> dict | None:
    if obj is None:
        return None
    return {
        "id": obj.id,
        "name": obj.name,
        "from_alert": obj.from_alert,
        "to_alert": obj.to_alert,
        "timeout_minutes": obj.timeout_minutes,
        "enabled": obj.enabled,
    }


def _serialize_runtime(obj: ScenarioRuntime | None) -> dict | None:
    if obj is None:
        return None
    return {
        "vm_id": obj.vm_id,
        "current_scenario_id": obj.current_scenario_id,
        "deadline_at_utc": _serialize_datetime(obj.deadline_at_utc),
        "status": obj.status,
        "last_received_alert": obj.last_received_alert,
        "created_at_utc": _serialize_datetime(obj.created_at_utc),
        "updated_at_utc": _serialize_datetime(obj.updated_at_utc),
    }


def _serialize_alert_result(result: AlertHandlingResult | None) -> dict | None:
    if result is None:
        return None
    return {
        "vm_id": result.vm_id,
        "outcome": result.outcome,
        "message": result.message,
        "scenario": _serialize_scenario(result.scenario),
        "next_scenario": _serialize_scenario(result.next_scenario),
        "runtime": _serialize_runtime(result.runtime),
    }


async def _load_alert_config(alert_id: int) -> AlertConfig | None:
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute(
            """
            SELECT
                id,
                name,
                enabled,
                threshold_count,
                threshold_window_seconds,
                is_scenario_trigger
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
        is_scenario_trigger=bool(row[5]),
    )


@api_app.get("/alerts", response_model=list[AlertOut])
async def get_alerts(
    vm_id: str | None = None,
    vm_name: str | None = None,
    _: None = Depends(verify_alert_token),
):
    if vm_id:
        try:
            await runtime_service.record_agent_heartbeat(vm_id, vm_name)
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Failed to record agent heartbeat for vm=%s: %s", vm_id, exc)
    async with aiosqlite.connect(settings.DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, name, pattern, is_scenario_trigger FROM alerts WHERE enabled = 1"
        )
        rows = await cursor.fetchall()
        await cursor.close()
    return [
        {
            "id": r[0],
            "name": r[1],
            "pattern": r[2],
            "is_scenario_trigger": bool(r[3]),
        }
        for r in rows
    ]


@api_app.post("/log_alert")
async def log_alert(alert: LogAlert, _: None = Depends(verify_alert_token)):
    now_utc = dt.datetime.now(dt.timezone.utc)
    try:
        await runtime_service.record_agent_heartbeat(alert.vm_id, alert.vm_name, seen_at=now_utc)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Failed to record agent heartbeat from log_alert for vm=%s: %s", alert.vm_id, exc)

    config = await _load_alert_config(alert.alert_id)
    if config is None:
        logger.warning("Received alert for unknown id=%s", alert.alert_id)
        return {
            "status": "ignored",
            "reason": "unknown_alert",
            "scenario": None,
        }

    if not config.enabled:
        logger.info("Alert id=%s disabled. Skipping notification.", alert.alert_id)
        return {
            "status": "ignored",
            "reason": "disabled",
            "scenario": None,
        }

    threshold_count = max(1, config.threshold_count)
    window_seconds = max(1, config.threshold_window_seconds)
    key = (config.id, alert.vm_id)
    bucket = _alert_windows.setdefault(key, deque())

    now_monotonic = time.monotonic()

    while bucket and now_monotonic - bucket[0] >= window_seconds:
        bucket.popleft()

    bucket.append(now_monotonic)

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
            "scenario": None,
        }

    current_count = len(bucket)
    bucket.clear()

    alert_name = config.name or alert.alert_name

    scenario_result: AlertHandlingResult | None = None
    try:
        scenario_result = await runtime_service.handle_alert(
            vm_id=alert.vm_id,
            alert_name=alert_name,
            now=now_utc,
        )
        if scenario_result.outcome != "ignored":
            logger.info(
                "Scenario update for vm=%s: outcome=%s message=%s",
                alert.vm_id,
                scenario_result.outcome,
                scenario_result.message,
            )
    except ScenarioRuntimeError as exc:
        logger.exception(
            "Failed to process scenario runtime for vm=%s: %s",
            alert.vm_id,
            exc,
        )

    scenario_payload = _serialize_alert_result(scenario_result)

    bot_instance = bot
    if (
        scenario_result
        and scenario_result.outcome == "completed"
        and scenario_result.scenario is not None
    ):
        await notify_completion(
            bot_instance,
            vm_id=alert.vm_id,
            finished_at=now_utc,
            last_scenario=scenario_result.scenario,
        )

    if config.is_scenario_trigger:
        if scenario_result and scenario_result.outcome == "ignored":
            logger.info(
                "Scenario trigger received for vm=%s but no matching scenario configured.",
                alert.vm_id,
            )
        return {
            "status": "scenario_trigger",
            "scenario": scenario_payload,
        }

    msg = (
        f"🚨 Alert: {alert_name} (#{config.id})\n"
        f"VM: {alert.vm_name or alert.vm_id}\n"
        f"Порог достигнут: {threshold_count} события(ий) за {window_seconds} сек.\n"
        f"Совпадений подряд: {current_count}\n"
        f"Последняя строка:\n```{alert.log_line}```"
    )

    if scenario_result and scenario_result.outcome in {
        "started",
        "progressed",
        "completed",
    }:
        msg += "\n\n📈 " + scenario_result.message
        deadline = (
            scenario_result.runtime.deadline_at_utc
            if scenario_result.runtime
            else None
        )
        if scenario_result.next_scenario is not None:
            msg += (
                f"\n→ Ожидаем: {scenario_result.next_scenario.to_alert}"
                f" до {_serialize_datetime(deadline) if deadline else '—'}"
            )
        elif deadline is not None:
            msg += f"\n→ Дедлайн: {_serialize_datetime(deadline)}"

    primary_targets: list[int] = (
        [settings.ALERT_CHAT_ID] if settings.ALERT_CHAT_ID else []
    )
    fallback_targets: list[int] = settings.ADMIN_IDS if settings.ADMIN_IDS else []

    if not primary_targets and not fallback_targets:
        logger.warning("No target chat IDs configured for alerts.")
        return {
            "warning": "No target chat IDs configured",
            "scenario": scenario_payload,
        }

    if bot_instance is None:
        logger.error("Bot not initialized. Cannot send alert to Telegram.")
        return (
            {
                "error": "Bot not initialized",
                "scenario": scenario_payload,
            },
            503,
        )

    sent = False
    for chat_id in primary_targets:
        try:
            await bot_instance.send_message(chat_id, msg)
            sent = True
        except Exception as exc:
            logger.error(
                "Failed to send alert to primary chat %s: %s",
                chat_id,
                exc,
            )

    if not sent:
        for admin_id in fallback_targets:
            try:
                await bot_instance.send_message(admin_id, f"[Fallback] {msg}")
            except Exception as exc:
                logger.error(
                    "Failed to send alert to admin %s: %s",
                    admin_id,
                    exc,
                )

    return {
        "status": "sent",
        "scenario": scenario_payload,
    }
