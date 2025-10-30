from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Sequence

import aiosqlite


PROGRESSIONS_INIT_SQL = """
CREATE TABLE IF NOT EXISTS scenario (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    from_alert TEXT NOT NULL,
    to_alert TEXT NOT NULL,
    timeout_minutes INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_from_alert
    ON scenario (from_alert);

CREATE TABLE IF NOT EXISTS scenario_runtime (
    vm_id TEXT PRIMARY KEY,
    current_scenario_id INTEGER,
    deadline_at_utc TEXT,
    status TEXT NOT NULL,
    last_received_alert TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL,
    FOREIGN KEY (current_scenario_id) REFERENCES scenario (id)
);
CREATE INDEX IF NOT EXISTS idx_scenario_runtime_status_deadline
    ON scenario_runtime (status, deadline_at_utc);

CREATE TABLE IF NOT EXISTS scenario_transition_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vm_id TEXT NOT NULL,
    scenario_id INTEGER NOT NULL,
    from_alert TEXT NOT NULL,
    to_alert TEXT NOT NULL,
    event_type TEXT NOT NULL,
    deadline_at_utc TEXT NOT NULL,
    event_received_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    FOREIGN KEY (scenario_id) REFERENCES scenario (id)
);
CREATE INDEX IF NOT EXISTS idx_transition_event_vm_created
    ON scenario_transition_event (vm_id, created_at_utc DESC);

CREATE TABLE IF NOT EXISTS agent_status (
    vm_id TEXT PRIMARY KEY,
    vm_name TEXT,
    last_seen_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);
"""


@dataclass(slots=True)
class Scenario:
    id: int
    name: str
    from_alert: str
    to_alert: str
    timeout_minutes: int
    enabled: bool
    created_at_utc: dt.datetime
    updated_at_utc: dt.datetime


@dataclass(slots=True)
class ScenarioRuntime:
    vm_id: str
    current_scenario_id: int | None
    deadline_at_utc: dt.datetime | None
    status: str
    last_received_alert: str | None
    created_at_utc: dt.datetime
    updated_at_utc: dt.datetime


@dataclass(slots=True)
class TransitionEvent:
    id: int
    vm_id: str
    scenario_id: int
    from_alert: str
    to_alert: str
    event_type: str
    deadline_at_utc: dt.datetime
    event_received_at_utc: dt.datetime | None
    created_at_utc: dt.datetime


@dataclass(slots=True)
class AgentStatus:
    vm_id: str
    vm_name: str | None
    last_seen_at_utc: dt.datetime
    created_at_utc: dt.datetime
    updated_at_utc: dt.datetime


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _serialize_datetime(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse_datetime(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    dt_value = dt.datetime.fromisoformat(value)
    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=dt.timezone.utc)
    return dt_value.astimezone(dt.timezone.utc)


async def init_progressions_db(path: str) -> None:
    async with aiosqlite.connect(path) as db:
        await db.executescript(PROGRESSIONS_INIT_SQL)
        await db.commit()


async def create_scenario(
    path: str,
    *,
    name: str,
    from_alert: str,
    to_alert: str,
    timeout_minutes: int,
    enabled: bool = True,
) -> Scenario:
    created_at = _utcnow()
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            """
            INSERT INTO scenario (
                name,
                from_alert,
                to_alert,
                timeout_minutes,
                enabled,
                created_at_utc,
                updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                from_alert,
                to_alert,
                timeout_minutes,
                int(enabled),
                _serialize_datetime(created_at),
                _serialize_datetime(created_at),
            ),
        )
        await db.commit()
        scenario_id = cur.lastrowid
    scenario = await get_scenario(path, scenario_id)
    if scenario is None:
        raise RuntimeError("Failed to load scenario after insert")
    return scenario


async def get_scenario(path: str, scenario_id: int) -> Scenario | None:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                id,
                name,
                from_alert,
                to_alert,
                timeout_minutes,
                enabled,
                created_at_utc,
                updated_at_utc
            FROM scenario
            WHERE id = ?
            """,
            (scenario_id,),
        )
        row = await cur.fetchone()
    return _row_to_scenario(row) if row else None


async def get_scenario_by_from_alert(path: str, from_alert: str) -> Scenario | None:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                id,
                name,
                from_alert,
                to_alert,
                timeout_minutes,
                enabled,
                created_at_utc,
                updated_at_utc
            FROM scenario
            WHERE from_alert = ?
            """,
            (from_alert,),
        )
        row = await cur.fetchone()
    return _row_to_scenario(row) if row else None


async def list_scenarios(path: str) -> Sequence[Scenario]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                id,
                name,
                from_alert,
                to_alert,
                timeout_minutes,
                enabled,
                created_at_utc,
                updated_at_utc
            FROM scenario
            ORDER BY id
            """
        )
        rows = await cur.fetchall()
    return tuple(_row_to_scenario(row) for row in rows)


