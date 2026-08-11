import json, time, os, sys
from playwright.sync_api import sync_playwright
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
OUT = "/workspace/docs/working/browser_tool_call_evidence"
os.makedirs(OUT, exist_ok=True)
res = {"passed": False, "tool_card_found": False, "tool_name": "", "status": "",
       "result_text": "", "page_snapshot": "", "errors": [], "events_log": [],
       "console_last": []}
console_msgs = []
try:
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter",
                  "--js-flags=--max-old-space-size=384", "--renderer-process-limit=1"])
        print("chromium launched", flush=True)
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        pg.set_default_timeout(10000)
        pg.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:150]}"))
        pg.on("pageerror", lambda e: res["errors"].append(f"pageerror: {str(e)[:300]}"))
        print("goto...", flush=True)
        try:
            pg.goto("http://127.0.0.1:5290", wait_until="commit", timeout=25000)
            print("goto done", flush=True)
        except Exception as e:
            res["errors"].append(f"goto: {str(e)[:150]}")
        mounted = False
        for i in range(12):  # 60s
            time.sleep(5)
            try:
                lf = pg.locator('[data-testid="login-form"]').count()
                ci = pg.locator('[data-testid="chat-input-textarea"]').count()
                print(f"t={i*5}s login_form={lf} chat_input={ci} errs={len(res['errors'])}", flush=True)
                if lf > 0 or ci > 0:
                    mounted = True
                    print("MOUNTED", flush=True)
                    break
            except Exception as e:
                res["errors"].append(f"poll: {str(e)[:100]}")
                break
        if mounted:
            # 登录
            try:
                pg.locator('[data-testid="login-username-input"]').fill("admin")
                pg.locator('[data-testid="login-password-input"]').fill("admin12345")
                pg.locator('[data-testid="login-submit-button"]').click()
                pg.locator('[data-testid="chat-input-textarea"]').wait_for(state="visible", timeout=30000)
                print("LOGIN_OK", flush=True)
                # 发消息
                pg.locator('[data-testid="chat-input-textarea"]').click()
                pg.locator('[data-testid="chat-input-textarea"]').fill("用计算工具算一下 5+3")
                pg.locator('[data-testid="chat-send-button"]').click()
                print("SENT", flush=True)
                # 轮询工具卡片 60s
                for i in range(12):
                    time.sleep(5)
                    try:
                        cards = pg.locator('[data-activity-type="tool_call"]')
                        cnt = cards.count()
                        if cnt > 0:
                            tc = cards.last
                            st = tc.get_attribute("data-activity-status") or ""
                            print(f"card t={i*5}s x{cnt} status={st!r}", flush=True)
                            res["tool_card_found"] = True
                            res["status"] = st
                            try:
                                res["tool_name"] = (tc.locator(".font-medium").first.text_content() or "").strip()
                            except Exception:
                                pass
                            try:
                                res["result_text"] = (tc.text_content() or "")[:1500]
                            except Exception:
                                pass
                            if st == "completed" and "8" in res["result_text"]:
                                res["passed"] = True
                            break
                    except Exception as e:
                        res["errors"].append(f"card poll: {str(e)[:100]}")
                        break
            except Exception as e:
                res["errors"].append(f"login/msg: {str(e)[:200]}")
        else:
            res["errors"].append("60s React 未挂载")
        try:
            pg.screenshot(path=f"{OUT}/final2.png")
            html = pg.content()
            with open(f"{OUT}/page.html", "w", encoding="utf-8") as f:
                f.write(html)
            import re
            snips = [html[max(0,m.start()-150):m.end()+1000] for m in re.finditer(r'data-activity-type="tool_call"', html)]
            res["page_snapshot"] = snips[0][:1200] if snips else "no tool_call marker"
        except Exception as e:
            res["errors"].append(f"snap: {str(e)[:100]}")
        res["console_last"] = console_msgs[-15:]
        b.close()
except Exception as e:
    res["errors"].append(f"launch: {type(e).__name__}: {str(e)[:200]}")
with open(f"{OUT}/result.json", "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print("RESULT:", json.dumps(res, ensure_ascii=False)[:1500], flush=True)
