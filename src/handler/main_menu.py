from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

ADD_ALERT_BUTTON = "➕ Добавить алерт"
LIST_ALERTS_BUTTON = "📋 Список алертов"
PROGRESSIONS_BUTTON = "🗺 Сценарии прогресса"
AGENT_SETTINGS_BUTTON = "⚙ Настройки агента"


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PROGRESSIONS_BUTTON)],
            [
                KeyboardButton(text=ADD_ALERT_BUTTON),
                KeyboardButton(text=LIST_ALERTS_BUTTON),
            ],
            [KeyboardButton(text=AGENT_SETTINGS_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…"
    )
