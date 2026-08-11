#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键综合验证：依赖检查→服务启动→浏览器验证→证据收集，一次完成防环境重置打断"""
import json, os, re, subprocess, sys, time

WORKSPACE = "/workspace"
OUT = "/workspace/docs/working/browser_tool_call_evidence"
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
BASE = "http://127.0.0.1:5290"
USER, PWD, MSG = "admin", "admin12345", "用计算工具算一下 5+3"
os.makedirs(OUT, exist_ok=True)

def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return -1, str(e)

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# ── 1. 依赖检查与安装 ──
log("步骤1: 依赖检查")
import importlib
need_install = []
for mod in ["agentos_plugin_sdk", "websockets", "mcp", "pydantic_settings", "playwright"]:
    try:
        importlib.import_module(mod)
        log(f"  {mod} OK")
    except ImportError:
        need_install.append(mod)
if need_install:
    log(f"  需安装: {need_install}")
    sh("cd /workspace && pip install websockets mcp pydantic_settings", 180)
    sh("cd /workspace/plugins/sdk && pip install -e .", 180)
    log("  依赖安装完成")

# ── 2. 启动服务 ──
log("步骤2: 启动服务")
sh("chmod +x /opt/ms-playwright/chromium-1234/chrome-linux64/chrome_crashpad_handler")

# mock LLM
code, _ = sh("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/v1/chat/completions -X POST -H 'Content-Type: application/json' -d '{\"model\":\"mock\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'", 10)
if code != "200":
    sh("cd /workspace && nohup python3 docs/working/mock_llm_server.py > /tmp/mock_llm.log 2>&1 &", 5)
    time.sleep(2)
    log("  mock LLM 已启动")

# 内核
code, _ = sh("curl -s -o /dev/null -w '%{http_code}' http://localhost:9100/health", 10)
if code != "200":
    sh("cd /workspace && nohup env AGENTOS_KERNEL_PORT=9100 AGENTOS_KERNEL_HOST=0.0.0.0 ./kernel/target/release/agentos-kernel > /tmp/kernel.log 2>&1 &", 5)
    for _ in range(15):
        time.sleep(2)
        code, _ = sh("curl -s -o /dev/null -w '%{http_code}' http://localhost:9100/health", 5)
        if code == "200":
            break
    log(f"  内核启动状态: {code}")

# 前端 vite
code, _ = sh("curl -s -o /dev/null -w '%{http_code}' http://localhost:5290", 5)
if code != "200":
    # 若 .vite/deps 缓存缺失则先预构建一轮
    _, deps_count = sh("ls /workspace/frontend/node_modules/.vite/deps/ 2>/dev/null | grep -cE '\\.js$'", 5)
    if deps_count.strip() in ("", "0"):
        log("  .vite/deps 缓存缺失，启动 vite 预构建...")
        sh("cd /workspace/frontend && nohup env NODE_OPTIONS='--max-old-space-size=800' VITE_API_BASE_URL='http://localhost:9100' npx --yes vite --host 0.0.0.0 --port 5290 > /tmp/frontend.log 2>&1 &", 5)
        time.sleep(40)  # 等预构建
        # 预构建后重启释放内存
        sh("ps aux | grep -E 'node.*vite|npm exec' | grep -v grep | awk '{print $2}' | xargs -r kill -9", 5)
        time.sleep(3)
    sh("cd /workspace/frontend && nohup env NODE_OPTIONS='--max-old-space-size=800' VITE_API_BASE_URL='http://localhost:9100' npx --yes vite --host 0.0.0.0 --port 5290 > /tmp/frontend.log 2>&1 &", 5)
    time.sleep(12)
    code, _ = sh("curl -s -o /dev/null -w '%{http_code}' http://localhost:5290", 5)
    log(f"  前端启动状态: {code}")

# 最终服务确认
for name, check in [("内核", "http://localhost:9100/health"), ("前端", "http://localhost:5290")]:
    code, _ = sh(f"curl -s -o /dev/null -w '%{{http_code}}' {check}", 5)
    log(f"  {name}: {code}")
code, body = sh("curl -s http://127.0.0.1:18080/v1/chat/completions -X POST -H 'Content-Type: application/json' -d '{\"model\":\"mock\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'", 5)
log(f"  mock: {body[:40]}")

# ── 3. 浏览器验证 ──
log("步骤3: 浏览器验证")
from playwright.sync_api import sync_playwright

result = {"passed": False, "tool_card_found": False, "tool_name": "", "status": "",
          "result_text": "", "page_snapshot": "", "errors": [], "events_log": []}
console_msgs = []

