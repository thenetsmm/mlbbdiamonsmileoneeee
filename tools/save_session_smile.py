# Run on your laptop, while the debug Chrome (port 9222) is logged in to smile.one.
# Saves ONLY smile.one cookies (no Google cookies) plus Chrome's user agent.
import json
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    ua = page.evaluate("navigator.userAgent")
    state = ctx.storage_state()
    state["cookies"] = [c for c in state["cookies"]
                        if "smile.one" in (c.get("domain") or "")]
    state["origins"] = []
    state["user_agent"] = ua
    json.dump(state, open("smileone_session.json", "w", encoding="utf-8"))
    print("saved", len(state["cookies"]), "smile.one cookies")
    print("UA:", ua)
    browser.close()
