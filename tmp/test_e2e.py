"""
E2E test: thinking content, tool cards, history messages, interaction card.
"""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 900})

    console_logs = []
    page.on('console', lambda msg: console_logs.append(f'[{msg.type}] {msg.text[:200]}'))
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

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_01_after_login.png', full_page=True)

    # Create new chat session
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(2)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_02_new_chat.png', full_page=True)

    # Check for textarea
    textarea_count = page.locator('textarea').count()
    print(f"After new chat - Textareas: {textarea_count}")

    if textarea_count == 0:
        # Try clicking + button in sidebar
        plus_btn = page.locator('button:has-text("+")')
        if plus_btn.count() > 0:
            plus_btn.first.click()
            time.sleep(2)

        textarea_count = page.locator('textarea').count()
        print(f"After plus button - Textareas: {textarea_count}")

    if textarea_count == 0:
        # Try other approaches
        content_editable = page.locator('[contenteditable="true"]')
        print(f"ContentEditable: {content_editable.count()}")

        # Check all input-like elements
        all_inputs = page.locator('textarea, input[type="text"], [contenteditable="true"], [role="textbox"]')
        print(f"All input-like elements: {all_inputs.count()}")

        # Debug: print all button texts
        all_btns = page.locator('button').all()
        btn_texts = [b.text_content().strip()[:40] for b in all_btns[:20]]
        print(f"Buttons: {btn_texts}")

        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_02b_debug.png', full_page=True)
        print("Cannot find input element. Aborting test.")
        browser.close()
        sys.exit(1)

    # ===== TEST 1: Send a message and check thinking =====
    print("\n=== TEST 1: Message with thinking content ===")
    textarea = page.locator('textarea').first
    textarea.fill('请思考一下1+1等于几')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent thinking test message")

    # Wait for response (up to 30s)
    time.sleep(20)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_03_thinking.png', full_page=True)

    # Check for thinking content
    page_content = page.content()
    has_thinking = any([
        '思考' in page_content,
        'thinking' in page_content.lower(),
        'Thinking' in page_content,
    ])
    print(f"  Has thinking indicator: {has_thinking}")

    # Check assistant messages
    assistant_msgs = page.locator('[data-role="assistant"]')
    print(f"  Assistant messages (data-role): {assistant_msgs.count()}")

    # Check for any visible assistant content
    all_msgs = page.locator('[class*="message"], [class*="Message"]')
    print(f"  Message elements: {all_msgs.count()}")

    # ===== TEST 2: Send a tool-related message =====
    print("\n=== TEST 2: Tool card display ===")
    textarea = page.locator('textarea').first
    if textarea.count() > 0:
        textarea.fill('请帮我搜索一下当前目录下的文件')
        textarea.press('Enter')
        print(f"[{time.strftime('%H:%M:%S')}] Sent tool test message")

        time.sleep(20)
        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_04_tool.png', full_page=True)

        # Check for tool cards
        tool_elements = page.locator('[class*="tool"], [class*="Tool"]')
        print(f"  Tool elements: {tool_elements.count()}")

    # ===== TEST 3: History messages =====
    print("\n=== TEST 3: History messages ===")

    # Create a second chat
    new_chat2 = page.locator('button:has-text("新会话")')
    if new_chat2.count() > 0:
        new_chat2.first.click()
        time.sleep(2)

    # Go back to the first chat via sidebar
    sidebar = page.locator('nav, aside, [class*="sidebar"], [class*="Sidebar"]')
    if sidebar.count() > 0:
        session_items = sidebar.first.locator('button, [class*="session"], [class*="Session"]')
        print(f"  Sidebar session items: {session_items.count()}")

        # Try to click the first session that's not "新会话"
        for i in range(min(session_items.count(), 10)):
            txt = session_items.nth(i).text_content() or ''
            if '新会话' not in txt and txt.strip():
                print(f"  Clicking session: '{txt.strip()[:30]}'")
                session_items.nth(i).click()
                time.sleep(3)
                break

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_05_history.png', full_page=True)

    # Check if messages loaded
    page_content2 = page.content()
    has_history = any([
        '1+1' in page_content2,
        '搜索' in page_content2,
        '文件' in page_content2,
    ])
    print(f"  Has history content: {has_history}")

    # ===== Summary =====
    print("\n=== SUMMARY ===")
    print(f"Thinking displayed: {has_thinking}")
    print(f"Tool elements found: {tool_elements.count() if textarea_count > 0 else 'N/A'}")
    print(f"History messages loaded: {has_history}")
    print(f"Page errors: {page_errors}")

    if page_errors:
        print("\nPage errors:")
        for e in page_errors:
            print(f"  {e}")

    browser.close()
    print("\nDone!")
