from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

ADD_ALERT_BUTTON = "➕ Добавить алерт"
LIST_ALERTS_BUTTON = "📋 Список алертов"
STATUS_BUTTON = "📡 Активный статус"
SCENARIO_LIST_BUTTON = "📜 Список сценариев"
SCENARIO_ADD_BUTTON = "➕ Добавить сценарий"
AGENT_SETTINGS_BUTTON = "⚙ Агенты и настройки"


def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=STATUS_BUTTON),
                KeyboardButton(text=SCENARIO_ADD_BUTTON),
                KeyboardButton(text=SCENARIO_LIST_BUTTON),
            ],
            [
                KeyboardButton(text=ADD_ALERT_BUTTON),
                KeyboardButton(text=LIST_ALERTS_BUTTON),
            ],
            [KeyboardButton(text=AGENT_SETTINGS_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…"
    )
