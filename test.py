###########################
# TEST SETUP FOR LOCAL RUN WITH ENABLED COLUMN #
###########################

import sqlite3
import os
import json

DB_PATH = "alerts.db"

# Удаляем старую базу, если она есть, чтобы пересоздать с нужной схемой
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

# Создаем таблицу alerts с колонкой enabled
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute('''
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1
)
''')
conn.commit()
conn.close()

# Создаем тестовый лог-файл
LOG_FILE = "test_log.log"
with open(LOG_FILE, "w") as f:
    f.write("Starting test log\n")

# Создаем тестовый конфиг агента (agent/config.json)
os.makedirs("agent", exist_ok=True)
config_data = {
    "VM_ID": "test_vm",
    "VM_NAME": "Test VM",
    "BOT_SERVER_URL": "http://127.0.0.1:8000",
    "LOG_FILE": LOG_FILE,
    "FETCH_INTERVAL": 10
}
with open("agent/config.json", "w") as f:
    json.dump(config_data, f, indent=4)

# Добавляем тестовый алерт через базу с включенным enabled
pattern = "ERROR"
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("INSERT INTO alerts (name, pattern, enabled) VALUES (?, ?, ?)", ("Test Error Alert", pattern, 1))
conn.commit()
conn.close()

print("Test setup completed.")
print(f"- Test log file: {LOG_FILE}")
print("- Agent config: agent/config.json")
print("- Test alert added to DB with enabled=1")

# Инструкции по запуску:
# 1. Запустить FastAPI сервер: uvicorn src.bot_server:app --reload
# 2. Запустить агента: python agent/agent.py
# 3. Добавить строку с ERROR в test_log.log для проверки алерта.
