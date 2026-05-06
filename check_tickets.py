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
    resp = requests.post(
        url,
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


async def scrape_events() -> list[dict]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        await page.goto(ARTIST_URL, wait_until="domcontentloaded", timeout=40000)

        # Dismiss GDPR consent popup if present
        try:
            await page.wait_for_selector(
                'button[id*="accept"], button[class*="accept"], '
                'button:has-text("Accept"), button:has-text("Akzeptieren"), '
                'button:has-text("Accept All"), button:has-text("I Accept")',
                timeout=5000,
            )
            btn = await page.query_selector(
                'button[id*="accept"], button[class*="accept"], '
                'button:has-text("Accept"), button:has-text("Akzeptieren"), '
                'button:has-text("Accept All"), button:has-text("I Accept")'
            )
            if btn:
                await btn.click()
                print("Dismissed consent popup.")
        except Exception:
            pass

        # Wait for event listings to load
        try:
            await page.wait_for_selector('a[href*="/event/"]', timeout=20000)
        except Exception:
            print("Timeout waiting for event links — saving debug screenshot.")
            await page.screenshot(path="debug.png", full_page=True)
            return []

        await asyncio.sleep(2)

        events = await page.evaluate("""
            () => {
                const btns = Array.from(document.querySelectorAll('a[href*="/event/"]'));
                return btns.map(b => {
                    const text = b.innerText.trim();
                    const parent = b.closest('li') || b.closest('article') || b.parentElement?.parentElement?.parentElement;
                    let status = null;
                    if (parent) {
                        const badge = parent.querySelector('[class*="Badge__Label"]');
                        if (badge) status = badge.innerText.trim();
                        // Also check for sold out text anywhere in parent
                        if (!status && parent.innerText.includes('Sold Out')) status = 'SOLD OUT';
                    }
                    return { text, href: b.href || '', status };
                });
            }
        """)

        await browser.close()
        print(f"Raw events scraped: {len(events)}")
        for ev in events:
            if "Berlin" in ev.get("text", "") or "Berlin" in ev.get("href", ""):
                print(f"  Berlin hit: {ev}")
        return events


def parse_berlin_events(raw: list[dict]) -> list[dict]:
    results = []
    for ev in raw:
        text = ev.get("text", "")
        href = ev.get("href", "")
        if "Berlin" not in text and "Berlin" not in href:
            continue
        for date in BERLIN_DATES:
            if date not in text:
                continue
            status = ev.get("status") or ""
            if "Sold Out" in text or "sold out" in status.lower() or status == "SOLD OUT":
                available = False
                status_label = "SOLD OUT"
            elif href and ("/event/" in href):
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
    print(f"Berlin events parsed: {events}")

    if not events:
        print("No Berlin events found — page structure may have changed or events sold out/removed.")
        save_state(load_state())  # ensure state.json exists
        sys.exit(0)

    state = load_state()
    changed = []

    for ev in events:
        key = ev["date"]
        prev = state.get(key, {}).get("available")
        curr = ev["available"]
        print(f"  {key}: {ev['status']} (prev={prev} → now={curr})")

        if curr and prev is not True:
            changed.append(ev)

        state[key] = {
            "available": curr,
            "status": ev["status"],
            "last_check": datetime.now().isoformat(),
        }

    save_state(state)

    if changed:
        lines = ["🎵 <b>James Blake Berlin — Tickets Available!</b>\n"]
        for ev in changed:
            lines.append(f"📅 <b>{ev['date']}</b> — {ev['status']}")
            lines.append(f"🔗 {ev['url']}\n")
        msg = "\n".join(lines)
        send_telegram(msg)
        print("Telegram notification sent.")
    else:
        print("No change in availability — no notification sent.")


asyncio.run(main())
