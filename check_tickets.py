import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

ARTIST_URL = "https://www.ticketmaster.de/artist/james-blake-tickets/765513"
STATE_FILE = Path("state.json")

BERLIN_DATES = ["15/10/2026", "16/10/2026"]

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[NOTIFY] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}, timeout=10)
    if not resp.ok:
        print(f"Telegram error: {resp.text}")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


async def scrape_events() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(locale="en-US")
        page = await ctx.new_page()
        await page.goto(ARTIST_URL, wait_until="networkidle", timeout=40000)

        events = await page.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('a, button'))
                    .filter(el => el.innerText && el.innerText.toLowerCase().includes('ticket'));

                return btns.map(b => {
                    const text = b.innerText.trim();
                    // Find nearest status badge in parent
                    let status = null;
                    const parent = b.closest('[class*="sc-"]') || b.parentElement?.parentElement;
                    if (parent) {
                        const badge = parent.querySelector('[class*="Badge__Label"]');
                        if (badge) status = badge.innerText.trim();
                    }
                    return { text, href: b.href || '', status };
                });
            }
        """)

        await browser.close()
        return events


def parse_berlin_events(raw: list[dict]) -> list[dict]:
    results = []
    for ev in raw:
        text = ev.get("text", "")
        if "Berlin" not in text:
            continue
        for date in BERLIN_DATES:
            if date not in text:
                continue
            # Determine availability
            status = ev.get("status") or ""
            href = ev.get("href", "")
            if "Sold Out" in text or "sold out" in status.lower():
                available = False
                status_label = "SOLD OUT"
            elif "Find tickets" in text or "tickets" in text.lower():
                available = True
                status_label = status if status else "AVAILABLE"
            else:
                available = False
                status_label = "UNKNOWN"

            results.append({
                "date": date,
                "url": href,
                "available": available,
                "status": status_label,
            })
    return results


async def main():
    print(f"[{datetime.now().isoformat()}] Checking James Blake Berlin tickets...")

    try:
        raw = await scrape_events()
    except Exception as e:
        print(f"Scrape failed: {e}")
        sys.exit(1)

    events = parse_berlin_events(raw)

    if not events:
        print("No Berlin events found on page — structure may have changed.")
        sys.exit(0)

    state = load_state()
    changed = []

    for ev in events:
        key = ev["date"]
        prev = state.get(key, {}).get("available")
        curr = ev["available"]

        print(f"  {key}: {ev['status']} (was: {prev})")

        # Alert when:  not previously available (or unknown) → now available
        if curr and prev is not True:
            changed.append(ev)

        state[key] = {"available": curr, "status": ev["status"], "last_check": datetime.now().isoformat()}

    save_state(state)

    if changed:
        lines = ["🎵 <b>James Blake Berlin — Tickets Available!</b>\n"]
        for ev in changed:
            lines.append(f"📅 <b>{ev['date']}</b> — {ev['status']}")
            lines.append(f"🔗 {ev['url']}\n")
        send_telegram("\n".join(lines))
        print("Notification sent.")
    else:
        print("No state change — no notification sent.")


asyncio.run(main())
