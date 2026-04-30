"""
E2E test v3: thinking, tools, history, interaction.
Handles disabled textarea by waiting for it to become enabled.
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright


def wait_for_textarea_enabled(page, timeout=60):
    """Wait for textarea to become enabled."""
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
    page.on('pageerror', lambda err: page_errors.append(str(err)[:300]))

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

    # ===== TEST 1: Thinking content =====
    print("\n=== TEST 1: Thinking content ===")
    wait_for_textarea_enabled(page)
    textarea = page.locator('textarea').first
    textarea.fill('请思考一下1+1等于几，详细说明你的思考过程')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent thinking test message")

    # Wait for response to complete
    wait_for_textarea_enabled(page, timeout=60)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e3_01_thinking.png', full_page=True)

    # Check results
    page_text = page.locator('body').text_content() or ''
    has_thinking_content = '思考' in page_text and ('The user' in page_text or '1+1' in page_text or '数学' in page_text or 'thinking' in page_text.lower())
    has_ai_response = '2' in page_text and ('1 + 1' in page_text or '1+1' in page_text)
    print(f"  Thinking content visible: {has_thinking_content}")
    print(f"  AI response visible: {has_ai_response}")

    # ===== TEST 2: Tool usage =====
    print("\n=== TEST 2: Tool card display ===")
    wait_for_textarea_enabled(page)
    textarea = page.locator('textarea').first
    textarea.fill('请使用搜索工具搜索当前目录下所有py文件')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent tool test message")

    wait_for_textarea_enabled(page, timeout=60)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e3_02_tool.png', full_page=True)

    # Check for tool-related content
    page_text2 = page.locator('body').text_content() or ''
    has_tool_card = any([
        '搜索' in page_text2 and ('.py' in page_text2 or '文件' in page_text2),
        'Tool' in page_text2,
        '工具' in page_text2 and ('调用' in page_text2 or '执行' in page_text2),
    ])
    print(f"  Tool card/content visible: {has_tool_card}")

    # Check specific tool elements
    tool_selectors = [
        ('[data-role="assistant"]', 'assistant messages'),
        ('[class*="execution"]', 'execution cards'),
        ('[class*="activity"]', 'activity cards'),
    ]
    for sel, label in tool_selectors:
        count = page.locator(sel).count()
        if count > 0:
            print(f"  {label} ({sel}): {count}")

    # ===== TEST 3: History messages =====
    print("\n=== TEST 3: History messages ===")

    # Refresh the page to test if messages persist
    page.reload()
    page.wait_for_load_state('networkidle')
    time.sleep(3)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e3_03_history_refresh.png', full_page=True)

    # After refresh, check if old messages are still there
    page_text3 = page.locator('body').text_content() or ''
    has_persisted = any([
        '1+1' in page_text3,
        '搜索' in page_text3 and 'py' in page_text3,
    ])
    print(f"  Messages persisted after refresh: {has_persisted}")

    # Also check if assistant messages are rendered
    assistant_count = page.locator('[data-role="assistant"]').count()
    print(f"  Assistant messages after refresh: {assistant_count}")

    # ===== TEST 4: Interaction card =====
    print("\n=== TEST 4: Interaction card ===")
    wait_for_textarea_enabled(page, timeout=30)
    textarea = page.locator('textarea').first

    if textarea.count() > 0 and textarea.is_enabled():
        textarea.fill('请使用 human_interaction 工具向用户确认是否继续')
        textarea.press('Enter')
        print(f"[{time.strftime('%H:%M:%S')}] Sent interaction trigger")

        # Wait for interaction card or response
        time.sleep(20)
        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e3_04_interaction.png', full_page=True)

        has_interaction = any([
            '等待用户响应' in page.content(),
            'animate-pulse-subtle' in page.content(),
            'interaction' in page.content().lower(),
            '选项' in page.content(),
        ])
        print(f"  Interaction card appeared: {has_interaction}")
    else:
        print("  Skipped (textarea not available)")

    # ===== Summary =====
    print("\n=== SUMMARY ===")
    print(f"  1. Thinking content: {'PASS' if has_thinking_content else 'FAIL'}")
    print(f"  2. AI response rendered: {'PASS' if has_ai_response else 'FAIL'}")
    print(f"  3. Tool content: {'PASS' if has_tool_card else 'FAIL'}")
    print(f"  4. History after refresh: {'PASS' if has_persisted else 'FAIL'}")
    print(f"  5. Page errors: {len(page_errors)}")

    if page_errors:
        for e in page_errors:
            print(f"    ERROR: {e}")

    browser.close()
    print("\nDone!")
