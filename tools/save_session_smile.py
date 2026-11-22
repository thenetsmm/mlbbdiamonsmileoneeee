# Run on your laptop, while the debug Chrome (port 9222) is logged in to smile.one.
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]
    ctx.storage_state(path="smileone_session.json")
    print("session saved: smileone_session.json")
    browser.close()
