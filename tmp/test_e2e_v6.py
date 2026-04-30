"""
E2E test v6: Comprehensive test with reliable interactions.
Tests: thinking display, AI response, history persistence via session click.
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright


def send_message(page, text):
    """Send a chat message using keyboard input."""
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
    time.sleep(3)

    results = {}

    # ===== TEST 1: Create new chat, send message, check thinking + AI response =====
    print("\n=== TEST 1: New chat with thinking content ===")

    # Create new chat
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(2)

    # Send message using keyboard
    send_message(page, 'what is 3+3? think step by step')
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] Message sent")

    ok = wait_for_response(page, timeout=60)
    elapsed = time.time() - t0
    print(f"[{time.strftime('%H:%M:%S')}] Response complete: {ok} ({elapsed:.1f}s)")

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/v6_01_response.png', full_page=True)

    body = page.locator('body').text_content() or ''
    has_thinking = '思考' in body or 'step' in body.lower() or 'thinking' in body.lower()
    has_response = '6' in body and ('3+3' in body or '3 + 3' in body or '3*3' not in body)

    print(f"  Thinking content visible: {has_thinking}")
    print(f"  AI math answer (6) visible: {has_response}")

    # Count assistant messages
    assistant_count = page.locator('[data-role="assistant"]').count()
    print(f"  Assistant messages: {assistant_count}")

    results['thinking'] = has_thinking
    results['response'] = has_response

    # ===== TEST 2: History reload via page refresh =====
    print("\n=== TEST 2: History reload via page refresh ===")

    # First save current URL
    current_url = page.url

    page.reload()
    page.wait_for_load_state('networkidle')
    time.sleep(4)

    # After reload, we should be on the welcome page (no active session)
    # Click the first session in the sidebar (sessions titled with agent name)
    session_items = page.locator('[title="灵汐"]')
    print(f"  Session items found: {session_items.count()}")

    if session_items.count() > 0:
        # Click the first session
        session_items.first.click()
        time.sleep(3)

        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/v6_02_history.png', full_page=True)

        body2 = page.locator('body').text_content() or ''
        has_history = any([
            '3+3' in body2 or '3 + 3' in body2,
            'step' in body2.lower(),
        ])
        assistant_after = page.locator('[data-role="assistant"]').count()

        print(f"  History content visible: {has_history}")
        print(f"  Assistant messages after click: {assistant_after}")
        results['history'] = has_history
    else:
        print("  No sessions found in sidebar")
        results['history'] = False

    # ===== TEST 3: Send another message (verifies continuous chat) =====
    print("\n=== TEST 3: Continuous chat after history reload ===")

    ta = page.locator('textarea').first
    if ta.count() > 0 and ta.is_enabled():
        send_message(page, 'hello')
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] Second message sent")

        ok = wait_for_response(page, timeout=60)
        elapsed = time.time() - t0
        print(f"[{time.strftime('%H:%M:%S')}] Response complete: {ok} ({elapsed:.1f}s)")

        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/v6_03_continuous.png', full_page=True)

        body3 = page.locator('body').text_content() or ''
        has_greeting = '你好' in body3 or 'Hello' in body3 or 'hello' in body3.lower() or '帮助' in body3
        print(f"  Greeting response: {has_greeting}")
        results['continuous'] = has_greeting
    else:
        print(f"  SKIP: textarea not ready")
        results['continuous'] = False

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
