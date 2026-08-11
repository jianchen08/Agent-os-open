#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动化迭代：打地鼠式修复 vite optimizeDeps include，直到 React 挂载成功并完成工具卡片验证"""
import json, os, re, subprocess, sys, time
from playwright.sync_api import sync_playwright

VITE_CONFIG = "/workspace/frontend/vite.config.ts"
OUT = "/workspace/docs/working/browser_tool_call_evidence"
CHROME = "/opt/ms-playwright/chromium-1234/chrome-linux64/chrome"
BASE = "http://127.0.0.1:5290"
USER, PWD, MSG = "admin", "admin12345", "用计算工具算一下 5+3"
os.makedirs(OUT, exist_ok=True)

def read_include():
    content = open(VITE_CONFIG, encoding="utf-8").read()
    m = re.search(r"include: \[(.*?)\]", content, re.S)
    if not m:
        return []
    return re.findall(r"'([^']+)'", m.group(1))

def write_include(items):
    content = open(VITE_CONFIG, encoding="utf-8").read()
    inner = "\n".join(f"        '{x}'," for x in items)
    new_block = "      include: [\n" + inner + "\n      ],"
    content = re.sub(r"      include: \[.*?\],", new_block, content, flags=re.S)
    open(VITE_CONFIG, "w", encoding="utf-8").write(content)

def restart_vite():
    # 停 vite
    subprocess.run("ps aux | grep -E 'node.*vite|npm exec' | grep -v grep | awk '{print $2}' | xargs -r kill -9",
                   shell=True, capture_output=True)
    time.sleep(2)
    subprocess.run("rm -rf /workspace/frontend/node_modules/.vite", shell=True, capture_output=True)
    subprocess.run("cd /workspace/frontend && nohup env NODE_OPTIONS='--max-old-space-size=800' "
                   "VITE_API_BASE_URL='http://localhost:9100' npx --yes vite --host 0.0.0.0 --port 5290 "
                   "> /tmp/frontend.log 2>&1 &", shell=True)
    # 等待就绪
    for _ in range(30):
        time.sleep(2)
        r = subprocess.run("curl -s -o /dev/null -w '%{http_code}' http://localhost:5290",
                           shell=True, capture_output=True, text=True)
        if r.stdout.strip() == "200":
            # 等预构建完成（.vite/deps 生成）
            for _ in range(20):
                time.sleep(2)
                deps = subprocess.run("ls /workspace/frontend/node_modules/.vite/deps/ 2>/dev/null | grep -cE '\\.js$'",
                                      shell=True, capture_output=True, text=True)
                if deps.stdout.strip() and int(deps.stdout.strip()) >= 5:
                    return True
                # 若 vite 被杀（OOM），提前返回 False
                alive = subprocess.run("pgrep -f 'vite --host' | head -1", shell=True, capture_output=True, text=True)
                if not alive.stdout.strip():
                    return False
            return True
    return False

