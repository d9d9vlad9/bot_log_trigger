import re
from aiogram import Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command

from .main_menu import main_menu
from ..config import settings
from src.db.db_alerts import add_alert, list_alerts, remove_alert

router = Router()

async def require_admin(message: Message | CallbackQuery) -> bool:
    user_id = message.from_user.id
    if user_id not in settings.ADMIN_IDS:
        if isinstance(message, Message):
            await message.reply("❌ У вас нет доступа к этой команде.")
        else:
            await message.answer("❌ У вас нет доступа", show_alert=True)
        return False
    return True

@router.message(Command("start"))
async def cmd_start(message: Message):
    if not await require_admin(message):
        return
    await message.answer("Главное меню:", reply_markup=main_menu())


@router.callback_query(lambda c: c.data and c.data.startswith("menu:"))
async def menu_callback(callback: CallbackQuery):
    if not await require_admin(callback):
        return

    action = callback.data.split(":")[1]

    if action == "add_alert":
        await callback.message.answer("Используй команду:\n/add_alert <name> | <regex>")
        await callback.answer()
    elif action == "list_alerts":
        await show_alerts(callback)
        await callback.answer()

@router.message(Command("add_alert"))
async def cmd_add_alert(message: Message):
    if not await require_admin(message):
        return

    parts = message.text.split(None, 1)
    if len(parts) < 2 or "|" not in parts[1]:
        await message.reply("Используй: /add_alert <name> | <regex>")
        return

    name, pattern = [x.strip() for x in parts[1].split("|", 1)]
    try:
        re.compile(pattern)
    except re.error as e:
        await message.reply(f"Неверный regex: {e}")
        return

    aid = await add_alert(settings.DB_PATH, name, pattern)
    await message.reply(f"✅ Алерт '{name}' добавлен с id={aid}")

async def show_alerts(callback: CallbackQuery):
    rows = await list_alerts(settings.DB_PATH)
    if not rows:
        await callback.message.answer("Нет алертов.")
        return

    for r in rows:
        alert_id, name, pattern, enabled = r
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Удалить",
                        callback_data=f"remove_alert:{alert_id}"
                    )
                ]
            ]
        )
        await callback.message.answer(
            f"{alert_id}: {name} (pattern: {pattern}) enabled={enabled}",
            reply_markup=keyboard
        )

@router.callback_query(lambda c: c.data and c.data.startswith("remove_alert:"))
async def callback_remove_alert(callback: CallbackQuery):
    if not await require_admin(callback):
        return

    alert_id = int(callback.data.split(":")[1])
    ok = await remove_alert(settings.DB_PATH, alert_id)
    await callback.message.edit_text(
        f"✅ Алерт с id={alert_id} удален" if ok else f"❌ Алерт с id={alert_id} не найден"
    )
    await callback.answer()