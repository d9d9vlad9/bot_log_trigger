import math
import re
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Dict, List

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

from .main_menu import (
    ADD_ALERT_BUTTON,
    AGENT_SETTINGS_BUTTON,
    LIST_ALERTS_BUTTON,
    SCENARIO_ADD_BUTTON,
    SCENARIO_LIST_BUTTON,
    STATUS_BUTTON,
    main_menu,
)
from ..config import settings
from src.db.db_alerts import (
    add_alert,
    get_alert,
    list_alerts,
    remove_alert,
    toggle_alert_enabled,
    toggle_alert_scenario_flag,
    update_alert_thresholds,
)
from src.service import (
    RuntimeStatus,
    ScenarioCatalogService,
    ScenarioConflictError,
    ScenarioNotFoundError,
    ScenarioRuntimeError,
    ScenarioRuntimeService,
)

router = Router()

BACK_BUTTON = "⬅ Назад"
MAIN_MENU_BUTTON = "🏠 Главное меню"
PREV_PAGE_BUTTON = "◀️ Предыдущие"
NEXT_PAGE_BUTTON = "Следующие ▶️"
LIST_PAGE_SIZE = 6

TOGGLE_ALERT_BUTTON = "🔁 Переключить активность"
TOGGLE_SCENARIO_BUTTON = "🎯 Переключить сценарный режим"
EDIT_LIMIT_BUTTON = "✏️ Порог срабатываний"
EDIT_WINDOW_BUTTON = "⏱ Интервал наблюдения"
DELETE_ALERT_BUTTON = "🗑 Удалить алерт"
CONFIRM_DELETE_BUTTON = "✅ Подтвердить удаление"

PROGRESS_REFRESH_BUTTON = "🔄 Обновить список"
PROGRESS_ADVANCE_BUTTON = "⏭ Переключить этап"
PROGRESS_HISTORY_BUTTON = "📜 История"
PROGRESS_TIMER_RUNTIME_BUTTON = "⏱ Таймер инстанса"
PROGRESS_TIMER_SCENARIO_BUTTON = "⏱ Таймер сценария"
PROGRESS_STOP_BUTTON = "⛔ Стоп выполнение"
PROGRESS_SHOW_INACTIVE_BUTTON = "📃 Показать неактивных"
PROGRESS_ASSIGN_BUTTON = "🧩 Назначить сценарий"

SCENARIO_RENAME_BUTTON = "✏️ Переименовать"
SCENARIO_TIMEOUT_BUTTON = "⏱ Изменить время"
SCENARIO_TOGGLE_BUTTON = "🔁 Включить/выключить"
SCENARIO_DELETE_BUTTON = "🗑 Удалить"

ASSIGN_CUSTOM_BUTTON = "📝 Кастомный таймер"


@dataclass
class AlertRecord:
    id: int
    name: str
    pattern: str
    enabled: int
    threshold_count: int
    threshold_window_seconds: int
    is_scenario_trigger: int

    @property
    def is_enabled(self) -> bool:
        return bool(self.enabled)

    @property
    def is_scenario(self) -> bool:
        return bool(self.is_scenario_trigger)


@dataclass
class AddAlertSession:
    step: str
    name: str | None = None
    pattern: str | None = None
    threshold_count: int = 1
    window_seconds: int = 60
    is_scenario_trigger: bool = False
    history: list[str] = field(default_factory=list)


@dataclass
class EditAlertSession:
    alert: AlertRecord
    step: str = "menu"
    pending_delete: bool = False


@dataclass
class TimerEditSession:
    vm_id: str
    scope: str  # "runtime" | "scenario"
    scenario_id: int | None = None


@dataclass
class ProgressionSession:
    step: str = "list"
    mapping: Dict[str, RuntimeStatus] = field(default_factory=dict)
    label_for_vm: Dict[str, str] = field(default_factory=dict)
    vm_id: str | None = None
    assign_map: Dict[str, int] = field(default_factory=dict)
    assign_selected_id: int | None = None
    assign_default_label: str | None = None


@dataclass
class ScenarioCreateSession:
    step: str = "name"
    name: str | None = None
    from_alert: str | None = None
    to_alert: str | None = None
    timeout_minutes: int | None = None
    history: list[str] = field(default_factory=list)


@dataclass
class ScenarioListSession:
    step: str = "list"
    label_to_id: Dict[str, int] = field(default_factory=dict)
    selected_id: int | None = None


_add_alert_sessions: Dict[int, AddAlertSession] = {}
_alert_list_cache: Dict[int, Dict[str, AlertRecord]] = {}
_alert_list_order: Dict[int, List[str]] = {}
_alert_list_pages: Dict[int, int] = {}
_alert_edit_sessions: Dict[int, EditAlertSession] = {}
_timer_sessions: Dict[int, TimerEditSession] = {}
_progress_sessions: Dict[int, ProgressionSession] = {}
_scenario_create_sessions: Dict[int, ScenarioCreateSession] = {}
_scenario_list_sessions: Dict[int, ScenarioListSession] = {}


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


@router.message(F.text == STATUS_BUTTON)
async def menu_status(message: Message):
    if not await require_admin(message):
        return
    user_id = message.from_user.id
    _progress_sessions[user_id] = ProgressionSession(step="list")
    await _open_progressions_menu(message, user_id)


@router.message(F.text == SCENARIO_ADD_BUTTON)
async def menu_add_scenario(message: Message):
    if not await require_admin(message):
        return
    user_id = message.from_user.id
    session = ScenarioCreateSession()
    _scenario_create_sessions[user_id] = session
    await message.answer(
        "Создание сценария. Чтобы отменить, нажмите «⬅ Назад» или «🏠 Главное меню»."
    )
    await _prompt_scenario_create(message, session)