def check_mount_and_verify():
    """打开页面，等待 React 挂载；若成功则登录发消息验证工具卡片。返回 (mounted, missing_modules, result)"""
    result = {"passed": False, "tool_card_found": False, "tool_name": "", "status": "",
              "result_text": "", "page_snapshot": "", "errors": [], "events_log": []}
    missing = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-crash-reporter"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(20000)
        console_msgs = []
        page.on("console", lambda m: console_msgs.append(f"{m.type}: {m.text[:150]}"))
        def on_pageerror(e):
            msg = str(e)
            result["errors"].append(f"pageerror: {msg[:300]}")
            # 提取缺失模块：'.../node_modules/<pkg>/...' does not provide an export named
            m = re.search(r"node_modules/([^/'\" ]+)", msg)
            if m:
                pkg = m.group(1)
                if pkg.startswith("@"):
                    m2 = re.search(r"node_modules/(@[^/'\" ]+/[^/'\" ]+)", msg)
                    if m2:
                        pkg = m2.group(1)
                missing.add(pkg)
        page.on("pageerror", on_pageerror)

        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            mounted = False
            for i in range(50):  # 100s
                try:
                    if page.locator('[data-testid="login-form"]').count() > 0 or \
                       page.locator('[data-testid="chat-input-textarea"]').count() > 0:
                        mounted = True
                        break
                except Exception:
                    pass
                page.wait_for_timeout(2000)

            if not mounted:
                page.screenshot(path=f"{OUT}/screenshot_no_mount.png")
                print(f"[round] React 未挂载, 缺失模块: {sorted(missing)}", flush=True)
                return False, sorted(missing), result

            # 登录
            login_form = page.locator('[data-testid="login-form"]')
            if login_form.count() > 0 and login_form.is_visible():
                page.locator('[data-testid="login-username-input"]').fill(USER)
                page.locator('[data-testid="login-password-input"]').fill(PWD)
                page.locator('[data-testid="login-submit-button"]').click()
                try:
                    page.locator('[data-testid="chat-input-textarea"]').wait_for(state="visible", timeout=45000)
                    print("[round] 登录成功", flush=True)
                except Exception as e:
                    result["errors"].append(f"登录后未出现聊天框: {str(e)[:150]}")
                    page.screenshot(path=f"{OUT}/screenshot_after_login.png")
                    return False, [], result

            # 发消息
            chat_input = page.locator('[data-testid="chat-input-textarea"]')
            if chat_input.count() == 0:
                result["errors"].append("未找到聊天输入框")
                return False, [], result
            chat_input.click()
            chat_input.fill(MSG)
            page.locator('[data-testid="chat-send-button"]').click()
            print(f"[round] 已发送: {MSG}", flush=True)

            # 轮询工具卡片
            tool_card = None
            for i in range(45):
                cards = page.locator('[data-activity-type="tool_call"]')
                cnt = cards.count()
                if cnt > 0:
                    tool_card = cards.last
                    st = tool_card.get_attribute("data-activity-status") or ""
                    print(f"[poll {i*2}s] 工具卡片 x{cnt} status={st!r}", flush=True)
                    if st == "completed":
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
                print(f"[result] tool_name={result['tool_name']!r} status={result['status']!r}", flush=True)
                print(f"[result] card_text 前200字: {card_text[:200]!r}", flush=True)
            else:
                result["errors"].append("等待 90s 未发现工具调用卡片")
                # 页面截图用于诊断
                page.screenshot(path=f"{OUT}/screenshot_no_card.png")
                print("[round] 未发现工具卡片", flush=True)

            page.screenshot(path=f"{OUT}/screenshot.png")
            try:
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
                result["errors"].append(f"保存 DOM 快照失败: {str(e)[:150]}")
        except Exception as e:
            import traceback
            result["errors"].append(f"执行异常: {e}\n{traceback.format_exc()}")
            try:
                page.screenshot(path=f"{OUT}/screenshot_error.png")
            except Exception:
                pass
        finally:
            result["events_log"] = console_msgs[-20:]
            try:
                browser.close()
            except Exception:
                pass
    return True, [], result

def main():
    items = read_include()
    print(f"[init] include 列表 {len(items)} 个: {items[:10]}...", flush=True)
    final_result = None
    for round_no in range(12):
        print(f"\n===== 第 {round_no+1} 轮 =====", flush=True)
        ok = restart_vite()
        if not ok:
            print("[round] vite 启动失败/预构建 OOM，等待后重试", flush=True)
            time.sleep(10)
            continue
        mounted, missing, result = check_mount_and_verify()
        if mounted:
            print(f"[round] React 挂载成功！passed={result['passed']}", flush=True)
            final_result = result
            with open(f"{OUT}/result.json", "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            return 0 if result["passed"] else 2
        if missing:
            added = []
            for m in missing:
                if m not in items:
                    items.append(m)
                    added.append(m)
            print(f"[round] 新增 include: {added}", flush=True)
            write_include(items)
        else:
            print("[round] 无缺失模块但未挂载，等待重试", flush=True)
            time.sleep(10)
    print("[main] 达到最大轮数仍未成功", flush=True)
    return 3

if __name__ == "__main__":
    sys.exit(main())
