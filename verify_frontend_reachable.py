"""
失败测试：登录页应在限定时间内渲染出登录表单（当前根因：首屏模块图爆炸导致白屏/超时）
断言：访问 /login 后 30s 内 DOMContentLoaded 触发，且登录表单（>=2 个 input + 登录按钮）可见
当前预期：失败（240s 才触发 DOMContentLoaded / 超时）
"""
import json, sys
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:5290'
RESULT = {'passed': False, 'dom_loaded_sec': None, 'form_visible': False, 'errors': []}

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    pageerrors = []
    page.on('pageerror', lambda e: pageerrors.append(str(e)[:300]))

    # 用 commit 快速拿到 HTML，然后等待 DOMContentLoaded（限时 30s）
    page.goto(BASE + '/login', timeout=15000, wait_until='commit')
    try:
        page.wait_for_function("document.readyState === 'complete'", timeout=30000)
        RESULT['dom_loaded_sec'] = 30
        RESULT['passed'] = True
    except Exception:
        state = page.evaluate("document.readyState")
        RESULT['dom_loaded_sec'] = None
        RESULT['errors'].append(f"30s 内未触发 DOMContentLoaded，当前 readyState={state}")

    # 检查登录表单
    inputs = page.locator('input')
    input_count = inputs.count()
    login_btn = page.locator('button').filter(has_text='登录').first
    try:
        btn_visible = login_btn.is_visible(timeout=5000)
    except Exception:
        btn_visible = False
    RESULT['form_visible'] = input_count >= 2 and btn_visible
    if not RESULT['form_visible']:
        RESULT['errors'].append(f"登录表单不可见: inputs={input_count} 登录按钮可见={btn_visible}")

    RESULT['errors'].extend(pageerrors[:5])
    try:
        body_text = page.inner_text('body')[:200]
        RESULT['body_text'] = body_text
    except Exception:
        RESULT['body_text'] = ''
    browser.close()

print(json.dumps(RESULT, ensure_ascii=False, indent=2))
sys.exit(0 if RESULT['passed'] and RESULT['form_visible'] else 1)
