import asyncio
import logging
import uvicorn
from src.config import settings
from src.service import bot_server
from src.handler import admin
from src.db.db_alerts import init_db
from aiogram import Bot, Dispatcher

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

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
    await init_db(settings.DB_PATH)
    bot = Bot(token=settings.BOT_TOKEN)
    bot_server.bot = bot
    dp = Dispatcher()
    dp.include_router(admin.router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logging.info("Bot session closed")

async def main():
    fastapi_task = asyncio.create_task(start_fastapi())
    bot_task = asyncio.create_task(start_bot())

    try:
        await asyncio.gather(fastapi_task, bot_task)
    except (KeyboardInterrupt, SystemExit):
        logging.info("Stopping tasks...")
        fastapi_task.cancel()
        bot_task.cancel()
        await asyncio.gather(fastapi_task, bot_task, return_exceptions=True)
        logging.info("All tasks stopped")

if __name__ == "__main__":
    asyncio.run(main())
