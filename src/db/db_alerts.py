import aiosqlite


DB_INIT_SQL = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    threshold_count INTEGER NOT NULL DEFAULT 1,
    threshold_window_seconds INTEGER NOT NULL DEFAULT 60,
    is_scenario_trigger INTEGER NOT NULL DEFAULT 0
);
"""

async def init_db(path: str):
    async with aiosqlite.connect(path) as db:
        await db.executescript(DB_INIT_SQL)
        await _ensure_additional_columns(db)
        await db.commit()

async def add_alert(
    path: str,
    name: str,
    pattern: str,
    threshold_count: int = 1,
    threshold_window_seconds: int = 60,
    *,
    is_scenario_trigger: bool = False,
) -> int:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            """
            INSERT INTO alerts (
                name,
                pattern,
                enabled,
                threshold_count,
                threshold_window_seconds,
                is_scenario_trigger
            )
            VALUES (?, ?, 1, ?, ?, ?)
            """,
            (
                name,
                pattern,
                threshold_count,
                threshold_window_seconds,
                int(is_scenario_trigger),
            ),
        )
        await db.commit()
        return cur.lastrowid

async def list_alerts(path: str):
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            """
            SELECT
                id,
                name,
                pattern,
                enabled,
                threshold_count,
                threshold_window_seconds,
                is_scenario_trigger
            FROM alerts
            ORDER BY id
            """
        )
        return await cur.fetchall()

async def remove_alert(path: str, alert_id: int) -> bool:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
        await db.commit()
        return cur.rowcount > 0

async def toggle_alert_scenario_flag(path: str, alert_id: int) -> int | None:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT is_scenario_trigger FROM alerts WHERE id = ?",
            (alert_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        new_value = 0 if row[0] else 1
        await db.execute(
            "UPDATE alerts SET is_scenario_trigger = ? WHERE id = ?",
            (new_value, alert_id),
        )
        await db.commit()
        return new_value

async def get_alert(path: str, alert_id: int):
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            """
            SELECT
                id,
                name,
                pattern,
                enabled,
                threshold_count,
                threshold_window_seconds,
                is_scenario_trigger
            FROM alerts
            WHERE id = ?
            """,
            (alert_id,),
        )
        return await cur.fetchone()

async def toggle_alert_enabled(path: str, alert_id: int) -> int | None:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT enabled FROM alerts WHERE id = ?",
            (alert_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        new_value = 0 if row[0] else 1
        await db.execute(
            "UPDATE alerts SET enabled = ? WHERE id = ?",
            (new_value, alert_id),
        )
        await db.commit()
        return new_value

async def update_alert_thresholds(
    path: str,
    alert_id: int,
    *,
    threshold_count: int | None = None,
    threshold_window_seconds: int | None = None,
) -> bool:
    parts: list[str] = []
    values: list[int] = []
    if threshold_count is not None:
        parts.append("threshold_count = ?")
        values.append(threshold_count)
    if threshold_window_seconds is not None:
        parts.append("threshold_window_seconds = ?")
        values.append(threshold_window_seconds)

    if not parts:
        return False

    values.append(alert_id)

    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            f"UPDATE alerts SET {', '.join(parts)} WHERE id = ?",
            values,
        )
        await db.commit()
        return cur.rowcount > 0

async def _ensure_additional_columns(db: aiosqlite.Connection) -> None:
    for column_sql in (
        "ALTER TABLE alerts ADD COLUMN threshold_count INTEGER NOT NULL DEFAULT 1",
        (
            "ALTER TABLE alerts ADD COLUMN "
            "threshold_window_seconds INTEGER NOT NULL DEFAULT 60"
        ),
        "ALTER TABLE alerts ADD COLUMN is_scenario_trigger INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            await db.execute(column_sql)
        except aiosqlite.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise
