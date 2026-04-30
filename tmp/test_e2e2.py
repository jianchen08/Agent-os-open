"""
E2E test: thinking content, tool cards, history messages, interaction card.
V2 - more robust with better debugging.
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

    # Create new chat session
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(2)

    textarea = page.locator('textarea').first
    if textarea.count() == 0:
        print("ERROR: No textarea found after creating new chat")
        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_err_no_textarea.png', full_page=True)
        browser.close()
        sys.exit(1)

    # ===== TEST 1: Send a message and check thinking =====
    print("\n=== TEST 1: Message with thinking content ===")
    textarea.fill('请思考一下1+1等于几')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent thinking test message")

    # Wait for response (up to 30s)
    time.sleep(25)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_03_thinking.png', full_page=True)

    # Check for thinking content in the DOM
    page_content = page.content()
    has_thinking = any([
        '思考' in page_content,
        'thinking' in page_content.lower(),
        'Thinking' in page_content,
    ])
    print(f"  Has thinking indicator: {has_thinking}")

    # Check message elements more thoroughly
    # Try different selectors for messages
    msg_selectors = [
        '[data-role="assistant"]',
        '[class*="message-item"]',
        '[class*="MessageItem"]',
        '[class*="bubble"]',
        '[class*="Bubble"]',
        '.prose',
    ]
    for sel in msg_selectors:
        count = page.locator(sel).count()
        if count > 0:
            print(f"  {sel}: {count} found")

    # Check for any text that looks like an AI response
    body_text = page.locator('body').text_content() or ''
    print(f"  Body text (first 500): {body_text[:500]}")

    # Check streaming status
    streaming_indicators = ['思考中', '生成中', 'typing', 'streaming', 'loading']
    for ind in streaming_indicators:
        if ind in body_text:
            print(f"  Found streaming indicator: '{ind}'")

    # Check thinking display component
    thinking_el = page.locator('[class*="thinking"], [class*="Thinking"]')
    print(f"  Thinking elements: {thinking_el.count()}")

    tool_count = 0

    # ===== TEST 2: Send a tool-related message =====
    print("\n=== TEST 2: Tool card display ===")
    textarea = page.locator('textarea').first
    if textarea.count() > 0:
        textarea.fill('请帮我搜索一下当前目录下的Python文件')
        textarea.press('Enter')
        print(f"[{time.strftime('%H:%M:%S')}] Sent tool test message")

        time.sleep(25)
        page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_04_tool.png', full_page=True)

        # Check for tool cards
        tool_selectors = [
            '[class*="tool"]',
            '[class*="Tool"]',
            '[class*="execution"]',
            '[class*="Execution"]',
            '[class*="activity"]',
            '[class*="Activity"]',
        ]
        for sel in tool_selectors:
            count = page.locator(sel).count()
            if count > 0:
                tool_count += count
                print(f"  {sel}: {count} found")

        if tool_count == 0:
            print("  No tool card elements found")

    # ===== TEST 3: History messages =====
    print("\n=== TEST 3: History messages ===")

    # Create a second chat
    new_chat2 = page.locator('button:has-text("新会话")')
    if new_chat2.count() > 0:
        new_chat2.first.click()
        time.sleep(2)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_05_new_chat2.png', full_page=True)

    # Go back to the first chat via sidebar
    # Look for session list items
    session_items = page.locator('[class*="session-item"], [class*="SessionItem"], [class*="history"]')
    if session_items.count() > 0:
        # Click the first non-active session
        for i in range(session_items.count()):
            txt = session_items.nth(i).text_content() or ''
            if txt.strip() and '新会话' not in txt:
                print(f"  Clicking session: '{txt.strip()[:30]}'")
                session_items.nth(i).click()
                time.sleep(3)
                break
    else:
        # Try clicking sidebar buttons
        sidebar = page.locator('[class*="sidebar"], [class*="Sidebar"], nav, aside')
        if sidebar.count() > 0:
            buttons = sidebar.first.locator('button')
            print(f"  Sidebar buttons: {buttons.count()}")
            for i in range(min(buttons.count(), 10)):
                txt = buttons.nth(i).text_content() or ''
                if txt.strip() and '新会话' not in txt and '+' not in txt:
                    print(f"  Clicking button: '{txt.strip()[:30]}'")
                    buttons.nth(i).click()
                    time.sleep(3)
                    break

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_06_history.png', full_page=True)

    # Check if messages loaded
    page_content2 = page.content()
    has_history = any([
        '1+1' in page_content2,
        '搜索' in page_content2,
        'Python' in page_content2,
    ])
    print(f"  Has history content: {has_history}")

    # ===== TEST 4: Interaction card (optional) =====
    print("\n=== TEST 4: Interaction card ===")
    textarea = page.locator('textarea').first
    if textarea.count() > 0:
        textarea.fill('提交一个任务测试')
        textarea.press('Enter')
        print(f"[{time.strftime('%H:%M:%S')}] Sent interaction trigger")

        # Wait for interaction card
        try:
            page.wait_for_selector('.animate-pulse-subtle', timeout=15000)
            print(f"[{time.strftime('%H:%M:%S')}] Interaction card appeared!")
            page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_07_interaction.png', full_page=True)

            # Check waiting text
            has_waiting = '等待用户响应' in page.content()
            print(f"  Has '等待用户响应': {has_waiting}")
        except Exception:
            print("  No interaction card appeared (15s timeout)")
            page.screenshot(path='d:/Jianguoyun/Agent os/tmp/e2e_07_no_interaction.png', full_page=True)
    else:
        print("  Skipped (no textarea)")

    # ===== Summary =====
    print("\n=== SUMMARY ===")
    print(f"Thinking displayed: {has_thinking}")
    print(f"Tool elements found: {tool_count}")
    print(f"History messages loaded: {has_history}")
    print(f"Page errors: {len(page_errors)}")

    if page_errors:
        print("\nPage errors:")
        for e in page_errors:
            print(f"  {e}")

    # Print relevant console logs
    ws_logs = [l for l in console_logs if any(k in l.lower() for k in ['thinking', 'tool', 'error', 'warn'])]
    if ws_logs:
        print(f"\nRelevant console logs ({len(ws_logs)}):")
        for log in ws_logs[:15]:
            print(f"  {log}")

    browser.close()
    print("\nDone!")
