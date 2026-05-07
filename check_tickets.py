import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright

# ── Config ─────────────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")
TM_API_KEY         = os.environ.get("TICKETMASTER_API_KEY")
NORDVPN_USER       = os.environ.get("NORDVPN_USER")
NORDVPN_PASS       = os.environ.get("NORDVPN_PASS")

STATE_FILE = Path("state.json")

BERLIN_EVENTS = [
    {
        "tm_id":    "953422232",
        "disco_id": "Z698xZC2Z1kPK8xFw",
        "label":    "Berlin Oct 15 – Astra Kulturhaus",
        "url":      "https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/953422232",
    },
    {
        "tm_id":    "352305340",
        "disco_id": "Z698xZC2Z1ku3GAuZ",
        "label":    "Berlin Oct 16 – Astra Kulturhaus",
        "url":      "https://www.ticketmaster.de/event/james-blake-trying-times-tour-tickets/352305340",
    },
]

NORDVPN_PROXY = "socks5://de.socks-proxy.nordvpn.com:1080"

STEALTH_JS = """() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
    Object.defineProperty(navigator, 'languages', { get: () => ['de-DE','de','en-US','en'] });
    window.chrome = { runtime: {} };
    const orig = window.navigator.permissions.query;
    window.navigator.permissions.query = p =>
        p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : orig(p);
}"""


# ── Helpers ────────────────────────────────────────────────────────────────────

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
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Tier 1: Discovery API (fast, no proxy) ─────────────────────────────────────

def discovery_check() -> dict:
    """Returns {tm_id: status_code}"""
    out = {}
    for params in [
        {"apikey": TM_API_KEY, "keyword": "james blake", "countryCode": "DE"},
        {"apikey": TM_API_KEY, "keyword": "james blake", "source": "tmr"},
    ]:
        try:
            r = requests.get(
                "https://app.ticketmaster.com/discovery/v2/events.json",
                params=params, timeout=15,
            )
            r.raise_for_status()
            for ev in r.json().get("_embedded", {}).get("events", []):
                url = ev.get("url", "")
                for be in BERLIN_EVENTS:
                    if be["tm_id"] in url:
                        key = f"{be['tm_id']}:{params.get('source','ticketmaster')}"
                        out[key] = ev["dates"]["status"]["code"]
        except Exception as e:
            print(f"  Discovery API error: {e}")
    return out


# ── Tier 2: Playwright + NordVPN (real soldOut + resale) ──────────────────────

async def browser_check(event: dict, page) -> dict:
    await page.goto(event["url"], wait_until="domcontentloaded", timeout=40000)

    # Wait for Next.js to hydrate eventInfo
    try:
        await page.wait_for_function(
            """() => {
                try {
                    const s = JSON.parse(document.getElementById('__NEXT_DATA__').textContent)
                        .props.pageProps.initialReduxState;
                    return s.eventInfo && s.eventInfo.stage === 'success';
                } catch { return false; }
            }""",
            timeout=20000,
        )
    except Exception:
        print(f"    Hydration timeout — reading whatever is available")

    await asyncio.sleep(3)  # let resale XHR complete

    return await page.evaluate("""() => {
        try {
            const s = JSON.parse(document.getElementById('__NEXT_DATA__').textContent)
                .props.pageProps.initialReduxState;

            const info    = s.eventInfo   || {};
            const resale  = s.resale      || {};
            const emb     = s.embeddedResale || {};

            const res     = emb.results  || {};
            const listings = res.listings || res.items || [];
            const resaleCount = Array.isArray(listings)
                ? listings.length
                : (res.totalListings || res.total || 0);

            return {
                ok:            true,
                soldOut:       info.soldOut,
                limited:       info.limitedAvailability,
                resaleEnabled: resale.resaleEnabled,
                resaleCount:   resaleCount,
                resaleStage:   emb.stage,
            };
        } catch(e) {
            return { ok: false, error: String(e) };
        }
    }""")


