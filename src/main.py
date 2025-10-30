import asyncio
import datetime as dt
import logging

import uvicorn
from aiogram import Bot, Dispatcher

from src.config import settings
from src.db.db_alerts import init_db as init_alerts_db
from src.db.db_progressions import init_progressions_db
from src.handler import admin
from src.service import bot_server
from src.service.notifications import notify_timeout
from src.service.progressions import ScenarioRuntimeService

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

async def init_databases():
    await init_alerts_db(settings.DB_PATH)
    await init_progressions_db(settings.DB_PATH)

async def start_fastapi():
    config = uvicorn.Config(
        bot_server.api_app,
        host=settings.API_HOST,
        port=settings.API_PORT,
        log_level=settings.LOG_LEVEL.lower()
    )
    server = uvicorn.Server(config)
    await server.serve()

async def start_bot():
    bot = Bot(token=settings.BOT_TOKEN)
    bot_server.bot = bot
    dp = Dispatcher()
    dp.include_router(admin.router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logging.info("Bot session closed")

async def timeout_monitor():
    service = ScenarioRuntimeService(settings.DB_PATH)
    poll_seconds = max(1.0, float(settings.PROGRESS_TIMEOUT_POLL_SECONDS))
    logger = logging.getLogger("timeout_monitor")
    while True:
        now = dt.datetime.now(dt.timezone.utc)
        try:
            due_runtimes = await service.list_due_runtimes(now=now)
            for runtime in due_runtimes:
                result = await service.handle_timeout(runtime.vm_id, now=now)
                if result.timed_out and result.scenario is not None:
                    await notify_timeout(
                        bot_server.bot,
                        vm_id=runtime.vm_id,
                        scenario=result.scenario,
                        runtime=result.runtime,
                        deadline_at=runtime.deadline_at_utc,
                        triggered_at=now,
                    )
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("Timeout monitor iteration failed: %s", exc)

        await asyncio.sleep(poll_seconds)

async def main():
    await init_databases()
    fastapi_task = asyncio.create_task(start_fastapi())
    bot_task = asyncio.create_task(start_bot())
    monitor_task = asyncio.create_task(timeout_monitor())

    try:
        await asyncio.gather(fastapi_task, bot_task, monitor_task)
    except (KeyboardInterrupt, SystemExit):
        logging.info("Stopping tasks...")
        fastapi_task.cancel()
        bot_task.cancel()
        monitor_task.cancel()
        await asyncio.gather(
            fastapi_task,
            bot_task,
            monitor_task,
            return_exceptions=True,
        )
        logging.info("All tasks stopped")

if __name__ == "__main__":
    asyncio.run(main())
