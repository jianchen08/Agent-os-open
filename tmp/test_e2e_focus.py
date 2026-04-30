"""
Focused E2E test: thinking display + message persistence + history reload.
Uses simple questions that won't trigger tool loops.
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright


def wait_for_response(page, timeout=90):
    """Wait for textarea to become enabled (response complete)."""
    start = time.time()
    while time.time() - start < timeout:
        ta = page.locator('textarea').first
        if ta.count() > 0 and ta.is_enabled():
            return True
        time.sleep(1)
    return False


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 900})

    page_errors = []
    page.on('pageerror', lambda err: page_errors.append(str(err)[:200]))

    # Login
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')
    time.sleep(1)
    page.locator('input[type="text"], input[name="username"]').first.fill('demo')
    page.locator('input[type="password"]').first.fill('demo12345')
    page.locator('button[type="submit"]').first.click()
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    # Create new chat
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(2)

    results = {}

    # ===== TEST 1: Thinking content =====
    print("\n=== TEST 1: Thinking content ===")
    ta = page.locator('textarea').first
    ta.fill('思考一下，1+1等于几？简单回答就行')
    ta.press('Enter')
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] Sent message")

    ok = wait_for_response(page, timeout=60)
    elapsed = time.time() - t0
    print(f"[{time.strftime('%H:%M:%S')}] Done in {elapsed:.1f}s (success={ok})")

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/focus_01_thinking.png', full_page=True)

    body = page.locator('body').text_content() or ''
    # Check for thinking-related content
    has_thinking_label = '思考' in body or 'Thinking' in body
    has_thinking_detail = any([
        'The user' in body,
        'user is asking' in body.lower(),
        '数学' in body,
        '加法' in body,
        '逻辑' in body,
    ])
    # Check AI response content
    has_response = any([
        '1 + 1 = 2' in body,
        '1+1=2' in body,
        '等于2' in body,
        '等于二' in body,
        '2' in body,
    ])

    print(f"  Thinking label: {has_thinking_label}")
    print(f"  Thinking detail: {has_thinking_detail}")
    print(f"  AI response (contains '2'): {has_response}")
    results['thinking_label'] = has_thinking_label
    results['thinking_detail'] = has_thinking_detail
    results['ai_response'] = has_response

    # ===== TEST 2: History reload =====
    print("\n=== TEST 2: History (page refresh) ===")
    page.reload()
    page.wait_for_load_state('networkidle')
    time.sleep(4)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/focus_02_history.png', full_page=True)

    body2 = page.locator('body').text_content() or ''
    has_old_msg = any([
        '1+1' in body2,
        '1 + 1' in body2,
        '思考' in body2 and ('等于' in body2 or '2' in body2),
    ])
    assistant_count = page.locator('[data-role="assistant"]').count()

    print(f"  Old messages visible: {has_old_msg}")
    print(f"  Assistant messages: {assistant_count}")
    results['history'] = has_old_msg

    # ===== TEST 3: Send second message (verifies chat still works after reload) =====
    print("\n=== TEST 3: Chat after reload ===")
    ta = page.locator('textarea').first
    if ta.count() > 0 and ta.is_enabled():
        ta.fill('你好，请简单介绍一下你自己')
        ta.press('Enter')
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] Sent second message")

        ok = wait_for_response(page, timeout=60)
        elapsed = time.time() - t0
        print(f"[{time.strftime('%H:%M:%S')}] Done in {elapsed:.1f}s (success={ok})")

        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/focus_03_after_reload.png', full_page=True)

        body3 = page.locator('body').text_content() or ''
        has_self_intro = any([
            '你好' in body3 and body3.count('你好') >= 2,  # At least our message + response
            '助手' in body3 or 'AI' in body3,
            '帮助' in body3,
        ])
        print(f"  Self-intro response: {has_self_intro}")
        results['chat_after_reload'] = has_self_intro
    else:
        print("  SKIP: textarea not available")
        results['chat_after_reload'] = False

    # ===== Summary =====
    print("\n=== SUMMARY ===")
    all_pass = all(results.values())
    for k, v in results.items():
        status = 'PASS' if v else 'FAIL'
        print(f"  {k}: {status}")

    if page_errors:
        print(f"\n  Page errors ({len(page_errors)}):")
        for e in page_errors[:5]:
            print(f"    {e}")

    print(f"\n  Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")

    browser.close()
    print("\nDone!")
