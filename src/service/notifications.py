from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot

from src.config import settings
from src.db.db_progressions import Scenario, ScenarioRuntime

logger = logging.getLogger(__name__)


def _format_dt(value: dt.datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _resolve_targets() -> tuple[list[int], list[int]]:
    primary = [settings.ALERT_CHAT_ID] if settings.ALERT_CHAT_ID else []
    fallback = settings.ADMIN_IDS if settings.ADMIN_IDS else []
    return primary, fallback


async def _dispatch_message(bot: Bot | None, message: str) -> bool:
    if bot is None:
        logger.error("Telegram bot is not initialized; cannot send notification.")
        return False

    primary, fallback = _resolve_targets()
    if not primary and not fallback:
        logger.warning("No Telegram chat IDs configured for notifications.")
        return False

    sent = False
    for chat_id in primary:
        try:
            await bot.send_message(chat_id, message)
            sent = True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to send notification to chat %s: %s", chat_id, exc)

    if sent:
        return True

    for chat_id in fallback:
        try:
            await bot.send_message(chat_id, f"[Fallback] {message}")
            sent = True
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Failed to send notification to fallback chat %s: %s", chat_id, exc)

    return sent


async def notify_timeout(
    bot: Bot | None,
    *,
    vm_id: str,
    scenario: Scenario,
    runtime: ScenarioRuntime | None,
    deadline_at: dt.datetime | None,
    triggered_at: dt.datetime,
) -> None:
    message = (
        "⏰ Тайм-аут сценария\n"
        f"VM: {vm_id}\n"
        f"Сценарий: {scenario.name} ({scenario.from_alert} → {scenario.to_alert})\n"
        f"Дедлайн: {_format_dt(deadline_at)}\n"
        f"Зафиксировано: {_format_dt(triggered_at)}"
    )
    if runtime:
        message += f"\nСледующий дедлайн: {_format_dt(runtime.deadline_at_utc)}"

    await _dispatch_message(bot, message)


async def notify_completion(
    bot: Bot | None,
    *,
    vm_id: str,
    finished_at: dt.datetime,
    last_scenario: Scenario,
) -> None:
    message = (
        "✅ Сценарий завершён\n"
        f"VM: {vm_id}\n"
        f"Последний переход: {last_scenario.name} "
        f"({last_scenario.from_alert} → {last_scenario.to_alert})\n"
        f"Время завершения: {_format_dt(finished_at)}"
    )
    await _dispatch_message(bot, message)
