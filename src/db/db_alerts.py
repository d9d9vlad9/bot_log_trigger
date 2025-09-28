import aiosqlite


DB_INIT_SQL = """
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
);
"""

async def init_db(path: str):
    async with aiosqlite.connect(path) as db:
        await db.executescript(DB_INIT_SQL)
        await db.commit()

async def add_alert(path: str, name: str, pattern: str) -> int:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "INSERT INTO alerts (name, pattern, enabled) VALUES (?, ?, 1)",
            (name, pattern)
        )
        await db.commit()
        return cur.lastrowid

async def list_alerts(path: str):
    async with aiosqlite.connect(path) as db:
        cur = await db.execute(
            "SELECT id, name, pattern, enabled FROM alerts ORDER BY id"
        )
        return await cur.fetchall()

async def remove_alert(path: str, alert_id: int) -> bool:
    async with aiosqlite.connect(path) as db:
        cur = await db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
        await db.commit()
        return cur.rowcount > 0
