import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

TM_API_KEY = os.environ.get("TICKETMASTER_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = Path("state.json")

BERLIN_EVENTS = [
    {
        "id": "Z698xZC2Z1kPK8xFw",   # Discovery API id
        "tm_id": "953422232",          # URL / event page id
        "label": "Berlin Oct 15 (Astra Kulturhaus)",
        "url": "https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/953422232",
    },
    {
        "id": "Z698xZC2Z1ku3GAuZ",
        "tm_id": "352305340",
        "label": "Berlin Oct 16 (Astra Kulturhaus)",
        "url": "https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/352305340",
    },
]


# ── Notifications ──────────────────────────────────────────────────────────────

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


# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Tier 1: Discovery API (fast pre-check) ─────────────────────────────────────

def discovery_api_check() -> dict:
    """Returns {tm_id: status_code} e.g. {"953422232": "onsale"}"""
    resp = requests.get(
        "https://app.ticketmaster.com/discovery/v2/events.json",
        params={"apikey": TM_API_KEY, "keyword": "james blake", "countryCode": "DE"},
        timeout=15,
    )
    resp.raise_for_status()
    events = resp.json().get("_embedded", {}).get("events", [])
    result = {}
    for ev in events:
        url = ev.get("url", "")
        for be in BERLIN_EVENTS:
            if be["tm_id"] in url:
                result[be["tm_id"]] = ev["dates"]["status"]["code"]
    return result


# ── Stealth patches (replaces playwright-stealth library) ─────────────────────

STEALTH_JS = """
() => {
    // Remove webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Fake plugins
    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // Fake languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    // Override chrome object
    window.chrome = { runtime: {} };

    // Pass permissions query
    const origQuery = window.navigator.permissions.query;
    window.navigator.permissions.query = (parameters) =>
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(parameters);
}
"""


async def apply_stealth(page):
    await page.add_init_script(STEALTH_JS)


# ── Tier 2: Playwright + stealth (full browser, real availability) ─────────────

async def browser_check_event(page, event: dict) -> dict:
    """Load event page with stealth and extract soldOut + resale data."""
    await page.goto(event["url"], wait_until="domcontentloaded", timeout=40000)

    # Wait for Next.js hydration and resale to load
    await page.wait_for_function(
        """() => {
            try {
                const d = JSON.parse(document.getElementById('__NEXT_DATA__').textContent);
                const s = d.props.pageProps.initialReduxState;
                return s.eventInfo && s.eventInfo.stage === 'success';
            } catch(e) { return false; }
        }""",
        timeout=20000,
    )
    await asyncio.sleep(3)  # allow resale XHR to complete

    return await page.evaluate("""
        () => {
            const state = JSON.parse(document.getElementById('__NEXT_DATA__').textContent)
                .props.pageProps.initialReduxState;
            const info = state.eventInfo || {};
            const resale = state.resale || {};
            const embedded = state.embeddedResale || {};

            // Count resale listings if present
            const results = embedded.results || {};
            const listings = results.listings || results.items || [];
            const resaleCount = Array.isArray(listings) ? listings.length
                : (results.totalListings || results.total || 0);

            return {
                soldOut: info.soldOut,
                limitedAvailability: info.limitedAvailability,
                resaleEnabled: resale.resaleEnabled,
                resaleCount: resaleCount,
                resaleResultsRaw: Object.keys(results),
            };
        }
    """)


ARTIST_URL = "https://www.ticketmaster.de/artist/james-blake-tickets/765513"


async def browser_check_all() -> dict:
    """Load artist page (less guarded than event pages) and extract soldOut per event."""
    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        await apply_stealth(page)

        print("  Loading artist page...")
        try:
            await page.goto(ARTIST_URL, wait_until="networkidle", timeout=40000)

            events_data = await page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    if (!el) return null;
                    const state = JSON.parse(el.textContent).props.pageProps.initialReduxState;
                    const api = state.api || [];
                    return api.map(ev => ({
                        id: ev.id,
                        soldOut: ev.soldOut,
                        limitedAvailability: ev.limitedAvailability,
                        cancelled: ev.cancelled,
                        url: ev.url,
                    }));
                }
            """)

            if events_data:
                print(f"  Artist page returned {len(events_data)} events")
                for ev in events_data:
                    for be in BERLIN_EVENTS:
                        if ev["id"] == be["tm_id"]:
                            results[be["tm_id"]] = {
                                "soldOut": ev["soldOut"],
                                "limitedAvailability": ev["limitedAvailability"],
                                "resaleEnabled": None,
                                "resaleCount": 0,
                                "resaleResultsRaw": [],
                            }
                            print(f"    {be['label']}: soldOut={ev['soldOut']} limited={ev['limitedAvailability']}")
            else:
                print("  Artist page: no __NEXT_DATA__ (bot-blocked or structure changed)")

        except Exception as e:
            print(f"  Artist page browser check failed: {e}")

        await browser.close()
    return results


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    print(f"[{datetime.now().isoformat()}] Checking James Blake Berlin tickets...")

    # Tier 1: Discovery API
    try:
        api_statuses = discovery_api_check()
        print(f"Discovery API: {api_statuses}")
    except Exception as e:
        print(f"Discovery API failed: {e}")
        api_statuses = {}

    # Tier 2: Browser check for real soldOut + resale data
    print("Running browser check...")
    browser_data = await browser_check_all()

    state = load_state()
    notifications = []

    for ev in BERLIN_EVENTS:
        tm_id = ev["tm_id"]
        label = ev["label"]
        url = ev["url"]

        api_status = api_statuses.get(tm_id, "unknown")
        bdata = browser_data.get(tm_id) or {}

        sold_out = bdata.get("soldOut", None)
        resale_count = bdata.get("resaleCount", 0)
        limited = bdata.get("limitedAvailability", False)

        # Primary available = not soldOut (use browser data; fall back to API)
        if sold_out is not None:
            primary_available = not sold_out
        else:
            primary_available = api_status == "onsale"

        resale_available = resale_count > 0

        prev = state.get(tm_id, {})
        prev_primary = prev.get("primary_available")
        prev_resale = prev.get("resale_available", False)

        print(f"  {label}: primary={primary_available}(was {prev_primary}) resale={resale_available}({resale_count} listings)")

        # Notify on transitions
        if primary_available and prev_primary is not True:
            tag = "LIMITED" if limited else "AVAILABLE"
            notifications.append(f"🎫 <b>PRIMARY</b> — {label}\nStatus: {tag}\n🔗 {url}")

        if resale_available and not prev_resale:
            notifications.append(f"🔄 <b>RESALE</b> — {label}\n{resale_count} listing(s)\n🔗 {url}")

        state[tm_id] = {
            "primary_available": primary_available,
            "resale_available": resale_available,
            "resale_count": resale_count,
            "api_status": api_status,
            "sold_out": sold_out,
            "last_check": datetime.now().isoformat(),
        }

    save_state(state)

    if notifications:
        header = "🎵 <b>James Blake Berlin — Tickets Available!</b>\n\n"
        send_telegram(header + "\n\n".join(notifications))
        print("Telegram notification sent.")
    else:
        print("No change — no notification sent.")


asyncio.run(main())
