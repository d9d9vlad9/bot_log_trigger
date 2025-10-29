import math
import re
from dataclasses import dataclass, field
from typing import Dict, List

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from .main_menu import (
    ADD_ALERT_BUTTON,
    AGENT_SETTINGS_BUTTON,
    LIST_ALERTS_BUTTON,
    PROGRESSIONS_BUTTON,
    main_menu,
)
from ..config import settings
from src.db.db_alerts import (
    add_alert,
    get_alert,
    list_alerts,
    remove_alert,
    toggle_alert_enabled,
    update_alert_thresholds,
)

router = Router()

BACK_BUTTON = "⬅ Назад"
MAIN_MENU_BUTTON = "🏠 Главное меню"
PREV_PAGE_BUTTON = "◀️ Предыдущие"
NEXT_PAGE_BUTTON = "Следующие ▶️"
LIST_PAGE_SIZE = 6

TOGGLE_ALERT_BUTTON = "🔁 Переключить активность"
EDIT_LIMIT_BUTTON = "✏️ Порог срабатываний"
EDIT_WINDOW_BUTTON = "⏱ Интервал наблюдения"
DELETE_ALERT_BUTTON = "🗑 Удалить алерт"
CONFIRM_DELETE_BUTTON = "✅ Подтвердить удаление"


@dataclass
class AlertRecord:
    id: int
    name: str
    pattern: str
    enabled: int
    threshold_count: int
    threshold_window_seconds: int

    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled)


@dataclass
class AddAlertSession:
    step: str
    name: str | None = None
    pattern: str | None = None
    threshold_count: int = 1
    window_seconds: int = 60
    history: list[str] = field(default_factory=list)


@dataclass
class EditAlertSession:
    alert: AlertRecord
    step: str = "menu"
    pending_delete: bool = False


_add_alert_sessions: Dict[int, AddAlertSession] = {}
_alert_list_cache: Dict[int, Dict[str, AlertRecord]] = {}
_alert_list_order: Dict[int, List[str]] = {}
_alert_list_pages: Dict[int, int] = {}
_alert_edit_sessions: Dict[int, EditAlertSession] = {}


async def require_admin(message: Message) -> bool:
    user_id = message.from_user.id
    if user_id not in settings.ADMIN_IDS:
        await message.reply("❌ У вас нет доступа к этой команде.")
        return False
    return True


@router.message(Command("start"))
async def cmd_start(message: Message):
    if not await require_admin(message):
        return
    await message.answer("Главное меню:", reply_markup=main_menu())


