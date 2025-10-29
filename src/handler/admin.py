import re
from dataclasses import dataclass
from typing import Dict

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

CANCEL_BUTTON = "❌ Отмена"
SKIP_BUTTON = "⏭ Пропустить"
CANCEL_WORD = "отмена"
SKIP_WORD = "пропустить"

TOGGLE_ALERT_BUTTON = "🔁 Переключить активность"
EDIT_LIMIT_BUTTON = "✏️ Лимит событий"
EDIT_WINDOW_BUTTON = "⏱ Окно наблюдения"
BACK_TO_ALERTS_BUTTON = "⬅ Назад к списку"
BACK_TO_ALERT_MENU_BUTTON = "⬅ Назад к действиям"
DELETE_ALERT_BUTTON = "🗑 Удалить алерт"
CONFIRM_DELETE_BUTTON = "✅ Подтвердить удаление"
CANCEL_DELETE_BUTTON = "↩ Отмена удаления"


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


@dataclass
class EditAlertSession:
    alert: AlertRecord
    step: str = "menu"
    pending_delete: bool = False


_add_alert_sessions: Dict[int, AddAlertSession] = {}
_alert_list_cache: Dict[int, Dict[str, AlertRecord]] = {}
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

    _add_alert_sessions[user_id] = AddAlertSession(step="name")
    await _send_step_prompt(message, "Введите название алерта:")


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
    lowered = text.lower()

    if text == CANCEL_BUTTON or lowered.startswith(CANCEL_WORD):
        del _add_alert_sessions[user_id]
        await message.answer("Создание алерта отменено.", reply_markup=main_menu())
        return

    if session.step == "name":
        if not text:
            await _send_step_prompt(
                message,
                "Название не должно быть пустым. Введите название алерта.",
            )
            return
        session.name = text
        session.step = "pattern"
        await _send_step_prompt(
            message,
            "Введи регулярное выражение (используется модуль re).",
        )
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
        session.step = "threshold_count"
        await _send_step_prompt(
            message,
            "Сколько совпадений допускается? (по умолчанию 1)",
            include_skip=True,
        )
        return

    if session.step == "threshold_count":
        if not text or lowered.startswith(SKIP_WORD) or text == SKIP_BUTTON:
            session.threshold_count = 1
        else:
            value = _parse_int(text)
            if value is None:
                await _send_step_prompt(
                    message,
                    (
                        "Лимит событий должен быть положительным целым числом. "
                        "Введите значение или нажмите «⏭ Пропустить»."
                    ),
                    include_skip=True,
                )
                return
            session.threshold_count = value
        session.step = "window_seconds"
        await _send_step_prompt(
            message,
            "За какой интервал считаем события? (число секунд или 5m, по умолчанию 60)",
            include_skip=True,
        )
        return

    if session.step == "window_seconds":
        if not text or lowered.startswith(SKIP_WORD) or text == SKIP_BUTTON:
            session.window_seconds = 60
        else:
            window = _parse_window(text)
            if window is None:
                await _send_step_prompt(
                    message,
                    (
                        "Окно указывается целым числом секунд или с суффиксом s/m. "
                        "Введите значение или нажмите «⏭ Пропустить»."
                    ),
                    include_skip=True,
                )
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
    lowered = text.lower()

    if text == CANCEL_BUTTON or lowered.startswith(CANCEL_WORD):
        _alert_edit_sessions.pop(user_id, None)
        _alert_list_cache.pop(user_id, None)
        await message.answer("Действие отменено.", reply_markup=main_menu())
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
                back_button=BACK_TO_ALERT_MENU_BUTTON,
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
                back_button=BACK_TO_ALERT_MENU_BUTTON,
            )
            return

        if text == DELETE_ALERT_BUTTON:
            session.pending_delete = True
            session.step = "confirm_delete"
            await _send_step_prompt(
                message,
                f"Удалить алерт '{session.alert.name}'? Это действие необратимо.",
                confirm_button=CONFIRM_DELETE_BUTTON,
                back_button=CANCEL_DELETE_BUTTON,
            )
            return

        if text == BACK_TO_ALERTS_BUTTON:
            _alert_edit_sessions.pop(user_id, None)
            await _open_alerts_menu(message)
            return

        await message.answer(
            "Выберите действие из меню или нажмите «⬅ Назад к списку».",
            reply_markup=_alert_actions_keyboard(),
        )
        return

    if session.step == "edit_threshold":
        if text == BACK_TO_ALERT_MENU_BUTTON:
            session.step = "menu"
            await _send_alert_details(message, session.alert)
            return
        value = _parse_int(text)
        if value is None:
            await _send_step_prompt(
                message,
                "Введите положительное целое число или нажмите «⬅ Назад к действиям».",
                back_button=BACK_TO_ALERT_MENU_BUTTON,
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
                reply_markup=_alert_actions_keyboard(),
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
        if text == BACK_TO_ALERT_MENU_BUTTON:
            session.step = "menu"
            await _send_alert_details(message, session.alert)
            return
        window = _parse_window(text)
        if window is None:
            await _send_step_prompt(
                message,
                (
                    "Введите целое число секунд или значение с суффиксом s/m. "
                    "Например: 300 или 5m."
                ),
                back_button=BACK_TO_ALERT_MENU_BUTTON,
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
                reply_markup=_alert_actions_keyboard(),
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
        if text == CANCEL_DELETE_BUTTON or lowered.startswith("отмена"):
            session.step = "menu"
            session.pending_delete = False
            await _send_alert_details(message, session.alert)
            return

        if text == CONFIRM_DELETE_BUTTON:
            deleted = await remove_alert(settings.DB_PATH, session.alert.id)
            _alert_edit_sessions.pop(user_id, None)
            _alert_list_cache.pop(user_id, None)
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
            "Нажмите «✅ Подтвердить удаление» или «↩ Отмена удаления».",
            reply_markup=_step_keyboard(
                confirm_button=CONFIRM_DELETE_BUTTON,
                back_button=CANCEL_DELETE_BUTTON,
            ),
        )
        return


@router.message(lambda m: m.from_user and m.from_user.id in _alert_list_cache)
async def handle_alert_list_navigation(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    mapping = _alert_list_cache[user_id]
    text = (message.text or "").strip()
    lowered = text.lower()

    if text == CANCEL_BUTTON or lowered.startswith(CANCEL_WORD):
        _alert_list_cache.pop(user_id, None)
        await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == BACK_TO_ALERTS_BUTTON:
        await _open_alerts_menu(message)
        return

    record = mapping.get(text)
    if record is None:
        await message.answer(
            "Выберите алерт из клавиатуры или нажмите «❌ Отмена».",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=_alert_list_keyboard(mapping),
                resize_keyboard=True,
                input_field_placeholder="Выберите алерт…",
            ),
        )
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
    mapping = {_alert_label(record): record for record in records}
    _alert_list_cache[user_id] = mapping

    text_lines = ["📋 Алерты:"]
    for record in records:
        text_lines.append(_alert_overview_line(record))
    text_lines.append("")
    text_lines.append("Выберите алерт из клавиатуры или нажмите «❌ Отмена».")

    keyboard = ReplyKeyboardMarkup(
        keyboard=_alert_list_keyboard(mapping),
        resize_keyboard=True,
        input_field_placeholder="Выберите алерт…",
    )

    await message.answer("\n".join(text_lines), reply_markup=keyboard)


async def _send_step_prompt(
    message: Message,
    text: str,
    include_skip: bool = False,
    back_button: str | None = None,
    confirm_button: str | None = None,
) -> None:
    await message.answer(
        text,
        reply_markup=_step_keyboard(
            include_skip=include_skip,
            back_button=back_button,
            confirm_button=confirm_button,
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
        reply_markup=_alert_actions_keyboard(),
    )


def _step_keyboard(
    include_skip: bool = False,
    back_button: str | None = None,
    confirm_button: str | None = None,
) -> ReplyKeyboardMarkup:
    row = [KeyboardButton(text=CANCEL_BUTTON)]
    if include_skip:
        row.append(KeyboardButton(text=SKIP_BUTTON))
    keyboard = [row]
    if confirm_button:
        keyboard.append([KeyboardButton(text=confirm_button)])
    if back_button:
        keyboard.append([KeyboardButton(text=back_button)])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Введите значение или отмените",
    )


def _alert_actions_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=TOGGLE_ALERT_BUTTON)],
            [
                KeyboardButton(text=EDIT_LIMIT_BUTTON),
                KeyboardButton(text=EDIT_WINDOW_BUTTON),
            ],
            [KeyboardButton(text=DELETE_ALERT_BUTTON)],
            [KeyboardButton(text=BACK_TO_ALERTS_BUTTON)],
            [KeyboardButton(text=CANCEL_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


def _alert_list_keyboard(mapping: Dict[str, AlertRecord]) -> list[list[KeyboardButton]]:
    keyboard = [[KeyboardButton(text=label)] for label in mapping.keys()]
    keyboard.append([KeyboardButton(text=CANCEL_BUTTON)])
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