async def update_scenario(
    path: str,
    scenario_id: int,
    *,
    name: str | None = None,
    from_alert: str | None = None,
    to_alert: str | None = None,
    timeout_minutes: int | None = None,
) -> Scenario | None:
    updates: list[str] = []
    values: list[object] = []
    if name is not None:
        updates.append("name = ?")
        values.append(name)
    if from_alert is not None:
        updates.append("from_alert = ?")
        values.append(from_alert)
    if to_alert is not None:
        updates.append("to_alert = ?")
        values.append(to_alert)
    if timeout_minutes is not None:
        updates.append("timeout_minutes = ?")
        values.append(timeout_minutes)
    if not updates:
        return await get_scenario(path, scenario_id)

    values.extend(
        (
            _serialize_datetime(_utcnow()),
            scenario_id,
        )
    )

    async with aiosqlite.connect(path) as db:
        await db.execute(
            f"""
            UPDATE scenario
            SET {', '.join(updates)}, updated_at_utc = ?
            WHERE id = ?
            """,
            values,
        )
        await db.commit()
    return await get_scenario(path, scenario_id)


async def set_scenario_enabled(
    path: str,
    scenario_id: int,
    *,
    enabled: bool,
) -> Scenario | None:
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            UPDATE scenario
            SET enabled = ?, updated_at_utc = ?
            WHERE id = ?
            """,
            (
                int(enabled),
                _serialize_datetime(_utcnow()),
                scenario_id,
            ),
        )
        await db.commit()
    return await get_scenario(path, scenario_id)


async def delete_scenario(path: str, scenario_id: int) -> bool:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute("DELETE FROM scenario WHERE id = ?", (scenario_id,))
        await db.commit()
        return cur.rowcount > 0


async def upsert_runtime(
    path: str,
    *,
    vm_id: str,
    current_scenario_id: int | None,
    deadline_at_utc: dt.datetime | None,
    status: str,
    last_received_alert: str | None,
) -> ScenarioRuntime:
    now = _utcnow()
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO scenario_runtime (
                vm_id,
                current_scenario_id,
                deadline_at_utc,
                status,
                last_received_alert,
                created_at_utc,
                updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vm_id) DO UPDATE SET
                current_scenario_id = excluded.current_scenario_id,
                deadline_at_utc = excluded.deadline_at_utc,
                status = excluded.status,
                last_received_alert = excluded.last_received_alert,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                vm_id,
                current_scenario_id,
                _serialize_datetime(deadline_at_utc),
                status,
                last_received_alert,
                _serialize_datetime(now),
                _serialize_datetime(now),
            ),
        )
        await db.commit()
    runtime = await get_runtime(path, vm_id)
    if runtime is None:
        raise RuntimeError("Failed to load runtime after upsert")
    return runtime


async def get_runtime(path: str, vm_id: str) -> ScenarioRuntime | None:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                vm_id,
                current_scenario_id,
                deadline_at_utc,
                status,
                last_received_alert,
                created_at_utc,
                updated_at_utc
            FROM scenario_runtime
            WHERE vm_id = ?
            """,
            (vm_id,),
        )
        row = await cur.fetchone()
    return _row_to_runtime(row) if row else None


async def list_active_runtimes(path: str) -> Sequence[ScenarioRuntime]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                vm_id,
                current_scenario_id,
                deadline_at_utc,
                status,
                last_received_alert,
                created_at_utc,
                updated_at_utc
            FROM scenario_runtime
            WHERE status = 'active'
            ORDER BY deadline_at_utc
            """
        )
        rows = await cur.fetchall()
    return tuple(_row_to_runtime(row) for row in rows)


async def list_all_runtimes(path: str) -> Sequence[ScenarioRuntime]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                vm_id,
                current_scenario_id,
                deadline_at_utc,
                status,
                last_received_alert,
                created_at_utc,
                updated_at_utc
            FROM scenario_runtime
            ORDER BY updated_at_utc DESC
            """
        )
        rows = await cur.fetchall()
    return tuple(_row_to_runtime(row) for row in rows)


