from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, Sequence

import aiosqlite

from src.db.db_progressions import (
    AgentStatus,
    Scenario,
    ScenarioRuntime,
    TransitionEvent,
    append_transition_event,
    create_scenario,
    delete_scenario,
    get_runtime,
    get_scenario,
    get_scenario_by_from_alert,
    init_progressions_db,
    list_agent_statuses,
    list_scenarios,
    list_transition_events,
    list_due_runtimes,
    list_all_runtimes,
    list_active_runtimes,
    set_scenario_enabled,
    upsert_agent_status,
    upsert_runtime,
    update_scenario,
)


class ScenarioConflictError(Exception):
    """Raised when scenario with the same from_alert already exists."""


class ScenarioInUseError(Exception):
    """Raised when scenario is referenced by active runtimes or history."""


class ScenarioNotFoundError(Exception):
    """Raised when scenario is not found."""


class ScenarioRuntimeError(Exception):
    """Raised when runtime-related operations cannot proceed."""


@dataclass(slots=True)
class AlertHandlingResult:
    vm_id: str
    outcome: Literal["ignored", "started", "progressed", "completed"]
    runtime: ScenarioRuntime | None
    scenario: Scenario | None
    next_scenario: Scenario | None
    message: str


@dataclass(slots=True)
class TimeoutResult:
    vm_id: str
    timed_out: bool
    runtime: ScenarioRuntime | None
    scenario: Scenario | None
    message: str


@dataclass(slots=True)
class RuntimeStatus:
    runtime: ScenarioRuntime
    scenario: Scenario | None
    next_scenario: Scenario | None
    vm_name: str | None
    agent_status: AgentStatus | None


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _add_timeout(now: dt.datetime, minutes: int) -> dt.datetime:
    return now + dt.timedelta(minutes=minutes)


class ScenarioCatalogService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def ensure_schema(self) -> None:
        await init_progressions_db(self.db_path)

    async def create_scenario(
        self,
        *,
        name: str,
        from_alert: str,
        to_alert: str,
        timeout_minutes: int,
        enabled: bool = True,
    ) -> Scenario:
        try:
            existing = await get_scenario_by_from_alert(self.db_path, from_alert)
            if existing is not None:
                raise ScenarioConflictError(
                    f"Scenario with from_alert='{from_alert}' already exists."
                )
            return await create_scenario(
                self.db_path,
                name=name,
                from_alert=from_alert,
                to_alert=to_alert,
                timeout_minutes=timeout_minutes,
                enabled=enabled,
            )
        except aiosqlite.IntegrityError as exc:
            raise ScenarioConflictError(str(exc)) from exc

    async def list_scenarios(self) -> Sequence[Scenario]:
        return await list_scenarios(self.db_path)

    async def get_scenario(self, scenario_id: int) -> Scenario:
        scenario = await get_scenario(self.db_path, scenario_id)
        if scenario is None:
            raise ScenarioNotFoundError(f"Scenario {scenario_id} not found.")
        return scenario

    async def update_scenario(
        self,
        scenario_id: int,
        *,
        name: str | None = None,
        from_alert: str | None = None,
        to_alert: str | None = None,
        timeout_minutes: int | None = None,
    ) -> Scenario:
        try:
            if from_alert is not None:
                existing = await get_scenario_by_from_alert(self.db_path, from_alert)
                if existing is not None and existing.id != scenario_id:
                    raise ScenarioConflictError(
                        f"Scenario with from_alert='{from_alert}' already exists."
                    )
            scenario = await update_scenario(
                self.db_path,
                scenario_id,
                name=name,
                from_alert=from_alert,
                to_alert=to_alert,
                timeout_minutes=timeout_minutes,
            )
        except aiosqlite.IntegrityError as exc:
            raise ScenarioConflictError(str(exc)) from exc

        if scenario is None:
            raise ScenarioNotFoundError(f"Scenario {scenario_id} not found.")
        return scenario

    async def set_enabled(self, scenario_id: int, *, enabled: bool) -> Scenario:
        scenario = await set_scenario_enabled(
            self.db_path,
            scenario_id,
            enabled=enabled,
        )
        if scenario is None:
            raise ScenarioNotFoundError(f"Scenario {scenario_id} not found.")
        return scenario

    async def delete_scenario(self, scenario_id: int) -> bool:
        try:
            return await delete_scenario(self.db_path, scenario_id)
        except aiosqlite.IntegrityError as exc:
            raise ScenarioInUseError(str(exc)) from exc


