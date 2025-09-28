from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery


def main_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить алерт", callback_data="menu:add_alert")],
            [InlineKeyboardButton(text="📋 Список алертов", callback_data="menu:list_alerts")],
        ]
    )