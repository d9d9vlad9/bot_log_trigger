###########################
# AGENT CODE ON EACH VM  #
###########################
import time, re, requests, json

CONFIG_FILE = "agent/config.json"

with open(CONFIG_FILE) as f:
    cfg = json.load(f)

VM_ID = cfg["VM_ID"]
VM_NAME = cfg.get("VM_NAME")
BOT_SERVER_URL = cfg["BOT_SERVER_URL"]
ALERTS_URL = f"{BOT_SERVER_URL}/alerts"
LOG_FILE = cfg["LOG_FILE"]
FETCH_INTERVAL = cfg.get("FETCH_INTERVAL", 60)

local_alerts = []

def fetch_alerts():
    global local_alerts
    try:
        r = requests.get(ALERTS_URL)
        r.raise_for_status()
        alerts = r.json()
        local_alerts = [{"id": a["id"], "name": a["name"], "pattern": re.compile(a["pattern"])} for a in alerts]
    except Exception as e:
        print("Failed to fetch alerts:", e)

fetch_alerts()
last_fetch = time.time()

with open(LOG_FILE) as f:
    f.seek(0, 2)
    while True:
        if time.time() - last_fetch > FETCH_INTERVAL:
            fetch_alerts()
            last_fetch = time.time()

        line = f.readline()
        if not line:
            time.sleep(0.5)
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
                    })
                    print("Response:", resp.status_code, resp.text)
                except Exception as e:
                    print("Failed to send alert:", e)