async def browser_check_all() -> dict:
    results = {}

    proxy_cfg = None
    if NORDVPN_USER and NORDVPN_PASS:
        proxy_cfg = {
            "server":   NORDVPN_PROXY,
            "username": NORDVPN_USER,
            "password": NORDVPN_PASS,
        }
        print(f"  Using NordVPN proxy: {NORDVPN_PROXY}")
    else:
        print("  No proxy — using direct connection")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy_cfg,
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="de-DE",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()
        await page.add_init_script(STEALTH_JS)

        for ev in BERLIN_EVENTS:
            print(f"  Loading: {ev['label']} ...")
            try:
                data = await browser_check(ev, page)
                results[ev["tm_id"]] = data
                print(f"    soldOut={data.get('soldOut')} "
                      f"limited={data.get('limited')} "
                      f"resaleCount={data.get('resaleCount')} "
                      f"resaleEnabled={data.get('resaleEnabled')}")
            except Exception as e:
                print(f"    Failed: {e}")
                results[ev["tm_id"]] = {"ok": False, "error": str(e)}

        await browser.close()
    return results


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    print(f"[{datetime.now().isoformat()}] Checking James Blake Berlin tickets...")

    # Tier 1: Discovery API
    api_statuses = discovery_check()
    print(f"Discovery API: {api_statuses}")

    # Tier 2: Browser check via NordVPN
    print("Running browser check...")
    browser_data = await browser_check_all()

    state      = load_state()
    alerts     = []

    for ev in BERLIN_EVENTS:
        tm_id = ev["tm_id"]
        label = ev["label"]
        url   = ev["url"]
        bdata = browser_data.get(tm_id, {})

        if not bdata.get("ok"):
            print(f"  {label}: browser check failed — falling back to Discovery API")
            # Fallback: use Discovery API status
            api_st    = api_statuses.get(f"{tm_id}:ticketmaster", "unknown")
            sold_out  = api_st not in ("onsale", "rescheduled")
            resale_count = 0
        else:
            sold_out     = bdata.get("soldOut", True)
            resale_count = bdata.get("resaleCount", 0)

        primary_available = not sold_out
        resale_available  = resale_count > 0

        prev              = state.get(tm_id, {})
        prev_primary      = prev.get("primary_available")
        prev_resale       = prev.get("resale_available", False)

        print(f"  {label}: primary={primary_available}(was {prev_primary}) "
              f"resale={resale_available}/{resale_count}(was {prev_resale})")

        if prev_primary is None:
            print("    First run — recording state, no alert")
        else:
            if primary_available and not prev_primary:
                alerts.append(
                    f"🎫 <b>PRIMARY AVAILABLE</b>\n"
                    f"📅 {label}\n"
                    f"🔗 {url}"
                )
            if prev_primary and not primary_available:
                alerts.append(
                    f"❌ <b>PRIMARY SOLD OUT</b>\n"
                    f"📅 {label}\n"
                    f"Check resale: viagogo.com/de · stubhub.de · ticketswap.de"
                )
            if resale_available and not prev_resale:
                alerts.append(
                    f"🔄 <b>RESALE AVAILABLE</b> ({resale_count} listing{'s' if resale_count>1 else ''})\n"
                    f"📅 {label}\n"
                    f"🔗 {url}"
                )
            if prev_resale and not resale_available:
                alerts.append(
                    f"⚠️ <b>RESALE GONE</b>\n"
                    f"📅 {label}"
                )

        state[tm_id] = {
            "primary_available": primary_available,
            "resale_available":  resale_available,
            "resale_count":      resale_count,
            "sold_out":          sold_out,
            "last_check":        datetime.now().isoformat(),
        }

    save_state(state)

    if alerts:
        header = "🎵 <b>James Blake Berlin — Update!</b>\n\n"
        send_telegram(header + "\n\n".join(alerts))
        print("Telegram notification sent.")
    else:
        print("No change — no notification sent.")


asyncio.run(main())
