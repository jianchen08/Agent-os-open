"""
E2E test v5: Uses keyboard.type() for reliable message sending.
Tests: thinking display, AI response, history persistence, post-reload chat.
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright


def send_message(page, text):
    """Send a chat message reliably using keyboard input."""
    ta = page.locator('textarea').first
    ta.click()
    time.sleep(0.3)
    page.keyboard.type(text, delay=30)
    time.sleep(0.3)
    page.keyboard.press('Enter')


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

    # ===== TEST 1: Thinking content + AI response =====
    print("\n=== TEST 1: Thinking content + AI response ===")
    send_message(page, '思考一下，1+1等于几？简单回答')
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] Message sent")

    ok = wait_for_response(page, timeout=60)
    elapsed = time.time() - t0
    print(f"[{time.strftime('%H:%M:%S')}] Response complete: {ok} ({elapsed:.1f}s)")

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/v5_01_thinking.png', full_page=True)

    body = page.locator('body').text_content() or ''
    has_thinking = '思考' in body
    has_response = '2' in body and any([
        '1 + 1' in body,
        '1+1' in body,
        '等于' in body,
        '加法' in body,
    ])

    print(f"  Thinking visible: {has_thinking}")
    print(f"  AI response visible: {has_response}")
    results['thinking'] = has_thinking
    results['response'] = has_response

    # ===== TEST 2: History persistence (page refresh) =====
    print("\n=== TEST 2: History (page refresh) ===")
    page.reload()
    page.wait_for_load_state('networkidle')
    time.sleep(4)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/v5_02_history.png', full_page=True)

    body2 = page.locator('body').text_content() or ''
    has_history = any([
        '1+1' in body2,
        '1 + 1' in body2,
        '思考' in body2 and '等于' in body2,
    ])
    assistant_count = page.locator('[data-role="assistant"]').count()

    print(f"  Old messages visible: {has_history}")
    print(f"  Assistant messages: {assistant_count}")
    results['history'] = has_history

    # ===== TEST 3: Chat after reload =====
    print("\n=== TEST 3: Chat after reload ===")
    ta = page.locator('textarea').first
    if ta.count() > 0 and ta.is_enabled():
        send_message(page, '你好')
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] Message sent after reload")

        ok = wait_for_response(page, timeout=60)
        elapsed = time.time() - t0
        print(f"[{time.strftime('%H:%M:%S')}] Response complete: {ok} ({elapsed:.1f}s)")

        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/v5_03_after_reload.png', full_page=True)

        body3 = page.locator('body').text_content() or ''
        has_greeting = any([
            '你好' in body3 and body3.count('你好') >= 2,
            '你好' in body3 and ('帮助' in body3 or '助手' in body3 or 'AI' in body3),
        ])
        print(f"  Greeting response: {has_greeting}")
        results['after_reload'] = has_greeting
    else:
        print(f"  SKIP: textarea not ready (count={ta.count()}, enabled={ta.is_enabled() if ta.count() > 0 else 'N/A'})")
        results['after_reload'] = False

    # ===== Summary =====
    print("\n=== SUMMARY ===")
    all_pass = all(results.values())
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")

    if page_errors:
        print(f"\n  Page errors ({len(page_errors)}):")
        for e in page_errors[:5]:
            print(f"    {e}")

    print(f"\n  Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")

    browser.close()
    print("\nDone!")