with sync_playwright() as p:
    browser = p.chromium.launch(
        executable_path=CHROME, headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter",
              "--js-flags=--max-old-space-size=512", "--renderer-process-limit=1"])
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.set_default_timeout(15000)
    page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:150]}"))
    page.on("pageerror", lambda e: result["errors"].append(f"pageerror: {str(e)[:300]}"))

    log("[1] 打开前端（commit）...")
    try:
        page.goto(BASE, wait_until="commit", timeout=45000)
    except Exception as e:
        result["errors"].append(f"goto 失败: {str(e)[:200]}")
    page.wait_for_timeout(3000)
    log(f"[2] URL={page.url} title={page.title()!r}")

    mounted = False
    for i in range(75):  # 150s
        try:
            if page.locator('[data-testid="login-form"]').count() > 0 or \
               page.locator('[data-testid="chat-input-textarea"]').count() > 0:
                mounted = True
                log(f"[3] React 挂载成功 (第 {i*2}s)")
                break
        except Exception:
            pass
        if i % 5 == 0:
            log(f"[3] 等待 React 挂载... ({i*2}s) errors={len(result['errors'])}")
        try:
            page.wait_for_timeout(2000)
        except Exception as e:
            result["errors"].append(f"等待中断: {str(e)[:150]}")
            break
    if not mounted:
        result["errors"].append("等待 150s React 未挂载")
        try:
            page.screenshot(path=f"{OUT}/screenshot_no_mount.png")
        except Exception:
            pass
        log("[FAIL] React 未挂载")
    else:
        login_form = page.locator('[data-testid="login-form"]')
        if login_form.count() > 0 and login_form.is_visible():
            log("[4] 执行登录...")
            try:
                page.locator('[data-testid="login-username-input"]').fill(USER)
                page.locator('[data-testid="login-password-input"]').fill(PWD)
                page.locator('[data-testid="login-submit-button"]').click()
                page.locator('[data-testid="chat-input-textarea"]').wait_for(state="visible", timeout=45000)
                log("[5] 登录成功，聊天输入框可见")
            except Exception as e:
                result["errors"].append(f"登录失败: {str(e)[:200]}")
                try:
                    page.screenshot(path=f"{OUT}/screenshot_after_login.png")
                except Exception:
                    pass
        else:
            log("[4] 已登录状态")

        chat_input = page.locator('[data-testid="chat-input-textarea"]')
        if chat_input.count() == 0:
            result["errors"].append("未找到聊天输入框")
        else:
            try:
                chat_input.click()
                chat_input.fill(MSG)
                page.locator('[data-testid="chat-send-button"]').click()
                log(f"[6] 已发送: {MSG}")
            except Exception as e:
                result["errors"].append(f"发送消息失败: {str(e)[:200]}")

            tool_card = None
            for i in range(45):
                try:
                    cards = page.locator('[data-activity-type="tool_call"]')
                    cnt = cards.count()
                    if cnt > 0:
                        tool_card = cards.last
                        st = tool_card.get_attribute("data-activity-status") or ""
                        log(f"[poll {i*2}s] 工具卡片 x{cnt} status={st!r}")
                        if st == "completed":
                            break
                except Exception as e:
                    result["errors"].append(f"轮询异常: {str(e)[:150]}")
                    break
                page.wait_for_timeout(2000)
            if tool_card is not None:
                result["tool_card_found"] = True
                result["status"] = tool_card.get_attribute("data-activity-status") or ""
                try:
                    result["tool_name"] = (tool_card.locator(".font-medium").first.text_content() or "").strip()
                except Exception:
                    pass
                try:
                    card_text = (tool_card.text_content() or "").strip()
                except Exception:
                    card_text = ""
                result["result_text"] = card_text[:2000]
                try:
                    html = tool_card.evaluate("(el) => el.outerHTML")
                    with open(f"{OUT}/tool_card.html", "w", encoding="utf-8") as f:
                        f.write(html)
                except Exception:
                    pass
                if result["status"] == "completed" and ("8" in card_text or "5+3" in card_text):
                    result["passed"] = True
                log(f"[result] tool_name={result['tool_name']!r} status={result['status']!r}")
                log(f"[result] card_text 前300字: {card_text[:300]!r}")
            else:
                result["errors"].append("等待 90s 未发现工具调用卡片")
                try:
                    page.screenshot(path=f"{OUT}/screenshot_no_card.png")
                except Exception:
                    pass
                log("[FAIL] 未发现工具调用卡片")

            try:
                page.screenshot(path=f"{OUT}/screenshot.png")
                html = page.content()
                with open(f"{OUT}/page.html", "w", encoding="utf-8") as f:
                    f.write(html)
                snippets = []
                for m in re.finditer(r'data-activity-type="tool_call"', html):
                    start = max(0, m.start() - 200)
                    end = min(len(html), m.end() + 1500)
                    snippets.append(html[start:end])
                result["page_snapshot"] = snippets[0] if snippets else "page.html 中未找到 tool_call 标记"
            except Exception as e:
                result["errors"].append(f"保存快照失败: {str(e)[:150]}")

    result["events_log"] = console_msgs[-25:]
    try:
        browser.close()
    except Exception:
        pass

with open(f"{OUT}/result.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
log("=" * 60)
log(json.dumps(result, ensure_ascii=False, indent=2)[:3000])
log("=" * 60)
sys.exit(0 if result["passed"] else 1)