@router.message(F.text == SCENARIO_LIST_BUTTON)
async def menu_scenario_list(message: Message):
    if not await require_admin(message):
        return
    user_id = message.from_user.id
    _scenario_list_sessions[user_id] = ScenarioListSession()
    await _open_scenario_list(message, user_id)


@router.message(F.text == AGENT_SETTINGS_BUTTON)
async def menu_agent_settings(message: Message):
    if not await require_admin(message):
        return
    await message.answer(
        (
            "Параметры агента находятся в файле agent/config.json. "
            "Убедись, что AUTH_TOKEN совпадает с ALERT_TOKEN на сервере."
        ),
        reply_markup=main_menu(),
    )


@router.message(lambda m: m.from_user and m.from_user.id in _timer_sessions)
async def handle_timer_edit(message: Message):
    if not await require_admin(message):
        return
    user_id = message.from_user.id
    session = _timer_sessions[user_id]
    text = (message.text or "").strip()
    value = _parse_int(text)
    if value is None:
        await message.answer("Введите положительное целое число минут.")
        return

    service = ScenarioRuntimeService(settings.DB_PATH)
    try:
        if session.scope == "runtime":
            runtime = await service.set_runtime_timeout(
                session.vm_id,
                minutes=value,
            )
            await message.answer(
                f"⏱ Таймер инстанса обновлён. Новый дедлайн: {_format_dt(runtime.deadline_at_utc)}",
            )
        else:
            if session.scenario_id is None:
                raise ScenarioRuntimeError("Нет сценария для обновления.")
            scenario = await service.set_scenario_timeout(
                session.scenario_id,
                minutes=value,
            )
            await message.answer(
                (
                    f"⏱ Таймаут сценария '{scenario.name}' обновлён: "
                    f"{scenario.timeout_minutes} мин."
                )
            )
        if user_id in _progress_sessions:
            await _refresh_progress_session(user_id)
            progress_session = _progress_sessions[user_id]
            status = _status_from_session(progress_session, session.vm_id)
            if status:
                await _send_runtime_detail(message, user_id, status)
    except ScenarioRuntimeError as exc:
        await message.answer(f"Не удалось обновить таймер: {exc}")
    finally:
        _timer_sessions.pop(user_id, None)


