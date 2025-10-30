from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.db.db_alerts import add_alert, init_db as init_alerts_db
from src.db.db_progressions import (
    append_transition_event,
    create_scenario,
    init_progressions_db,
    upsert_runtime,
)

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "alerts.db"
LOG_FILE = BASE_DIR / "test_log.log"
AGENT_DIR = BASE_DIR / "agent"
AGENT_CONFIG = AGENT_DIR / "config.json"


async def setup_database(path: Path) -> None:
    if path.exists():
        path.unlink()

    await init_alerts_db(str(path))
    await init_progressions_db(str(path))

    await add_alert(
        str(path),
        name="Test Error Alert",
        pattern="ERROR",
    )

    for alert_name in ("A", "B", "C"):
        await add_alert(
            str(path),
            name=alert_name,
            pattern=alert_name,
            is_scenario_trigger=True,
        )

    scenario_ab = await create_scenario(
        str(path),
        name="A→B",
        from_alert="A",
        to_alert="B",
        timeout_minutes=5,
    )
    await create_scenario(
        str(path),
        name="B→C",
        from_alert="B",
        to_alert="C",
        timeout_minutes=10,
    )

    now = datetime.now(timezone.utc)
    await upsert_runtime(
        str(path),
        vm_id="test_vm",
        current_scenario_id=scenario_ab.id,
        deadline_at_utc=now + timedelta(minutes=scenario_ab.timeout_minutes),
        status="active",
        last_received_alert="A",
    )
    await append_transition_event(
        str(path),
        vm_id="test_vm",
        scenario_id=scenario_ab.id,
        from_alert="A",
        to_alert="B",
        event_type="success",
        deadline_at_utc=now,
        event_received_at_utc=now,
    )


def setup_log_file(path: Path) -> None:
    path.write_text("Starting test log\n", encoding="utf-8")


def setup_agent_config(path: Path, log_file: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    config_data = {
        "VM_ID": "test_vm",
        "VM_NAME": "Test VM",
        "BOT_SERVER_URL": "http://127.0.0.1:8000",
        "LOG_FILE": str(log_file),
        "FETCH_INTERVAL": 10,
    }
    path.write_text(json.dumps(config_data, indent=4), encoding="utf-8")


async def main() -> None:
    await setup_database(DB_PATH)
    setup_log_file(LOG_FILE)
    setup_agent_config(AGENT_CONFIG, LOG_FILE)

    print("Test setup completed.")
    print(f"- Database: {DB_PATH}")
    print(f"- Test log file: {LOG_FILE}")
    print(f"- Agent config: {AGENT_CONFIG}")


if __name__ == "__main__":
    asyncio.run(main())
