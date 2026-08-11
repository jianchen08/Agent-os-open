import time, sys
from playwright.sync_api import sync_playwright
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
print("start", flush=True)
try:
    with sync_playwright() as p:
        print("launching...", flush=True)
        b = p.chromium.launch(executable_path=CHROME, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter"])
        print("launched", flush=True)
        pg = b.new_page()
        print("page created", flush=True)
        pg.goto("about:blank", timeout=15000)
        print("goto about:blank ok", flush=True)
        b.close()
        print("chromium OK", flush=True)
except Exception as e:
    print(f"FAIL: {type(e).__name__}: {str(e)[:200]}", flush=True)
    sys.exit(1)