@router.message(lambda m: m.from_user and m.from_user.id in _scenario_create_sessions)
async def handle_scenario_create(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    session = _scenario_create_sessions[user_id]
    text = (message.text or "").strip()

    if text == MAIN_MENU_BUTTON:
        _scenario_create_sessions.pop(user_id, None)
        await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == BACK_BUTTON:
        if session.history:
            session.step = session.history.pop()
            await _prompt_scenario_create(message, session)
        else:
            _scenario_create_sessions.pop(user_id, None)
            await _open_progressions_menu(message, user_id)
        return

    if session.step == "name":
        if not text:
            await message.answer("Название не должно быть пустым.")
            await _prompt_scenario_create(message, session)
            return
        session.name = text
        session.history.append("name")
        session.step = "from_alert"
        await _prompt_scenario_create(message, session)
        return

    if session.step == "from_alert":
        if not text:
            await message.answer("from_alert не должен быть пустым.")
            await _prompt_scenario_create(message, session)
            return
        session.from_alert = text
        session.history.append("from_alert")
        session.step = "to_alert"
        await _prompt_scenario_create(message, session)
        return

    if session.step == "to_alert":
        if not text:
            await message.answer("to_alert не должен быть пустым.")
            await _prompt_scenario_create(message, session)
            return
        session.to_alert = text
        session.history.append("to_alert")
        session.step = "timeout"
        await _prompt_scenario_create(message, session)
        return

    if session.step == "timeout":
        value = _parse_int(text)
        if value is None:
            await message.answer("Введите положительное целое число минут.")
            await _prompt_scenario_create(message, session)
            return
        session.timeout_minutes = value

        catalog = ScenarioCatalogService(settings.DB_PATH)
        try:
            scenario = await catalog.create_scenario(
                name=session.name or "",
                from_alert=session.from_alert or "",
                to_alert=session.to_alert or "",
                timeout_minutes=session.timeout_minutes,
            )
        except ScenarioConflictError as exc:
            await message.answer(f"Не удалось создать сценарий: {exc}")
            session.step = "from_alert"
            if session.history:
                session.history.pop()  # remove timeout step
            await _prompt_scenario_create(message, session)
            return

        _scenario_create_sessions.pop(user_id, None)
        await message.answer(
            (
                "✅ Сценарий создан:\n"
                f"{scenario.name} ({scenario.from_alert} → {scenario.to_alert})\n"
                f"Таймаут: {scenario.timeout_minutes} мин."
            )
        )
        await _refresh_progress_session(user_id)
        if user_id in _progress_sessions:
            await _open_progressions_menu(message, user_id)
        if user_id in _scenario_list_sessions:
            await _open_scenario_list(message, user_id)
        else:
            _scenario_list_sessions[user_id] = ScenarioListSession()
            await _open_scenario_list(message, user_id)


@router.message(lambda m: m.from_user and m.from_user.id in _progress_sessions)
async def handle_progress_session(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    session = _progress_sessions[user_id]
    text = (message.text or "").strip()

    if text == MAIN_MENU_BUTTON:
        _progress_sessions.pop(user_id, None)
        await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == BACK_BUTTON:
        if session.step == "detail":
            session.step = "list"
            session.vm_id = None
            await _open_progressions_menu(message, user_id)
        elif session.step == "inactive":
            session.step = "list"
            session.vm_id = None
            await _open_progressions_menu(message, user_id)
        elif session.step == "assign":
            session.step = "detail"
            await _refresh_progress_session(user_id)
            current = _status_from_session(_progress_sessions[user_id], session.vm_id)
            if current:
                await _send_runtime_detail(message, user_id, current)
            else:
                await _open_progressions_menu(message, user_id)
        elif session.step == "assign_confirm":
            session.step = "assign"
            await _start_assign_flow(message, user_id, session)
        elif session.step == "assign_custom":
            session.step = "assign_confirm"
            await _send_assign_confirm_from_session(message, user_id, session)
        else:
            _progress_sessions.pop(user_id, None)
            await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    service = ScenarioRuntimeService(settings.DB_PATH)

    if session.step == "list":
        if text == PROGRESS_REFRESH_BUTTON:
            await _open_progressions_menu(message, user_id)
            return

        if text == PROGRESS_SHOW_INACTIVE_BUTTON:
            session.step = "inactive"
            await _send_inactive_list(message, user_id)
            return

        status = session.mapping.get(text)
        if status is None:
            await message.answer(
                "Не удалось распознать выбор. Используйте кнопки меню."
            )
            await _open_progressions_menu(message, user_id)
            return

        session.step = "detail"
        session.vm_id = status.runtime.vm_id
        await _send_runtime_detail(message, user_id, status)
        return

    if session.step == "inactive":
        if text == PROGRESS_REFRESH_BUTTON:
            await _send_inactive_list(message, user_id)
            return
        await message.answer("Используйте кнопки для навигации (⬅ Назад, 🏠 Главное меню).")
        return

    if session.step == "assign":
        if text == PROGRESS_REFRESH_BUTTON:
            await _start_assign_flow(message, user_id, session)
            return
        scenario_id = session.assign_map.get(text)
        if scenario_id is None:
            await message.answer("Выберите сценарий из списка или нажмите «⬅ Назад».")
            return
        await _send_assign_confirm(message, user_id, session, scenario_id)
        return

    if session.step == "assign_confirm":
        if text == PROGRESS_REFRESH_BUTTON:
            await _send_assign_confirm_from_session(message, user_id, session)
            return
        scenario_id = session.assign_selected_id
        if scenario_id is None:
            await _start_assign_flow(message, user_id, session)
            return
        catalog = ScenarioCatalogService(settings.DB_PATH)
        try:
            scenario = await catalog.get_scenario(scenario_id)
        except ScenarioNotFoundError:
            await message.answer("Сценарий не найден. Обновляю список.")
            await _start_assign_flow(message, user_id, session)
            return
        if text == session.assign_default_label:
            await _apply_scenario_assignment(
                message,
                user_id,
                session,
                scenario,
                service,
                minutes_override=None,
            )
            return
        if text == ASSIGN_CUSTOM_BUTTON:
            session.step = "assign_custom"
            await message.answer(
                "Введите таймер (в минутах).",
                reply_markup=_navigation_keyboard(include_back=True),
            )
            return
        minutes_value = _parse_int(text)
        if minutes_value is not None:
            await _apply_scenario_assignment(
                message,
                user_id,
                session,
                scenario,
                service,
                minutes_override=minutes_value,
            )
            return
        await message.answer(
            "Используйте кнопки или введите положительное целое число минут."
        )
        return

    if session.step == "assign_custom":
        scenario_id = session.assign_selected_id
        if scenario_id is None:
            await _start_assign_flow(message, user_id, session)
            return
        minutes_value = _parse_int(text)
        if minutes_value is None:
            await message.answer("Введите положительное целое число минут.")
            return
        catalog = ScenarioCatalogService(settings.DB_PATH)
        try:
            scenario = await catalog.get_scenario(scenario_id)
        except ScenarioNotFoundError:
            await message.answer("Сценарий не найден. Обновляю список.")
            await _start_assign_flow(message, user_id, session)
            return
        await _apply_scenario_assignment(
            message,
            user_id,
            session,
            scenario,
            service,
            minutes_override=minutes_value,
        )
        return

    if session.step == "detail":
        if text == PROGRESS_REFRESH_BUTTON:
            await _refresh_progress_session(user_id)
            session = _progress_sessions[user_id]
            status = _status_from_session(session, session.vm_id)
            if status:
                await _send_runtime_detail(message, user_id, status)
            else:
                await _open_progressions_menu(message, user_id)
            return

        if text == PROGRESS_ADVANCE_BUTTON:
            if not session.vm_id:
                await message.answer("Выполнение не выбрано.")
                return
            try:
                await service.manual_advance(session.vm_id)
                await message.answer("Этап переключён вручную.")
            except ScenarioRuntimeError as exc:
                await message.answer(f"Не удалось переключить этап: {exc}")
            await _refresh_progress_session(user_id)
            session = _progress_sessions[user_id]
            status = _status_from_session(session, session.vm_id)
            if status:
                await _send_runtime_detail(message, user_id, status)
            else:
                await _open_progressions_menu(message, user_id)
            return

        if text == PROGRESS_HISTORY_BUTTON:
            if not session.vm_id:
                await message.answer("Выполнение не выбрано.")
                return
            events = await service.list_history(vm_id=session.vm_id, limit=10)
            await message.answer(_format_history(events))
            return

        if text == PROGRESS_ASSIGN_BUTTON:
            await _start_assign_flow(message, user_id, session)
            return

        if text == PROGRESS_TIMER_RUNTIME_BUTTON:
            await _refresh_progress_session(user_id)
            session = _progress_sessions[user_id]
            status = _status_from_session(session, session.vm_id)
            if status is None:
                await message.answer("Инстанс не найден.")
                await _open_progressions_menu(message, user_id)
                return
            _timer_sessions[user_id] = TimerEditSession(
                vm_id=status.runtime.vm_id,
                scope="runtime",
                scenario_id=status.scenario.id if status.scenario else None,
            )
            await message.answer(
                "Введите новый таймер (в минутах) для текущего выполнения."
            )
            return

        if text == PROGRESS_TIMER_SCENARIO_BUTTON:
            await _refresh_progress_session(user_id)
            session = _progress_sessions[user_id]
            status = _status_from_session(session, session.vm_id)
            if status is None or status.scenario is None:
                await message.answer("Сценарий недоступен для изменения таймера.")
                return
            _timer_sessions[user_id] = TimerEditSession(
                vm_id=status.runtime.vm_id,
                scope="scenario",
                scenario_id=status.scenario.id,
            )
            await message.answer(
                "Введите новый таймер (в минутах) для сценария."
            )
            return

        if text == PROGRESS_STOP_BUTTON:
            if not session.vm_id:
                await message.answer("Выполнение не выбрано.")
                return
            try:
                await service.stop_runtime(session.vm_id)
                await message.answer("Выполнение остановлено.")
            except ScenarioRuntimeError as exc:
                await message.answer(f"Не удалось остановить: {exc}")
            await _refresh_progress_session(user_id)
            session = _progress_sessions[user_id]
            status = _status_from_session(session, session.vm_id)
            if status:
                await _send_runtime_detail(message, user_id, status)
            else:
                await _open_progressions_menu(message, user_id)
            return

        await message.answer("Используйте доступные кнопки для управления.")
        return


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
        session.step = "scenario_flag"
        await _prompt_add_step(message, session)
        return

    if session.step == "scenario_flag":
        normalized = text.strip().lower()
        truthy = {"да", "yes", "y", "true", "1", "д", "+"}
        falsy = {"нет", "no", "n", "false", "0", "н", "-"}
        if normalized in truthy:
            session.is_scenario_trigger = True
        elif normalized in falsy:
            session.is_scenario_trigger = False
        else:
            await message.answer(
                "Ответьте «да» или «нет» (можно использовать y/n, 1/0)."
            )
            await _prompt_add_step(message, session)
            return
        session.history.append("scenario_flag")
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
            is_scenario_trigger=session.is_scenario_trigger,
        )
        del _add_alert_sessions[user_id]
        mode_text = (
            "сценарный триггер"
            if session.is_scenario_trigger
            else "Telegram-уведомление"
        )
        await message.answer(
            (
                f"✅ Алерт '{session.name}' добавлен с id={aid}. "
                f"Порог: {session.threshold_count} событий за "
                f"{session.window_seconds} сек. "
                f"Режим: {mode_text}."
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

        if text == TOGGLE_SCENARIO_BUTTON:
            toggled = await toggle_alert_scenario_flag(
                settings.DB_PATH,
                session.alert.id,
            )
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
            mode_text = (
                "сценарный триггер" if record.is_scenario else "уведомление"
            )
            await _send_alert_details(
                message,
                record,
                prefix=f"Режим обновлён: {mode_text}.",
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
        "scenario_flag": (
            "Этот алерт используется только как триггер сценария? "
            "Ответьте «да» или «нет»."
        ),
        "threshold_count": "Введите лимит событий (целое число, например 1).",
        "window_seconds": (
            "Введите окно (в секундах или формате 5m/30s, например 300)."
        ),
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
    elif session.step == "scenario_flag":
        mode = "да" if session.is_scenario_trigger else "нет"
        text += f"\nСейчас: {mode}"

    await _send_step_prompt(
        message,
        text,
        include_back=bool(session.history),
    )

async def _prompt_scenario_create(
    message: Message,
    session: ScenarioCreateSession,
) -> None:
    prompts: dict[str, str] = {
        "name": "Введите название сценария.",
        "from_alert": "Введите from_alert (имя алерта, который запускает сценарий).",
        "to_alert": "Введите to_alert (алерт, который ожидаем).",
        "timeout": "Введите таймаут в минутах.",
    }
    text = prompts.get(session.step, "Введите значение.")
    if session.step == "name" and session.name:
        text += f"\nТекущее значение: {session.name}"
    elif session.step == "from_alert" and session.from_alert:
        text += f"\nТекущее значение: {session.from_alert}"
    elif session.step == "to_alert" and session.to_alert:
        text += f"\nТекущее значение: {session.to_alert}"
    elif session.step == "timeout" and session.timeout_minutes is not None:
        text += f"\nТекущее значение: {session.timeout_minutes} мин."

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


async def _open_progressions_menu(message: Message, user_id: int) -> None:
    session = _progress_sessions.setdefault(user_id, ProgressionSession())
    statuses_all, statuses_active = await _refresh_progress_session(user_id)
    catalog = ScenarioCatalogService(settings.DB_PATH)
    scenarios = await catalog.list_scenarios()
    session.step = "list"
    session.vm_id = None

    lines: list[str] = []
    if statuses_active:
        lines.append("📊 Активные сценарии:")
        for status in statuses_active:
            lines.append(_runtime_overview_line(status))
    else:
        lines.append("📊 Активные сценарии: нет активных выполнений.")

    if scenarios:
        lines.append("")
        lines.append("📚 Каталог сценариев:")
        for scenario in scenarios:
            status_text = "включён" if scenario.enabled else "выключен"
            lines.append(
                f"#{scenario.id} {scenario.name} · {scenario.from_alert}→{scenario.to_alert} · "
                f"{scenario.timeout_minutes} мин · {status_text}"
            )
    else:
        lines.append("")
    lines.append("📚 Каталог сценариев пуст. Добавьте сценарий кнопкой ниже.")

    text = "\n".join(lines)

    active_labels = {
        session.label_for_vm.get(status.runtime.vm_id)
        for status in statuses_active
        if session.label_for_vm.get(status.runtime.vm_id)
    }
    labels = [label for label in session.mapping.keys() if label in active_labels]
    if not labels:
        labels = [label for label in session.mapping.keys()]

    await message.answer(
        text,
        reply_markup=_progress_list_keyboard(labels),
    )


async def _send_runtime_detail(
    message: Message,
    user_id: int,
    status: RuntimeStatus,
) -> None:
    session = _progress_sessions.setdefault(user_id, ProgressionSession())
    session.step = "detail"
    session.vm_id = status.runtime.vm_id
    await message.answer(
        _runtime_detail_text(status),
        reply_markup=_progress_detail_keyboard(status),
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
        [KeyboardButton(text=TOGGLE_SCENARIO_BUTTON)],
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


async def _refresh_progress_session(user_id: int) -> tuple[list[RuntimeStatus], list[RuntimeStatus]]:
    service = ScenarioRuntimeService(settings.DB_PATH)
    statuses_all = list(await service.list_all_status())
    mapping, label_map = _build_progress_mapping(statuses_all)
    session = _progress_sessions.setdefault(user_id, ProgressionSession())
    session.mapping = mapping
    session.label_for_vm = label_map
    if session.vm_id and session.vm_id not in label_map:
        session.vm_id = None
        session.step = "list"
    now = datetime.now(timezone.utc)
    threshold_minutes = settings.AGENT_ACTIVE_THRESHOLD_MINUTES
    cutoff = now - timedelta(minutes=threshold_minutes) if threshold_minutes > 0 else None

    statuses_active: list[RuntimeStatus] = []
    for status in statuses_all:
        agent = status.agent_status
        if status.runtime.status == "active":
            statuses_active.append(status)
            continue
        if agent and agent.last_seen_at_utc:
            if cutoff is None or agent.last_seen_at_utc >= cutoff:
                statuses_active.append(status)
                continue
    return statuses_all, statuses_active


def _build_progress_mapping(
    statuses: list[RuntimeStatus],
) -> tuple[Dict[str, RuntimeStatus], Dict[str, str]]:
    mapping: Dict[str, RuntimeStatus] = {}
    label_map: Dict[str, str] = {}
    for status in statuses:
        vm_id = status.runtime.vm_id
        vm_name = status.vm_name
        scenario = status.scenario
        vm_label = vm_name or vm_id
        if scenario:
            label = (
                f"{vm_label} · {scenario.from_alert}→{scenario.to_alert} | "
                f"{status.runtime.status}"
            )
        else:
            label = f"{vm_label} · статус={status.runtime.status}"
        mapping[label] = status
        label_map[vm_id] = label
    return mapping, label_map


def _progress_list_keyboard(labels: list[str]) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = [[KeyboardButton(text=label)] for label in labels]
    keyboard.append([KeyboardButton(text=PROGRESS_SHOW_INACTIVE_BUTTON)])
    keyboard.append([KeyboardButton(text=PROGRESS_REFRESH_BUTTON)])
    keyboard.append(
        [
            KeyboardButton(text=BACK_BUTTON),
            KeyboardButton(text=MAIN_MENU_BUTTON),
        ]
    )
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите сценарий…",
    )


def _progress_detail_keyboard(status: RuntimeStatus) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = []
    if status.runtime.status == "active" and status.scenario is not None:
        keyboard.append([KeyboardButton(text=PROGRESS_ADVANCE_BUTTON)])
        keyboard.append(
            [
                KeyboardButton(text=PROGRESS_TIMER_RUNTIME_BUTTON),
                KeyboardButton(text=PROGRESS_TIMER_SCENARIO_BUTTON),
            ]
        )
    keyboard.append([KeyboardButton(text=PROGRESS_HISTORY_BUTTON)])
    keyboard.append([KeyboardButton(text=PROGRESS_ASSIGN_BUTTON)])
    if status.runtime.status == "active":
        keyboard.append([KeyboardButton(text=PROGRESS_STOP_BUTTON)])
    keyboard.append([KeyboardButton(text=PROGRESS_REFRESH_BUTTON)])
    keyboard.append(
        [
            KeyboardButton(text=BACK_BUTTON),
            KeyboardButton(text=MAIN_MENU_BUTTON),
        ]
    )
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


def _inactive_list_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PROGRESS_REFRESH_BUTTON)],
            [KeyboardButton(text=BACK_BUTTON)],
            [KeyboardButton(text=MAIN_MENU_BUTTON)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Неактивные VM…",
    )


async def _send_inactive_list(message: Message, user_id: int) -> None:
    statuses_all, statuses_active = await _refresh_progress_session(user_id)
    inactive = [
        status for status in statuses_all if status not in statuses_active
    ]
    if not inactive:
        text = "🛌 Все агенты на связи."
    else:
        lines = ["🛌 Неактивные VM:"]
        for status in inactive:
            lines.append(_runtime_overview_line(status))
        text = "\n".join(lines)
    await message.answer(text, reply_markup=_inactive_list_keyboard())


async def _load_enabled_scenarios() -> list:
    catalog = ScenarioCatalogService(settings.DB_PATH)
    scenarios = await catalog.list_scenarios()
    return [scenario for scenario in scenarios if scenario.enabled]


async def _start_assign_flow(
    message: Message,
    user_id: int,
    session: ProgressionSession,
) -> None:
    scenarios = await _load_enabled_scenarios()
    if not scenarios:
        await message.answer(
            "Нет включённых сценариев. Используйте список сценариев, чтобы добавить или включить."
        )
        session.step = "detail"
        await _refresh_progress_session(user_id)
        current = _status_from_session(_progress_sessions[user_id], session.vm_id)
        if current:
            await _send_runtime_detail(message, user_id, current)
        return
    session.assign_map = {f"#{sc.id} {sc.name}": sc.id for sc in scenarios}
    session.assign_selected_id = None
    session.assign_default_label = None
    session.step = "assign"
    await message.answer(
        "Выберите сценарий для назначения:",
        reply_markup=_assign_list_keyboard(list(session.assign_map.keys())),
    )


async def _send_assign_confirm(
    message: Message,
    user_id: int,
    session: ProgressionSession,
    scenario_id: int,
) -> None:
    catalog = ScenarioCatalogService(settings.DB_PATH)
    try:
        scenario = await catalog.get_scenario(scenario_id)
    except ScenarioNotFoundError:
        await message.answer("Сценарий не найден. Обновляю список.")
        await _start_assign_flow(message, user_id, session)
        return
    if not scenario.enabled:
        await message.answer("Сценарий отключён. Включите его перед назначением.")
        return
    session.assign_selected_id = scenario_id
    session.assign_default_label = f"Дефолтный = {scenario.timeout_minutes} мин"
    session.step = "assign_confirm"
    await message.answer(
        (
            f"Сценарий: {scenario.name} ({scenario.from_alert} → {scenario.to_alert})\n"
            f"Таймаут по умолчанию: {scenario.timeout_minutes} мин.\n"
            "Выберите вариант назначения или введите количество минут."
        ),
        reply_markup=_assign_confirm_keyboard(session.assign_default_label),
    )


async def _send_assign_confirm_from_session(
    message: Message,
    user_id: int,
    session: ProgressionSession,
) -> None:
    scenario_id = session.assign_selected_id
    if scenario_id is None:
        await _start_assign_flow(message, user_id, session)
        return
    await _send_assign_confirm(message, user_id, session, scenario_id)


def _assign_list_keyboard(labels: list[str]) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = [[KeyboardButton(text=label)] for label in labels]
    keyboard.append([KeyboardButton(text=PROGRESS_REFRESH_BUTTON)])
    keyboard.append([KeyboardButton(text=BACK_BUTTON)])
    keyboard.append([KeyboardButton(text=MAIN_MENU_BUTTON)])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите сценарий…",
    )


def _assign_confirm_keyboard(default_label: str) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = [
        [KeyboardButton(text=default_label)],
        [KeyboardButton(text=ASSIGN_CUSTOM_BUTTON)],
        [KeyboardButton(text=PROGRESS_REFRESH_BUTTON)],
        [KeyboardButton(text=BACK_BUTTON)],
        [KeyboardButton(text=MAIN_MENU_BUTTON)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


async def _apply_scenario_assignment(
    message: Message,
    user_id: int,
    session: ProgressionSession,
    scenario,
    service: ScenarioRuntimeService,
    *,
    minutes_override: int | None,
) -> None:
    if not session.vm_id:
        await message.answer("VM не выбрана." )
        session.step = "detail"
        return
    try:
        assigned_status = await service.assign_scenario(
            session.vm_id,
            scenario.id,
            minutes_override=minutes_override,
        )
    except ScenarioRuntimeError as exc:
        await message.answer(f"Не удалось назначить сценарий: {exc}")
        return
    session.step = "detail"
    session.assign_selected_id = None
    session.assign_default_label = None
    await _refresh_progress_session(user_id)
    current = _status_from_session(_progress_sessions[user_id], session.vm_id)
    if current is None:
        current = assigned_status
    minutes_text = (
        f"по умолчанию ({scenario.timeout_minutes} мин)"
        if minutes_override is None
        else f"на {minutes_override} мин"
    )
    await message.answer(
        (
            f"Сценарий '{scenario.name}' назначен {minutes_text}.\n"
            f"Дедлайн: {_format_dt(current.runtime.deadline_at_utc)}"
        )
    )
    await _send_runtime_detail(message, user_id, current)


async def _reload_scenarios(user_id: int) -> list:
    catalog = ScenarioCatalogService(settings.DB_PATH)
    scenarios = list(await catalog.list_scenarios())
    session = _scenario_list_sessions.setdefault(user_id, ScenarioListSession())
    mapping: Dict[str, int] = {}
    for scenario in scenarios:
        label = f"#{scenario.id} {scenario.name}"
        mapping[label] = scenario.id
    session.label_to_id = mapping
    if session.selected_id and session.selected_id not in mapping.values():
        session.selected_id = None
    return scenarios


def _scenario_list_keyboard(labels: list[str]) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = [[KeyboardButton(text=label)] for label in labels]
    keyboard.append([KeyboardButton(text=SCENARIO_ADD_BUTTON)])
    keyboard.append([KeyboardButton(text=PROGRESS_REFRESH_BUTTON)])
    keyboard.append([KeyboardButton(text=BACK_BUTTON)])
    keyboard.append([KeyboardButton(text=MAIN_MENU_BUTTON)])
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите сценарий…",
    )


def _scenario_detail_text(scenario) -> str:
    status_text = "✅ включён" if scenario.enabled else "🚫 выключен"
    return (
        f"#{scenario.id} {scenario.name}\n"
        f"Переход: {scenario.from_alert} → {scenario.to_alert}\n"
        f"Таймаут: {scenario.timeout_minutes} мин.\n"
        f"Статус: {status_text}"
    )


def _scenario_detail_keyboard(_scenario) -> ReplyKeyboardMarkup:
    keyboard: list[list[KeyboardButton]] = [
        [KeyboardButton(text=SCENARIO_TIMEOUT_BUTTON), KeyboardButton(text=SCENARIO_RENAME_BUTTON)],
        [KeyboardButton(text=SCENARIO_TOGGLE_BUTTON)],
        [KeyboardButton(text=SCENARIO_DELETE_BUTTON)],
        [KeyboardButton(text=PROGRESS_REFRESH_BUTTON)],
        [KeyboardButton(text=BACK_BUTTON)],
        [KeyboardButton(text=MAIN_MENU_BUTTON)],
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие…",
    )


async def _open_scenario_list(message: Message, user_id: int) -> None:
    scenarios = await _reload_scenarios(user_id)
    session = _scenario_list_sessions.setdefault(user_id, ScenarioListSession())
    session.step = "list"
    session.selected_id = None

    if scenarios:
        lines = ["📚 Сценарии:"]
        for scenario in scenarios:
            status_text = "включён" if scenario.enabled else "выключен"
            lines.append(
                f"#{scenario.id} {scenario.name} · {scenario.from_alert}→{scenario.to_alert} · "
                f"{scenario.timeout_minutes} мин · {status_text}"
            )
        text = "\n".join(lines)
    else:
        text = "📚 Сценариев пока нет."

    labels = list(session.label_to_id.keys())
    await message.answer(text, reply_markup=_scenario_list_keyboard(labels))


async def _send_scenario_detail(message: Message, user_id: int, scenario_id: int) -> None:
    await _reload_scenarios(user_id)
    catalog = ScenarioCatalogService(settings.DB_PATH)
    try:
        scenario = await catalog.get_scenario(scenario_id)
    except ScenarioNotFoundError:
        await message.answer("Сценарий не найден. Обновляю список.")
        await _open_scenario_list(message, user_id)
        return

    session = _scenario_list_sessions.setdefault(user_id, ScenarioListSession())
    session.step = "detail"
    session.selected_id = scenario_id

    await message.answer(
        _scenario_detail_text(scenario),
        reply_markup=_scenario_detail_keyboard(scenario),
    )


@router.message(lambda m: m.from_user and m.from_user.id in _scenario_list_sessions)
async def handle_scenario_list_session(message: Message):
    if not await require_admin(message):
        return

    user_id = message.from_user.id
    session = _scenario_list_sessions[user_id]
    text = (message.text or "").strip()

    if text == MAIN_MENU_BUTTON:
        _scenario_list_sessions.pop(user_id, None)
        await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == BACK_BUTTON:
        if session.step in {"detail", "rename", "timeout"}:
            session.step = "list"
            session.selected_id = None
            await _open_scenario_list(message, user_id)
        else:
            _scenario_list_sessions.pop(user_id, None)
            await message.answer("Возвращаюсь в главное меню.", reply_markup=main_menu())
        return

    if text == PROGRESS_REFRESH_BUTTON:
        if session.step == "detail" and session.selected_id is not None:
            await _send_scenario_detail(message, user_id, session.selected_id)
        else:
            await _open_scenario_list(message, user_id)
        return

    catalog = ScenarioCatalogService(settings.DB_PATH)

    if session.step == "list":
        if text == SCENARIO_ADD_BUTTON:
            create_session = ScenarioCreateSession()
            _scenario_create_sessions[user_id] = create_session
            await message.answer(
                "Создание сценария. Чтобы отменить, нажмите «⬅ Назад» или «🏠 Главное меню»."
            )
            await _prompt_scenario_create(message, create_session)
            return
        scenario_id = session.label_to_id.get(text)
        if scenario_id is None:
            await message.answer("Не удалось распознать выбор. Используйте кнопки.")
            return
        session.selected_id = scenario_id
        session.step = "detail"
        await _send_scenario_detail(message, user_id, scenario_id)
        return

    if session.step == "detail":
        scenario_id = session.selected_id
        if scenario_id is None:
            await _open_scenario_list(message, user_id)
            return
        if text == SCENARIO_TIMEOUT_BUTTON:
            session.step = "timeout"
            await message.answer(
                "Введите новый таймаут (в минутах).",
                reply_markup=_navigation_keyboard(include_back=True),
            )
            return
        if text == SCENARIO_RENAME_BUTTON:
            session.step = "rename"
            await message.answer(
                "Введите новое название сценария.",
                reply_markup=_navigation_keyboard(include_back=True),
            )
            return
        if text == SCENARIO_TOGGLE_BUTTON:
            try:
                scenario = await catalog.get_scenario(scenario_id)
            except ScenarioNotFoundError:
                await message.answer("Сценарий не найден. Обновляю список.")
                await _open_scenario_list(message, user_id)
                return
            enabled = not scenario.enabled
            scenario = await catalog.set_enabled(scenario_id, enabled=enabled)
            await message.answer(
                "Сценарий {}.".format("включён" if scenario.enabled else "выключен")
            )
            await _send_scenario_detail(message, user_id, scenario_id)
            return
        if text == SCENARIO_DELETE_BUTTON:
            deleted = await catalog.delete_scenario(scenario_id)
            if deleted:
                await message.answer("Сценарий удалён.")
            else:
                await message.answer("Не удалось удалить сценарий.")
            session.step = "list"
            session.selected_id = None
            await _open_scenario_list(message, user_id)
            return
        await message.answer("Используйте кнопки действий для выбранного сценария.")
        return

    if session.step == "rename":
        scenario_id = session.selected_id
        if scenario_id is None:
            await _open_scenario_list(message, user_id)
            return
        if not text:
            await message.answer("Название не должно быть пустым.")
            return
        try:
            await catalog.update_scenario(scenario_id, name=text)
        except ScenarioConflictError as exc:
            await message.answer(f"Не удалось изменить название: {exc}")
            return
        except ScenarioNotFoundError:
            await message.answer("Сценарий не найден. Обновляю список.")
            session.step = "list"
            session.selected_id = None
            await _open_scenario_list(message, user_id)
            return
        session.step = "detail"
        await _send_scenario_detail(message, user_id, scenario_id)
        return

    if session.step == "timeout":
        scenario_id = session.selected_id
        if scenario_id is None:
            await _open_scenario_list(message, user_id)
            return
        value = _parse_int(text)
        if value is None:
            await message.answer("Введите положительное целое число минут.")
            return
        try:
            await catalog.update_scenario(scenario_id, timeout_minutes=value)
        except ScenarioNotFoundError:
            await message.answer("Сценарий не найден. Обновляю список.")
            session.step = "list"
            session.selected_id = None
            await _open_scenario_list(message, user_id)
            return
        session.step = "detail"
        await _send_scenario_detail(message, user_id, scenario_id)
        return


def _status_from_session(
    session: ProgressionSession,
    vm_id: str | None,
) -> RuntimeStatus | None:
    if vm_id is None:
        return None
    label = session.label_for_vm.get(vm_id)
    if label is None:
        return None
    return session.mapping.get(label)


def _runtime_overview_line(status: RuntimeStatus) -> str:
    runtime = status.runtime
    scenario = status.scenario
    vm_label = status.vm_name or runtime.vm_id
    scenario_text = (
        f"{scenario.from_alert}→{scenario.to_alert}" if scenario else "—"
    )
    expected = scenario.to_alert if scenario else "—"
    deadline = _format_dt(runtime.deadline_at_utc)
    seen = _format_dt(status.agent_status.last_seen_at_utc) if status.agent_status else "—"
    return (
        f"{vm_label} ({runtime.vm_id}): {scenario_text} · статус={runtime.status} · "
        f"ждём {expected} до {deadline} · агент: {seen}"
    )


def _runtime_detail_text(status: RuntimeStatus) -> str:
    runtime = status.runtime
    scenario = status.scenario
    vm_label = status.vm_name or runtime.vm_id
    lines = [
        f"VM: {vm_label} ({runtime.vm_id})",
        f"Статус: {runtime.status}",
    ]
    if scenario:
        lines.append(
            f"Текущий сценарий: {scenario.name} "
            f"({scenario.from_alert} → {scenario.to_alert})"
        )
        lines.append(f"Таймаут сценария: {scenario.timeout_minutes} мин.")
        if runtime.status == "active":
            lines.append(f"Ожидаем: {scenario.to_alert}")
    else:
        lines.append("Текущий сценарий: —")

    if status.next_scenario:
        ns = status.next_scenario
        lines.append(
            f"Следующий шаг: {ns.name} ({ns.from_alert} → {ns.to_alert})"
        )

    lines.append(f"Дедлайн: {_format_dt(runtime.deadline_at_utc)}")
    if runtime.last_received_alert:
        lines.append(f"Последний подтверждённый алерт: {runtime.last_received_alert}")
    lines.append(f"Создано: {_format_dt(runtime.created_at_utc)}")
    lines.append(f"Обновлено: {_format_dt(runtime.updated_at_utc)}")
    if status.agent_status:
        lines.append(f"Агент онлайн: {_format_dt(status.agent_status.last_seen_at_utc)}")
        if status.agent_status.vm_name and status.agent_status.vm_name != status.vm_name:
            lines.append(f"Имя агента: {status.agent_status.vm_name}")
    return "\n".join(lines)


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_history(events) -> str:
    if not events:
        return "📜 История пуста."
    lines = ["📜 История событий:"]
    for event in events:
        header = (
            f"{event.event_type.upper()} • "
            f"{event.from_alert} → {event.to_alert}"
        )
        lines.append(header)
        lines.append(f"  Дедлайн: {_format_dt(event.deadline_at_utc)}")
        if event.event_received_at_utc:
            lines.append(
                f"  Получено: {_format_dt(event.event_received_at_utc)}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()

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
        is_scenario_trigger=row[6],
    )


def _alert_label(record: AlertRecord) -> str:
    name = record.name
    if len(name) > 28:
        name = name[:25] + "…"
    return f"#{record.id} {name}"


def _alert_overview_line(record: AlertRecord) -> str:
    status = "включён" if record.is_enabled else "выключен"
    mode = "сценарий" if record.is_scenario else "уведомление"
    return (
        f"#{record.id} {record.name} · {status} · "
        f"{record.threshold_count}/{record.threshold_window_seconds}s · {mode}"
    )


def _alert_detail_text(record: AlertRecord) -> str:
    status = "✅ включён" if record.is_enabled else "🚫 выключен"
    mode = "🎯 сценарный триггер" if record.is_scenario else "📣 уведомление в Telegram"
    return (
        f"{_alert_label(record)}\n"
        f"{status}\n"
        f"{mode}\n"
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