class ScenarioRuntimeService:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def get_runtime(self, vm_id: str) -> ScenarioRuntime | None:
        return await get_runtime(self.db_path, vm_id)

    async def record_agent_heartbeat(
        self,
        vm_id: str,
        vm_name: str | None,
        *,
        seen_at: dt.datetime | None = None,
    ) -> AgentStatus:
        vm_name = vm_name or None
        seen_at = seen_at or _utcnow()
        return await upsert_agent_status(
            self.db_path,
            vm_id=vm_id,
            vm_name=vm_name,
            last_seen_at_utc=seen_at,
        )

    async def get_agent_statuses(self) -> Sequence[AgentStatus]:
        return await list_agent_statuses(self.db_path)

    async def list_history(
        self,
        *,
        vm_id: str | None = None,
        limit: int = 50,
    ) -> Sequence[TransitionEvent]:
        return await list_transition_events(
            self.db_path,
            vm_id=vm_id,
            limit=limit,
        )

    async def list_due_runtimes(
        self,
        *,
        now: dt.datetime,
    ) -> Sequence[ScenarioRuntime]:
        return await list_due_runtimes(self.db_path, now_utc=now)

    async def list_active_status(self) -> Sequence[RuntimeStatus]:
        runtimes = await list_active_runtimes(self.db_path)
        agent_map = {agent.vm_id: agent for agent in await self.get_agent_statuses()}
        results: list[RuntimeStatus] = []
        for runtime in runtimes:
            agent = agent_map.get(runtime.vm_id)
            results.append(await self._runtime_to_status(runtime, agent))
        return tuple(results)

    async def list_all_status(self) -> Sequence[RuntimeStatus]:
        runtimes = list(await list_all_runtimes(self.db_path))
        agent_map = {agent.vm_id: agent for agent in await self.get_agent_statuses()}
        results: list[RuntimeStatus] = []
        seen_vm_ids: set[str] = set()
        for runtime in runtimes:
            agent = agent_map.get(runtime.vm_id)
            status = await self._runtime_to_status(runtime, agent)
            results.append(status)
            seen_vm_ids.add(runtime.vm_id)

        for agent in agent_map.values():
            if agent.vm_id in seen_vm_ids:
                continue
            synthetic_runtime = ScenarioRuntime(
                vm_id=agent.vm_id,
                current_scenario_id=None,
                deadline_at_utc=None,
                status="idle",
                last_received_alert=None,
                created_at_utc=agent.created_at_utc,
                updated_at_utc=agent.updated_at_utc,
            )
            results.append(
                RuntimeStatus(
                    runtime=synthetic_runtime,
                    scenario=None,
                    next_scenario=None,
                    vm_name=agent.vm_name,
                    agent_status=agent,
                )
            )
        return tuple(results)

    async def get_runtime_status(self, vm_id: str) -> RuntimeStatus | None:
        all_statuses = await self.list_all_status()
        for status in all_statuses:
            if status.runtime.vm_id == vm_id:
                return status
        return None

    async def assign_scenario(
        self,
        vm_id: str,
        scenario_id: int,
        *,
        minutes_override: int | None = None,
        now: dt.datetime | None = None,
    ) -> RuntimeStatus:
        scenario = await get_scenario(self.db_path, scenario_id)
        if scenario is None:
            raise ScenarioNotFoundError(f"Scenario {scenario_id} not found.")
        if not scenario.enabled:
            raise ScenarioRuntimeError("Сценарий отключён и не может быть назначен.")
        minutes = minutes_override if minutes_override is not None else scenario.timeout_minutes
        if minutes <= 0:
            raise ScenarioRuntimeError("Таймаут должен быть положительным числом минут.")
        deadline = _add_timeout(now or _utcnow(), minutes)
        runtime = await upsert_runtime(
            self.db_path,
            vm_id=vm_id,
            current_scenario_id=scenario.id,
            deadline_at_utc=deadline,
            status="active",
            last_received_alert=scenario.from_alert,
        )
        agent_map = {agent.vm_id: agent for agent in await self.get_agent_statuses()}
        return await self._runtime_to_status(runtime, agent_map.get(vm_id))

    async def _runtime_to_status(
        self,
        runtime: ScenarioRuntime,
        agent: AgentStatus | None,
    ) -> RuntimeStatus:
        scenario = None
        next_scenario = None
        if runtime.current_scenario_id is not None:
            scenario = await get_scenario(self.db_path, runtime.current_scenario_id)
            if scenario is not None:
                next_scenario = await get_scenario_by_from_alert(
                    self.db_path,
                    scenario.to_alert,
                )
        return RuntimeStatus(
            runtime=runtime,
            scenario=scenario,
            next_scenario=next_scenario,
            vm_name=agent.vm_name if agent else None,
            agent_status=agent,
        )

    async def handle_alert(
        self,
        vm_id: str,
        alert_name: str,
        *,
        now: dt.datetime | None = None,
    ) -> AlertHandlingResult:
        now = now or _utcnow()
        runtime = await get_runtime(self.db_path, vm_id)

        if runtime is None or runtime.status != "active":
            return await self._start_if_possible(vm_id, alert_name, now)

        if runtime.current_scenario_id is None:
            # Runtime persisted without active scenario, treat as idle.
            return await self._start_if_possible(vm_id, alert_name, now)

        current = await get_scenario(self.db_path, runtime.current_scenario_id)
        if current is None or not current.enabled:
            updated = await upsert_runtime(
                self.db_path,
                vm_id=vm_id,
                current_scenario_id=None,
                deadline_at_utc=None,
                status="stopped",
                last_received_alert=runtime.last_received_alert,
            )
            raise ScenarioRuntimeError(
                f"Scenario {runtime.current_scenario_id} disabled or missing."
            ) from None

        expected_alert = current.to_alert
        if alert_name == expected_alert:
            return await self._complete_current(
                runtime=runtime,
                current=current,
                now=now,
            )

        if alert_name == current.from_alert:
            return AlertHandlingResult(
                vm_id=vm_id,
                outcome="ignored",
                runtime=runtime,
                scenario=current,
                next_scenario=None,
                message="Duplicate of already confirmed from_alert ignored.",
            )

        # Ignore unrelated alerts while waiting for the expected to_alert.
        return AlertHandlingResult(
            vm_id=vm_id,
            outcome="ignored",
            runtime=runtime,
            scenario=current,
            next_scenario=None,
            message="Alert does not match current expectation.",
        )

    async def handle_timeout(
        self,
        vm_id: str,
        *,
        now: dt.datetime | None = None,
    ) -> TimeoutResult:
        now = now or _utcnow()
        runtime = await get_runtime(self.db_path, vm_id)
        if runtime is None or runtime.status != "active":
            return TimeoutResult(
                vm_id=vm_id,
                timed_out=False,
                runtime=runtime,
                scenario=None,
                message="Runtime inactive; timeout skipped.",
            )
        if runtime.current_scenario_id is None or runtime.deadline_at_utc is None:
            return TimeoutResult(
                vm_id=vm_id,
                timed_out=False,
                runtime=runtime,
                scenario=None,
                message="Runtime missing active scenario or deadline.",
            )
        if runtime.deadline_at_utc > now:
            return TimeoutResult(
                vm_id=vm_id,
                timed_out=False,
                runtime=runtime,
                scenario=None,
                message="Deadline not reached yet.",
            )

        current = await get_scenario(self.db_path, runtime.current_scenario_id)
        if current is None:
            return TimeoutResult(
                vm_id=vm_id,
                timed_out=False,
                runtime=runtime,
                scenario=None,
                message="Scenario not found; timeout skipped.",
            )

        await append_transition_event(
            self.db_path,
            vm_id=vm_id,
            scenario_id=current.id,
            from_alert=current.from_alert,
            to_alert=current.to_alert,
            event_type="timeout",
            deadline_at_utc=runtime.deadline_at_utc,
            event_received_at_utc=None,
        )

        new_deadline = _add_timeout(now, current.timeout_minutes)
        runtime = await upsert_runtime(
            self.db_path,
            vm_id=vm_id,
            current_scenario_id=current.id,
            deadline_at_utc=new_deadline,
            status="active",
            last_received_alert=runtime.last_received_alert,
        )

        return TimeoutResult(
            vm_id=vm_id,
            timed_out=True,
            runtime=runtime,
            scenario=current,
            message="Timeout recorded and deadline extended.",
        )

    async def manual_advance(
        self,
        vm_id: str,
        *,
        now: dt.datetime | None = None,
    ) -> AlertHandlingResult:
        now = now or _utcnow()
        runtime = await get_runtime(self.db_path, vm_id)
        if runtime is None or runtime.status != "active":
            raise ScenarioRuntimeError("Runtime not active; cannot advance.")
        if runtime.current_scenario_id is None:
            raise ScenarioRuntimeError("Runtime has no active scenario to advance.")
        current = await get_scenario(self.db_path, runtime.current_scenario_id)
        if current is None:
            raise ScenarioRuntimeError("Current scenario not found.")
        return await self._complete_current(runtime=runtime, current=current, now=now)

    async def set_runtime_timeout(
        self,
        vm_id: str,
        *,
        minutes: int,
        now: dt.datetime | None = None,
    ) -> ScenarioRuntime:
        if minutes <= 0:
            raise ScenarioRuntimeError("Minutes must be positive.")
        runtime = await get_runtime(self.db_path, vm_id)
        if runtime is None or runtime.status != "active":
            raise ScenarioRuntimeError("Runtime not active; cannot update timeout.")
        if runtime.current_scenario_id is None:
            raise ScenarioRuntimeError("Runtime has no active scenario.")
        deadline = _add_timeout(now or _utcnow(), minutes)
        return await upsert_runtime(
            self.db_path,
            vm_id=vm_id,
            current_scenario_id=runtime.current_scenario_id,
            deadline_at_utc=deadline,
            status="active",
            last_received_alert=runtime.last_received_alert,
        )

    async def set_scenario_timeout(
        self,
        scenario_id: int,
        *,
        minutes: int,
    ) -> Scenario:
        if minutes <= 0:
            raise ScenarioRuntimeError("Minutes must be positive.")
        scenario = await update_scenario(
            self.db_path,
            scenario_id,
            timeout_minutes=minutes,
        )
        if scenario is None:
            raise ScenarioNotFoundError(f"Scenario {scenario_id} not found.")
        return scenario

    async def stop_runtime(self, vm_id: str) -> ScenarioRuntime:
        runtime = await get_runtime(self.db_path, vm_id)
        if runtime is None:
            raise ScenarioRuntimeError("Runtime not found.")
        runtime = await upsert_runtime(
            self.db_path,
            vm_id=vm_id,
            current_scenario_id=None,
            deadline_at_utc=None,
            status="stopped",
            last_received_alert=runtime.last_received_alert,
        )
        return runtime

    async def _start_if_possible(
        self,
        vm_id: str,
        alert_name: str,
        now: dt.datetime,
    ) -> AlertHandlingResult:
        scenario = await get_scenario_by_from_alert(self.db_path, alert_name)
        if scenario is None or not scenario.enabled:
            return AlertHandlingResult(
                vm_id=vm_id,
                outcome="ignored",
                runtime=None,
                scenario=None,
                next_scenario=None,
                message="No scenario to start for received alert.",
            )

        deadline = _add_timeout(now, scenario.timeout_minutes)
        runtime = await upsert_runtime(
            self.db_path,
            vm_id=vm_id,
            current_scenario_id=scenario.id,
            deadline_at_utc=deadline,
            status="active",
            last_received_alert=scenario.from_alert,
        )

        return AlertHandlingResult(
            vm_id=vm_id,
            outcome="started",
            runtime=runtime,
            scenario=scenario,
            next_scenario=scenario,
            message="Scenario started.",
        )

    async def _complete_current(
        self,
        runtime: ScenarioRuntime,
        current: Scenario,
        now: dt.datetime,
    ) -> AlertHandlingResult:
        vm_id = runtime.vm_id
        await append_transition_event(
            self.db_path,
            vm_id=vm_id,
            scenario_id=current.id,
            from_alert=current.from_alert,
            to_alert=current.to_alert,
            event_type="success",
            deadline_at_utc=runtime.deadline_at_utc or now,
            event_received_at_utc=now,
        )

        next_scenario = await get_scenario_by_from_alert(
            self.db_path,
            current.to_alert,
        )
        if next_scenario is None or not next_scenario.enabled:
            runtime = await upsert_runtime(
                self.db_path,
                vm_id=vm_id,
                current_scenario_id=None,
                deadline_at_utc=None,
                status="success",
                last_received_alert=current.to_alert,
            )
            return AlertHandlingResult(
                vm_id=vm_id,
                outcome="completed",
                runtime=runtime,
                scenario=current,
                next_scenario=None,
                message="Scenario chain completed.",
            )

        deadline = _add_timeout(now, next_scenario.timeout_minutes)
        runtime = await upsert_runtime(
            self.db_path,
            vm_id=vm_id,
            current_scenario_id=next_scenario.id,
            deadline_at_utc=deadline,
            status="active",
            last_received_alert=current.to_alert,
        )

        return AlertHandlingResult(
            vm_id=vm_id,
            outcome="progressed",
            runtime=runtime,
            scenario=current,
            next_scenario=next_scenario,
            message="Advanced to next scenario.",
        )
