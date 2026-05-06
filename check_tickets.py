import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

URL = "https://www.ticketmaster.de/artist/james-blake-tickets/765513"
STATE_FILE = Path("state.json")
BERLIN_IDS = {"953422232": "Berlin Oct 15", "352305340": "Berlin Oct 16"}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


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


def fetch_events() -> list[dict]:
    resp = requests.get(URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if not tag:
        raise RuntimeError("__NEXT_DATA__ not found — page structure changed")

    data = json.loads(tag.string)
    events = data["props"]["pageProps"]["initialReduxState"]["api"]

    results = []
    for ev in events:
        ev_id = ev.get("id", "")
        if ev_id not in BERLIN_IDS:
            continue
        results.append({
            "id": ev_id,
            "name": BERLIN_IDS[ev_id],
            "url": ev.get("url", ""),
            "soldOut": ev.get("soldOut", True),
            "limitedAvailability": ev.get("limitedAvailability", False),
            "cancelled": ev.get("cancelled", False),
            "available": not ev.get("soldOut", True) and not ev.get("cancelled", False),
        })

    return results


def main():
    print(f"[{datetime.now().isoformat()}] Checking James Blake Berlin tickets...")

    try:
        events = fetch_events()
    except Exception as e:
        print(f"Fetch failed: {e}")
        sys.exit(1)

    print(f"Berlin events found: {len(events)}")
    for ev in events:
        print(f"  {ev['name']}: soldOut={ev['soldOut']} limited={ev['limitedAvailability']} cancelled={ev['cancelled']}")

    state = load_state()
    changed = []

    for ev in events:
        key = ev["id"]
        prev = state.get(key, {}).get("available")
        curr = ev["available"]

        if curr and prev is not True:
            changed.append(ev)

        state[key] = {
            "available": curr,
            "soldOut": ev["soldOut"],
            "limitedAvailability": ev["limitedAvailability"],
            "last_check": datetime.now().isoformat(),
        }

    save_state(state)

    if changed:
        lines = ["🎵 <b>James Blake Berlin — Tickets Available!</b>\n"]
        for ev in changed:
            label = "LIMITED" if ev["limitedAvailability"] else "AVAILABLE"
            lines.append(f"📅 <b>{ev['name']}</b> — {label}")
            lines.append(f"🔗 {ev['url']}\n")
        send_telegram("\n".join(lines))
        print("Telegram notification sent.")
    else:
        print("No change — no notification sent.")


main()
