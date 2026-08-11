import json, time, sys, os
from playwright.sync_api import sync_playwright
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
OUT = "/workspace/docs/working/browser_tool_call_evidence"
os.makedirs(OUT, exist_ok=True)
result = {"passed": False, "tool_card_found": False, "tool_name": "", "status": "",
          "result_text": "", "page_snapshot": "", "errors": [], "events_log": []}
print("probe chromium...", flush=True)
try:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter",
                  "--js-flags=--max-old-space-size=384", "--renderer-process-limit=1"])
        print("chromium launched", flush=True)
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        pg.set_default_timeout(12000)
        console_msgs = []
        pg.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:120]}"))
        pg.on("pageerror", lambda e: result["errors"].append(f"pageerror: {str(e)[:300]}"))
        print("goto frontend...", flush=True)
        try:
            pg.goto("http://127.0.0.1:5290", wait_until="commit", timeout=25000)
        except Exception as e:
            result["errors"].append(f"goto: {str(e)[:150]}")
        # 轮询 60s 等 React 挂载
        mounted = False
        for i in range(12):
            time.sleep(5)
            try:
                lf = pg.locator('[data-testid="login-form"]').count()
                ci = pg.locator('[data-testid="chat-input-textarea"]').count()
                print(f"t={i*5}s login_form={lf} chat_input={ci} errors={len(result['errors'])}", flush=True)
                if lf > 0 or ci > 0:
                    mounted = True
                    print("REACT_MOUNTED", flush=True)
                    break
            except Exception as e:
                result["errors"].append(f"poll: {str(e)[:100]}")
                break
        if not mounted:
            result["errors"].append("60s 内 React 未挂载")
        try:
            pg.screenshot(path=f"{OUT}/final_screenshot.png")
            html = pg.content()
            with open(f"{OUT}/page.html", "w", encoding="utf-8") as f:
                f.write(html)
            body = pg.locator("body").text_content(timeout=5000) if pg.locator("body").count() else ""
            result["page_snapshot"] = (body or "")[:500]
        except Exception as e:
            result["errors"].append(f"snapshot: {str(e)[:100]}")
        result["events_log"] = console_msgs[-15:]
        b.close()
except Exception as e:
    result["errors"].append(f"launch fail: {type(e).__name__}: {str(e)[:200]}")
    print("chromium launch FAILED", flush=True)
with open(f"{OUT}/result.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print("RESULT:", json.dumps(result, ensure_ascii=False)[:1200], flush=True)
