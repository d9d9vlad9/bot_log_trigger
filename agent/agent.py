###########################
# AGENT CODE ON EACH VM  #
###########################
import time, re, requests, json
from datetime import datetime
from pathlib import Path

CONFIG_FILE = "agent/config.json"

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

VM_ID = cfg["VM_ID"]
VM_NAME = cfg.get("VM_NAME")
BOT_SERVER_URL = cfg["BOT_SERVER_URL"]
ALERTS_URL = f"{BOT_SERVER_URL}/alerts"
LOG_FILE_TEMPLATE = cfg["LOG_FILE"]
LOG_POLL_INTERVAL = cfg.get("LOG_POLL_INTERVAL", 0.5)
if LOG_POLL_INTERVAL <= 0:
    LOG_POLL_INTERVAL = 0.5

if "CONFIG_REFRESH_INTERVAL" in cfg:
    CONFIG_REFRESH_INTERVAL = cfg["CONFIG_REFRESH_INTERVAL"]
elif "FETCH_INTERVAL" in cfg:
    CONFIG_REFRESH_INTERVAL = cfg["FETCH_INTERVAL"]
else:
    CONFIG_REFRESH_INTERVAL = 600

if CONFIG_REFRESH_INTERVAL <= 0:
    CONFIG_REFRESH_INTERVAL = 600
AUTH_TOKEN = cfg.get("AUTH_TOKEN")

local_alerts = []

def request_headers():
    if AUTH_TOKEN:
        return {"X-Alert-Token": AUTH_TOKEN}
    return None

def fetch_alerts():
    global local_alerts
    try:
        r = requests.get(ALERTS_URL, headers=request_headers())
        r.raise_for_status()
        alerts = r.json()
        local_alerts = [{"id": a["id"], "name": a["name"], "pattern": re.compile(a["pattern"])} for a in alerts]
    except Exception as e:
        print("Failed to fetch alerts:", e)

fetch_alerts()
last_fetch = time.time()

def current_log_path() -> Path:
    return Path(datetime.now().strftime(LOG_FILE_TEMPLATE))

log_path = current_log_path()
log_file = None

while True:
    new_path = current_log_path()
    if log_file is None or new_path != log_path:
        if log_file:
            log_file.close()
        log_path = new_path
        try:
            log_file = open(log_path, "r")
            log_file.seek(0, 2)
            print("Switched to log file:", log_path)
        except FileNotFoundError:
            print("Log file not found, waiting:", log_path)
            log_file = None
            time.sleep(1)
            continue

    if time.time() - last_fetch > CONFIG_REFRESH_INTERVAL:
        fetch_alerts()
        last_fetch = time.time()

    line = log_file.readline() if log_file else ""
    if not line:
        time.sleep(LOG_POLL_INTERVAL)
        continue

    for alert in local_alerts:
        if alert["pattern"].search(line):
            print("Matched alert:", alert["name"], "=> sending POST")
            try:
                resp = requests.post(f"{BOT_SERVER_URL}/log_alert", json={
                    "vm_id": VM_ID,
                    "vm_name": VM_NAME,
                    "alert_name": alert["name"],
                    "alert_id": alert["id"],
                    "log_line": line.strip()
                }, headers=request_headers())
                print("Response:", resp.status_code, resp.text)
            except Exception as e:
                print("Failed to send alert:", e)
