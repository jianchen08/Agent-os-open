import json, time
from playwright.sync_api import sync_playwright

CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
errors = []
console_msgs = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=CHROME, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter",
              "--js-flags=--max-old-space-size=512", "--renderer-process-limit=1"])
    pg = b.new_page(viewport={"width": 1280, "height": 800})
    pg.on("pageerror", lambda e: errors.append(str(e)[:400]))
    pg.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:200]}"))
    try:
        pg.goto("http://127.0.0.1:5290", wait_until="commit", timeout=30000)
    except Exception as e:
        errors.append(f"goto: {str(e)[:150]}")
    for i in range(10):
        time.sleep(5)
        lf = pg.locator('[data-testid="login-form"]').count()
        ci = pg.locator('[data-testid="chat-input-textarea"]').count()
        print(f"t={i*5}s login_form={lf} chat_input={ci} errors={len(errors)}", flush=True)
        if lf > 0 or ci > 0:
            print("REACT_MOUNTED", flush=True)
            break
    print("PAGEERRORS:", json.dumps(errors, ensure_ascii=False, indent=1), flush=True)
    print("CONSOLE_LAST:", json.dumps(console_msgs[-10:], ensure_ascii=False, indent=1), flush=True)
    try:
        pg.screenshot(path="/workspace/docs/working/browser_tool_call_evidence/diag_min.png")
    except Exception:
        pass
    b.close()
