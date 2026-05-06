import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

TM_API_KEY = os.environ.get("TICKETMASTER_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = Path("state.json")

BERLIN_EVENT_IDS = {
    "Z698xZC2Z1kPK8xFw": "Berlin Oct 15 (Astra Kulturhaus)",
    "Z698xZC2Z1ku3GAuZ": "Berlin Oct 16 (Astra Kulturhaus)",
}

AVAILABLE_STATUSES = {"onsale", "rescheduled"}
UNAVAILABLE_STATUSES = {"offsale", "cancelled", "postponed"}


def fetch_events() -> list[dict]:
    resp = requests.get(
        "https://app.ticketmaster.com/discovery/v2/events.json",
        params={
            "apikey": TM_API_KEY,
            "keyword": "james blake",
            "countryCode": "DE",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("_embedded", {}).get("events", [])


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[NOTIFY] {message}")
        return
    resp = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
        timeout=10,
    )
    if not resp.ok:
        print(f"Telegram error: {resp.text}")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    if not TM_API_KEY:
        print("Missing TICKETMASTER_API_KEY")
        sys.exit(1)

    print(f"[{datetime.now().isoformat()}] Checking James Blake Berlin tickets...")

    try:
        events = fetch_events()
    except Exception as e:
        print(f"API call failed: {e}")
        sys.exit(1)

    # Filter to only Berlin events we care about
    berlin = [e for e in events if e["id"] in BERLIN_EVENT_IDS]
    print(f"Berlin events found: {len(berlin)}")

    state = load_state()
    newly_available = []

    for ev in berlin:
        ev_id = ev["id"]
        label = BERLIN_EVENT_IDS[ev_id]
        status = ev["dates"]["status"]["code"]
        url = ev.get("url", "")
        available = status in AVAILABLE_STATUSES

        prev_available = state.get(ev_id, {}).get("available")
        print(f"  {label}: status={status} available={available} (prev={prev_available})")

        # Notify only on transition to available
        if available and prev_available is not True:
            newly_available.append({"label": label, "status": status, "url": url})

        state[ev_id] = {
            "available": available,
            "status": status,
            "last_check": datetime.now().isoformat(),
        }

    save_state(state)

    if newly_available:
        lines = ["🎵 <b>James Blake Berlin — Tickets Available!</b>\n"]
        for ev in newly_available:
            lines.append(f"📅 <b>{ev['label']}</b> — {ev['status'].upper()}")
            lines.append(f"🔗 {ev['url']}\n")
        send_telegram("\n".join(lines))
        print("Telegram notification sent.")
    else:
        print("No change — no notification sent.")


main()