@router.message(F.text == ADD_ALERT_BUTTON)
async def menu_add_alert(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    _alert_list_cache.pop(user_id, None)
    _alert_edit_sessions.pop(user_id, None)

    session = AddAlertSession(step="name")
    _add_alert_sessions[user_id] = session
    await _prompt_add_step(message, session)


@router.message(F.text == LIST_ALERTS_BUTTON)
async def menu_list_alerts(message: Message):
    if not await require_admin(message):
        return
    await _open_alerts_menu(message)


@router.message(F.text == PROGRESSIONS_BUTTON)
async def menu_progressions(message: Message):
    if not await require_admin(message):
        return
    await message.answer(
        (
            "⚙ Управление сценариями прогресса выполняется через API и "
            "кнопку «🗺 Сценарии прогресса» в Telegram. В этом боте "
            "список пока недоступен."
        ),
        reply_markup=main_menu(),
    )


@router.message(F.text == AGENT_SETTINGS_BUTTON)
async def menu_agent_settings(message: Message):
    if not await require_admin(message):
        return
    await message.answer(
        (
            "Настройки агента находятся в файле agent/config.json. "
            "Убедись, что AUTH_TOKEN совпадает с ALERT_TOKEN на сервере."
        ),
        reply_markup=main_menu(),
    )


@router.message(lambda m: m.from_user and m.from_user.id in _add_alert_sessions)
async def handle_add_alert_step(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    session = _add_alert_sessions[user_id]
    text = (message.text or "").strip()

    if text == MAIN_MENU_BUTTON:
        del _add_alert_sessions[user_id]
        await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == BACK_BUTTON:
        if session.history:
            session.step = session.history.pop()
            await _prompt_add_step(message, session)
        else:
            del _add_alert_sessions[user_id]
            await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if session.step == "name":
        if not text:
            await _send_step_prompt(
                message,
                "Название не должно быть пустым. Введите название алерта.",
            )
            return
        session.name = text
        session.history.append("name")
        session.step = "pattern"
        await _prompt_add_step(message, session)
        return

    if session.step == "pattern":
        try:
            re.compile(text)
        except re.error as exc:
            await _send_step_prompt(
                message,
                f"Неверный regex: {exc}. Попробуйте снова.",
            )
            return
        session.pattern = text
        session.history.append("pattern")
        session.step = "threshold_count"
        await _prompt_add_step(message, session)
        return

    if session.step == "threshold_count":
        value = _parse_int(text)
        if value is None:
            await message.answer(
                "Лимит событий должен быть положительным целым числом."
            )
            await _prompt_add_step(message, session)
            return
        session.threshold_count = value
        session.history.append("threshold_count")
        session.step = "window_seconds"
        await _prompt_add_step(message, session)
        return

    if session.step == "window_seconds":
        window = _parse_window(text)
        if window is None:
            await message.answer(
                "Окно указывается целым числом секунд или с суффиксом s/m (например, 300 или 5m)."
            )
            await _prompt_add_step(message, session)
            return
        session.window_seconds = window

        aid = await add_alert(
            settings.DB_PATH,
            session.name or "Alert",
            session.pattern or "",
            threshold_count=session.threshold_count,
            threshold_window_seconds=session.window_seconds,
        )
        del _add_alert_sessions[user_id]
        await message.answer(
            (
                f"✅ Алерт '{session.name}' добавлен с id={aid}. "
                f"Порог: {session.threshold_count} событий за "
                f"{session.window_seconds} сек."
            ),
            reply_markup=main_menu(),
        )


@router.message(lambda m: m.from_user and m.from_user.id in _alert_edit_sessions)
async def handle_alert_edit(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    session = _alert_edit_sessions[user_id]
    text = (message.text or "").strip()

    if text == MAIN_MENU_BUTTON:
        _alert_edit_sessions.pop(user_id, None)
        _alert_list_cache.pop(user_id, None)
        _alert_list_order.pop(user_id, None)
        _alert_list_pages.pop(user_id, None)
        await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == BACK_BUTTON:
        if session.step == "menu":
            _alert_edit_sessions.pop(user_id, None)
            await _send_alert_list_page(message, user_id, include_back=False)
        elif session.step in {"edit_threshold", "edit_window"}:
            session.step = "menu"
            await _send_alert_details(message, session.alert)
        elif session.step == "confirm_delete":
            session.step = "menu"
            session.pending_delete = False
            await _send_alert_details(message, session.alert)
        else:
            _alert_edit_sessions.pop(user_id, None)
            await _send_alert_list_page(message, user_id, include_back=False)
        return

    if session.step == "menu":
        if text == TOGGLE_ALERT_BUTTON:
            toggled = await toggle_alert_enabled(settings.DB_PATH, session.alert.id)
            if toggled is None:
                await message.answer(
                    "Алерт не найден. Список будет обновлён.",
                    reply_markup=main_menu(),
                )
                _alert_edit_sessions.pop(user_id, None)
                await _open_alerts_menu(message)
                return
            record = await _load_alert(session.alert.id)
            if record is None:
                await message.answer(
                    "Не удалось обновить состояние алерта.",
                    reply_markup=main_menu(),
                )
                _alert_edit_sessions.pop(user_id, None)
                return
            session.alert = record
            _update_alert_cache(user_id, record)
            status_text = "включён" if record.is_enabled else "выключен"
            await _send_alert_details(
                message,
                record,
                prefix=f"Статус обновлён: {status_text}",
            )
            return

        if text == EDIT_LIMIT_BUTTON:
            session.step = "edit_threshold"
            await _send_step_prompt(
                message,
                (
                    "Новый лимит событий (целое число). "
                    f"Текущее значение: {session.alert.threshold_count}"
                ),
            )
            return

        if text == EDIT_WINDOW_BUTTON:
            session.step = "edit_window"
            await _send_step_prompt(
                message,
                (
                    "Новое окно в секундах или в формате 5m/30s. "
                    f"Текущее значение: {session.alert.threshold_window_seconds} сек."
                ),
            )
            return

        if text == DELETE_ALERT_BUTTON:
            session.pending_delete = True
            session.step = "confirm_delete"
            await _send_step_prompt(
                message,
                f"Удалить алерт '{session.alert.name}'? Это действие необратимо.",
                confirm_button=CONFIRM_DELETE_BUTTON,
            )
            return

        await message.answer(
            "Выберите действие из меню или воспользуйтесь кнопками ниже.",
            reply_markup=_alert_actions_keyboard(),
        )
        return

    if session.step == "edit_threshold":
        value = _parse_int(text)
        if value is None:
            await _send_step_prompt(
                message,
                "Введите положительное целое число или нажмите «⬅ Назад».",
            )
            return
        updated = await update_alert_thresholds(
            settings.DB_PATH,
            session.alert.id,
            threshold_count=value,
        )
        if not updated:
            await message.answer(
                "Не удалось обновить лимит. Попробуйте позже.",
                reply_markup=_alert_actions_keyboard(include_back=True),
            )
            session.step = "menu"
            return
        record = await _load_alert(session.alert.id)
        if record is None:
            await message.answer(
                "Алерт не найден. Список будет обновлён.",
                reply_markup=main_menu(),
            )
            _alert_edit_sessions.pop(user_id, None)
            return
        session.alert = record
        session.step = "menu"
        _update_alert_cache(user_id, record)
        await _send_alert_details(
            message,
            record,
            prefix=f"Лимит обновлён: {record.threshold_count} события(-ий).",
        )
        return

    if session.step == "edit_window":
        window = _parse_window(text)
        if window is None:
            await _send_step_prompt(
                message,
                (
                    "Введите целое число секунд или значение с суффиксом s/m. "
                    "Например: 300 или 5m."
                ),
            )
            return
        updated = await update_alert_thresholds(
            settings.DB_PATH,
            session.alert.id,
            threshold_window_seconds=window,
        )
        if not updated:
            await message.answer(
                "Не удалось обновить окно. Попробуйте позже.",
                reply_markup=_alert_actions_keyboard(include_back=True),
            )
            session.step = "menu"
            return
        record = await _load_alert(session.alert.id)
        if record is None:
            await message.answer(
                "Алерт не найден. Список будет обновлён.",
                reply_markup=main_menu(),
            )
            _alert_edit_sessions.pop(user_id, None)
            return
        session.alert = record
        session.step = "menu"
        _update_alert_cache(user_id, record)
        await _send_alert_details(
            message,
            record,
            prefix=(
                "Окно обновлено: "
                f"{record.threshold_window_seconds} сек."
            ),
        )
        return

    if session.step == "confirm_delete":
        if text == CONFIRM_DELETE_BUTTON:
            deleted = await remove_alert(settings.DB_PATH, session.alert.id)
            _alert_edit_sessions.pop(user_id, None)
            _alert_list_cache.pop(user_id, None)
            _alert_list_order.pop(user_id, None)
            _alert_list_pages.pop(user_id, None)
            if deleted:
                await message.answer(
                    f"✅ Алерт '{session.alert.name}' удалён.",
                    reply_markup=main_menu(),
                )
            else:
                await message.answer(
                    "Алерт не найден или уже удалён.",
                    reply_markup=main_menu(),
                )
            return

        await message.answer(
            "Нажмите «✅ Подтвердить удаление» или используйте кнопку «⬅ Назад».",
            reply_markup=_navigation_keyboard(confirm_button=CONFIRM_DELETE_BUTTON),
        )
        return


@router.message(lambda m: m.from_user and m.from_user.id in _alert_list_cache)
async def handle_alert_list_navigation(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    mapping = _alert_list_cache[user_id]
    text = (message.text or "").strip()

    if text == MAIN_MENU_BUTTON:
        _alert_list_cache.pop(user_id, None)
        _alert_list_order.pop(user_id, None)
        _alert_list_pages.pop(user_id, None)
        await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == BACK_BUTTON:
        await _send_alert_list_page(message, user_id, include_back=False)
        return

    if text == NEXT_PAGE_BUTTON:
        labels = _alert_list_order.get(user_id, [])
        if labels:
            total_pages = max(1, math.ceil(len(labels) / LIST_PAGE_SIZE))
            page = min(
                _alert_list_pages.get(user_id, 0) + 1,
                total_pages - 1,
            )
            _alert_list_pages[user_id] = page
        await _send_alert_list_page(message, user_id, include_back=False)
        return

    if text == PREV_PAGE_BUTTON:
        labels = _alert_list_order.get(user_id, [])
        if labels:
            page = max(_alert_list_pages.get(user_id, 0) - 1, 0)
            _alert_list_pages[user_id] = page
        await _send_alert_list_page(message, user_id, include_back=False)
        return

    record = mapping.get(text)
    if record is None:
        await message.answer("Не удалось распознать выбор.")
        await _send_alert_list_page(message, user_id, include_back=False)
        return

    _alert_edit_sessions[user_id] = EditAlertSession(alert=record)
    await _send_alert_details(message, record)


async def _open_alerts_menu(message: Message) -> None:
    user_id = message.from_user.id
    _add_alert_sessions.pop(user_id, None)
    _alert_edit_sessions.pop(user_id, None)

    rows = await list_alerts(settings.DB_PATH)
    if not rows:
        _alert_list_cache.pop(user_id, None)
        await message.answer("Нет алертов.", reply_markup=main_menu())
        return

    records = [_row_to_record(row) for row in rows]
    labels = [_alert_label(record) for record in records]
    mapping = dict(zip(labels, records))
    _alert_list_cache[user_id] = mapping
    _alert_list_order[user_id] = labels
    _alert_list_pages[user_id] = 0

    await _send_alert_list_page(message, user_id, include_back=False)


async def _prompt_add_step(message: Message, session: AddAlertSession) -> None:
    prompts: dict[str, str] = {
        "name": "Введите название алерта.",
        "pattern": "Введи регулярное выражение (используется модуль re).",
        "threshold_count": "Введите лимит событий (целое число, например 1).",
        "window_seconds": "Введите окно (в секундах или формате 5m/30s, например 300).",
    }

    text = prompts.get(session.step, "Введите значение.")

    if session.step == "name" and session.name:
        text += f"\nТекущее значение: {session.name}"
    elif session.step == "pattern" and session.pattern:
        text += f"\nТекущий шаблон: {session.pattern}"
    elif session.step == "threshold_count":
        text += f"\nТекущее значение: {session.threshold_count}"
    elif session.step == "window_seconds":
        text += f"\nТекущее значение: {session.window_seconds} сек."

    await _send_step_prompt(
        message,
        text,
        include_back=bool(session.history),
    )

async def _send_alert_list_page(
    message: Message,
    user_id: int,
    *,
    include_back: bool,
) -> None:
    labels = _alert_list_order.get(user_id, [])
    mapping = _alert_list_cache.get(user_id, {})
    if not labels or not mapping:
        await message.answer("Нет алертов.", reply_markup=main_menu())
        return

    total_pages = max(1, math.ceil(len(labels) / LIST_PAGE_SIZE))
    page = _alert_list_pages.get(user_id, 0)
    if page < 0:
        page = 0
    if page > total_pages - 1:
        page = total_pages - 1
    _alert_list_pages[user_id] = page

    start = page * LIST_PAGE_SIZE
    end = start + LIST_PAGE_SIZE
    page_labels = labels[start:end]
    page_records = [mapping[label] for label in page_labels]

    text_lines = [f"📋 Алерты (стр. {page + 1}/{total_pages}):"]
    for record in page_records:
        text_lines.append(_alert_overview_line(record))
    text_lines.append("")
    text_lines.append("Выберите алерт из клавиатуры или воспользуйтесь кнопками ниже.")

    keyboard = ReplyKeyboardMarkup(
        keyboard=_alert_list_keyboard(
            page_labels,
            page=page,
            total_pages=total_pages,
            include_back=include_back,
        ),
        resize_keyboard=True,
        input_field_placeholder="Выберите алерт…",
    )

    await message.answer("\n".join(text_lines), reply_markup=keyboard)


async def _send_step_prompt(
    message: Message,
    text: str,
    *,
    confirm_button: str | None = None,
    include_back: bool = True,
) -> None:
    await message.answer(
        text,
        reply_markup=_navigation_keyboard(
            confirm_button=confirm_button,
            include_back=include_back,
        ),
    )


async def _send_alert_details(
    message: Message,
    record: AlertRecord,
    *,
    prefix: str | None = None,
) -> None:
    text = _alert_detail_text(record)
    if prefix:
        text = f"{prefix}\n\n{text}"
    await message.answer(
        text,
        reply_markup=_alert_actions_keyboard(include_back=True),
    )


def _navigation_keyboard(
    *,
    confirm_button: str | None = None,
    include_back: bool = True,
) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = []
    if confirm_button:
        keyboard.append([KeyboardButton(text=confirm_button)])
    nav_row: list[KeyboardButton] = [KeyboardButton(text=MAIN_MENU_BUTTON)]
    if include_back:
        nav_row.insert(0, KeyboardButton(text=BACK_BUTTON))
    keyboard.append(nav_row)
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


def _alert_actions_keyboard(*, include_back: bool = True) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = [
        [KeyboardButton(text=TOGGLE_ALERT_BUTTON)],
        [
            KeyboardButton(text=EDIT_LIMIT_BUTTON),
            KeyboardButton(text=EDIT_WINDOW_BUTTON),
        ],
        [KeyboardButton(text=DELETE_ALERT_BUTTON)],
    ]

    nav_row: list[KeyboardButton] = [KeyboardButton(text=MAIN_MENU_BUTTON)]
    if include_back:
        nav_row.insert(0, KeyboardButton(text=BACK_BUTTON))
    keyboard.append(nav_row)

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )

def _alert_list_keyboard(
    labels: List[str],
    *,
    page: int,
    total_pages: int,
    include_back: bool = True,
) -> list[list[KeyboardButton]]:
    keyboard = [[KeyboardButton(text=label)] for label in labels]

    nav_buttons: list[KeyboardButton] = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(KeyboardButton(text=PREV_PAGE_BUTTON))
        if page < total_pages - 1:
            nav_buttons.append(KeyboardButton(text=NEXT_PAGE_BUTTON))
    if nav_buttons:
        keyboard.append(nav_buttons)

    nav_row: list[KeyboardButton] = [KeyboardButton(text=MAIN_MENU_BUTTON)]
    if include_back:
        nav_row.insert(0, KeyboardButton(text=BACK_BUTTON))
    keyboard.append(nav_row)
    return keyboard


def _parse_int(raw: str) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        return None
    if value < 1:
        return None
    return value


def _parse_window(raw: str) -> int | None:
    token = raw.lower().strip()
    multiplier = 1
    if token.endswith("m"):
        multiplier = 60
        token = token[:-1]
    elif token.endswith("s"):
        token = token[:-1]

    try:
        value = int(token)
    except ValueError:
        return None

    if value < 1:
        return None
    return value * multiplier


def _row_to_record(row: tuple) -> AlertRecord:
    return AlertRecord(
        id=row[0],
        name=row[1],
        pattern=row[2],
        enabled=row[3],
        threshold_count=row[4],
        threshold_window_seconds=row[5],
    )


def _alert_label(record: AlertRecord) -> str:
    name = record.name
    if len(name) > 28:
        name = name[:25] + "…"
    return f"#{record.id} {name}"


def _alert_overview_line(record: AlertRecord) -> str:
    status = "включён" if record.is_enabled else "выключен"
    return (
        f"#{record.id} {record.name} · {status} · "
        f"{record.threshold_count}/{record.threshold_window_seconds}s"
    )


def _alert_detail_text(record: AlertRecord) -> str:
    status = "✅ включён" if record.is_enabled else "🚫 выключен"
    return (
        f"{_alert_label(record)}\n"
        f"{status}\n"
        f"Лимит: {record.threshold_count} событий за "
        f"{record.threshold_window_seconds} сек.\n"
        f"Шаблон: {record.pattern}"
    )


def _update_alert_cache(user_id: int, record: AlertRecord) -> None:
    mapping = _alert_list_cache.get(user_id)
    if not mapping:
        return
    for label, cached in list(mapping.items()):
        if cached.id == record.id:
            mapping[label] = record


async def _load_alert(alert_id: int) -> AlertRecord | None:
    row = await get_alert(settings.DB_PATH, alert_id)
    if row is None:
        return None
    return _row_to_record(row)
