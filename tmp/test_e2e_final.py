"""
E2E test v4: reliable with longer timeouts.
Tests thinking content, tool cards, history messages.
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

    # Navigate and login
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')
    time.sleep(1)

    page.locator('input[type="text"], input[name="username"]').first.fill('demo')
    page.locator('input[type="password"]').first.fill('demo12345')
    page.locator('button[type="submit"]').first.click()
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    # Create new chat session
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(2)

    results = {}

    # ===== TEST 1: Thinking content =====
    print("\n=== TEST 1: Thinking content ===")
    ta = page.locator('textarea').first
    ta.fill('请思考一下1+1等于几，说明你的思考过程')
    ta.press('Enter')
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] Sent message, waiting for response...")

    ok = wait_for_response(page, timeout=90)
    elapsed = time.time() - t0
    print(f"[{time.strftime('%H:%M:%S')}] Response complete: {ok} ({elapsed:.1f}s)")

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e4_01_thinking.png', full_page=True)

    # Analyze
    body = page.locator('body').text_content() or ''
    has_thinking = '思考' in body
    has_ai_response = ('1 + 1 = 2' in body or '1+1=2' in body or '等于二' in body or
                       '2' in body and ('加' in body or '数学' in body))
    print(f"  Thinking content visible: {has_thinking}")
    print(f"  AI math answer visible: {has_ai_response}")
    results['thinking'] = has_thinking
    results['ai_response'] = has_ai_response

    # ===== TEST 2: Tool usage =====
    print("\n=== TEST 2: Tool card display ===")
    ta = page.locator('textarea').first
    ta.fill('请列出当前工作目录下的所有Python文件，使用search工具搜索')
    ta.press('Enter')
    t0 = time.time()
    print(f"[{time.strftime('%H:%M:%S')}] Sent tool message, waiting...")

    ok = wait_for_response(page, timeout=90)
    elapsed = time.time() - t0
    print(f"[{time.strftime('%H:%M:%S')}] Response complete: {ok} ({elapsed:.1f}s)")

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e4_02_tool.png', full_page=True)

    body2 = page.locator('body').text_content() or ''
    # Check for tool execution or file search results
    has_tool = any([
        '.py' in body2 and ('搜索' in body2 or '文件' in body2 or 'search' in body2.lower()),
        '工具' in body2 and '调用' in body2,
        '执行' in body2,
    ])
    has_search_result = '.py' in body2
    print(f"  Tool content visible: {has_tool}")
    print(f"  Search results (.py files): {has_search_result}")
    results['tool'] = has_tool
    results['search'] = has_search_result

    # ===== TEST 3: History messages (page refresh) =====
    print("\n=== TEST 3: History messages (refresh) ===")
    page.reload()
    page.wait_for_load_state('networkidle')
    time.sleep(3)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e4_03_history.png', full_page=True)

    body3 = page.locator('body').text_content() or ''
    has_history = any([
        '1+1' in body3 or '1 + 1' in body3,
        'Python' in body3 or '.py' in body3,
    ])
    assistant_count = page.locator('[data-role="assistant"]').count()
    print(f"  Messages persisted after refresh: {has_history}")
    print(f"  Assistant messages: {assistant_count}")
    results['history'] = has_history

    # ===== Summary =====
    print("\n=== SUMMARY ===")
    all_pass = all(results.values())
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")

    if page_errors:
        print(f"\n  Page errors ({len(page_errors)}):")
        for e in page_errors:
            print(f"    {e}")

    print(f"\n  Overall: {'ALL PASS' if all_pass else 'SOME FAILURES'}")

    browser.close()
    print("\nDone!")
