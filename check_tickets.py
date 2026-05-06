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

BERLIN_EVENTS = {
    "953422232": "Berlin Oct 15 (Astra Kulturhaus)",
    "352305340": "Berlin Oct 16 (Astra Kulturhaus)",
}

# These statuses mean you can buy — primary or resale relisted
AVAILABLE_STATUSES = {"onsale", "rescheduled"}


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


def _parse_events(raw: list) -> list[dict]:
    results = []
    for ev in raw:
        ev_url = ev.get("url", "")
        for tm_id, label in BERLIN_EVENTS.items():
            if tm_id in ev_url:
                source = ev.get("source", {}).get("name", "ticketmaster")
                results.append({
                    "tm_id": tm_id,
                    "label": label,
                    "url": ev_url,
                    "status": ev["dates"]["status"]["code"],
                    "source": source,
                    "sale_end": ev.get("sales", {}).get("public", {}).get("endDateTime"),
                })
    return results


def fetch_berlin_events() -> list[dict]:
    seen_ids = set()
    results = []

    # Primary + all sources for DE
    for source in ["ticketmaster,tmr,universe,frontgate", "tmr"]:
        params = {"apikey": TM_API_KEY, "keyword": "james blake", "source": source}
        if source != "tmr":
            params["countryCode"] = "DE"

        try:
            resp = requests.get(
                "https://app.ticketmaster.com/discovery/v2/events.json",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            evs = resp.json().get("_embedded", {}).get("events", [])
            for ev in _parse_events(evs):
                key = f"{ev['tm_id']}:{ev['source']}"
                if key not in seen_ids:
                    seen_ids.add(key)
                    results.append(ev)
        except Exception as e:
            print(f"  API query ({source}) failed: {e}")

    return results


def main():
    if not TM_API_KEY:
        print("Missing TICKETMASTER_API_KEY")
        sys.exit(1)

    print(f"[{datetime.now().isoformat()}] Checking James Blake Berlin tickets...")

    try:
        events = fetch_berlin_events()
    except Exception as e:
        print(f"API call failed: {e}")
        sys.exit(1)

    if not events:
        print("No Berlin events returned by API")
        sys.exit(0)

    state = load_state()
    notifications = []

    for ev in events:
        key = f"{ev['tm_id']}:{ev['source']}"
        status = ev["status"]
        available = status in AVAILABLE_STATUSES
        is_resale = ev["source"] == "tmr"
        tag = "RESALE 🔄" if is_resale else "PRIMARY 🎫"

        prev = state.get(key, {})
        prev_available = prev.get("available")
        prev_status = prev.get("status", "unknown")

        print(f"  [{ev['source']}] {ev['label']}: {status} (was: {prev_status}) → available={available}")

        if available and prev_available is not True:
            notifications.append(
                f"{tag} <b>{ev['label']}</b>\n"
                f"Status: <b>{status.upper()}</b>\n"
                f"🔗 {ev['url']}"
            )

        if prev_available is True and not available:
            notifications.append(
                f"❌ <b>{ev['label']}</b> [{ev['source']}] now <b>{status.upper()}</b>\n"
                f"Check: viagogo.com/de · stubhub.de · ticketswap.de"
            )

        state[key] = {
            "available": available,
            "status": status,
            "source": ev["source"],
            "sale_end": ev["sale_end"],
            "last_check": datetime.now().isoformat(),
        }

    save_state(state)

    if notifications:
        header = "🎵 <b>James Blake Berlin — Status Change!</b>\n\n"
        send_telegram(header + "\n\n".join(notifications))
        print("Telegram notification sent.")
    else:
        print("No change — no notification sent.")


main()