async def list_due_runtimes(path: str, *, now_utc: dt.datetime) -> Sequence[ScenarioRuntime]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                vm_id,
                current_scenario_id,
                deadline_at_utc,
                status,
                last_received_alert,
                created_at_utc,
                updated_at_utc
            FROM scenario_runtime
            WHERE status = 'active'
              AND deadline_at_utc IS NOT NULL
              AND deadline_at_utc <= ?
            ORDER BY deadline_at_utc
            """,
            (_serialize_datetime(now_utc),),
        )
        rows = await cur.fetchall()
    return tuple(_row_to_runtime(row) for row in rows)


async def delete_runtime(path: str, vm_id: str) -> bool:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute("DELETE FROM scenario_runtime WHERE vm_id = ?", (vm_id,))
        await db.commit()
        return cur.rowcount > 0


async def upsert_agent_status(
    path: str,
    *,
    vm_id: str,
    vm_name: str | None,
    last_seen_at_utc: dt.datetime,
) -> AgentStatus:
    serialized_last_seen = _serialize_datetime(last_seen_at_utc)
    timestamp = _serialize_datetime(_utcnow())
    async with aiosqlite.connect(path) as db:
        await db.execute(
            """
            INSERT INTO agent_status (
                vm_id,
                vm_name,
                last_seen_at_utc,
                created_at_utc,
                updated_at_utc
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(vm_id) DO UPDATE SET
                vm_name = excluded.vm_name,
                last_seen_at_utc = excluded.last_seen_at_utc,
                updated_at_utc = excluded.updated_at_utc
            """,
            (
                vm_id,
                vm_name,
                serialized_last_seen,
                timestamp,
                timestamp,
            ),
        )
        await db.commit()

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT vm_id, vm_name, last_seen_at_utc, created_at_utc, updated_at_utc
            FROM agent_status
            WHERE vm_id = ?
            """,
            (vm_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("Failed to load agent status after upsert")
    return _row_to_agent_status(row)


async def list_agent_statuses(path: str) -> Sequence[AgentStatus]:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT vm_id, vm_name, last_seen_at_utc, created_at_utc, updated_at_utc
            FROM agent_status
            ORDER BY updated_at_utc DESC
            """
        )
        rows = await cur.fetchall()
    return tuple(_row_to_agent_status(row) for row in rows)


async def append_transition_event(
    path: str,
    *,
    vm_id: str,
    scenario_id: int,
    from_alert: str,
    to_alert: str,
    event_type: str,
    deadline_at_utc: dt.datetime,
    event_received_at_utc: dt.datetime | None,
) -> TransitionEvent:
    created_at = _utcnow()
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            """
            INSERT INTO scenario_transition_event (
                vm_id,
                scenario_id,
                from_alert,
                to_alert,
                event_type,
                deadline_at_utc,
                event_received_at_utc,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                vm_id,
                scenario_id,
                from_alert,
                to_alert,
                event_type,
                _serialize_datetime(deadline_at_utc),
                _serialize_datetime(event_received_at_utc),
                _serialize_datetime(created_at),
            ),
        )
        await db.commit()
        event_id = cur.lastrowid
    event = await get_transition_event(path, event_id)
    if event is None:
        raise RuntimeError("Failed to load transition event after insert")
    return event


async def get_transition_event(path: str, event_id: int) -> TransitionEvent | None:
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT
                id,
                vm_id,
                scenario_id,
                from_alert,
                to_alert,
                event_type,
                deadline_at_utc,
                event_received_at_utc,
                created_at_utc
            FROM scenario_transition_event
            WHERE id = ?
            """,
            (event_id,),
        )
        row = await cur.fetchone()
    return _row_to_transition_event(row) if row else None


async def list_transition_events(
    path: str,
    *,
    vm_id: str | None = None,
    limit: int = 50,
) -> Sequence[TransitionEvent]:
    if limit <= 0:
        return ()

    query = """
        SELECT
            id,
            vm_id,
            scenario_id,
            from_alert,
            to_alert,
            event_type,
            deadline_at_utc,
            event_received_at_utc,
            created_at_utc
        FROM scenario_transition_event
    """
    params: list[object] = []
    filters: list[str] = []
    if vm_id is not None:
        filters.append("vm_id = ?")
        params.append(vm_id)
    if filters:
        query += " WHERE " + " AND ".join(filters)
    query += " ORDER BY created_at_utc DESC LIMIT ?"
    params.append(limit)

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
    return tuple(_row_to_transition_event(row) for row in rows)


def _row_to_scenario(row: aiosqlite.Row) -> Scenario:
    return Scenario(
        id=row["id"],
        name=row["name"],
        from_alert=row["from_alert"],
        to_alert=row["to_alert"],
        timeout_minutes=row["timeout_minutes"],
        enabled=bool(row["enabled"]),
        created_at_utc=_parse_datetime(row["created_at_utc"]),
        updated_at_utc=_parse_datetime(row["updated_at_utc"]),
    )


def _row_to_runtime(row: aiosqlite.Row) -> ScenarioRuntime:
    return ScenarioRuntime(
        vm_id=row["vm_id"],
        current_scenario_id=row["current_scenario_id"],
        deadline_at_utc=_parse_datetime(row["deadline_at_utc"]),
        status=row["status"],
        last_received_alert=row["last_received_alert"],
        created_at_utc=_parse_datetime(row["created_at_utc"]),
        updated_at_utc=_parse_datetime(row["updated_at_utc"]),
    )


def _row_to_transition_event(row: aiosqlite.Row) -> TransitionEvent:
    return TransitionEvent(
        id=row["id"],
        vm_id=row["vm_id"],
        scenario_id=row["scenario_id"],
        from_alert=row["from_alert"],
        to_alert=row["to_alert"],
        event_type=row["event_type"],
        deadline_at_utc=_parse_datetime(row["deadline_at_utc"]),
        event_received_at_utc=_parse_datetime(row["event_received_at_utc"]),
        created_at_utc=_parse_datetime(row["created_at_utc"]),
    )


def _row_to_agent_status(row: aiosqlite.Row) -> AgentStatus:
    return AgentStatus(
        vm_id=row["vm_id"],
        vm_name=row["vm_name"],
        last_seen_at_utc=_parse_datetime(row["last_seen_at_utc"]),
        created_at_utc=_parse_datetime(row["created_at_utc"]),
        updated_at_utc=_parse_datetime(row["updated_at_utc"]),
    )
