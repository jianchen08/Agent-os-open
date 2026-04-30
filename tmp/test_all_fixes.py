"""
Comprehensive test: thinking content, tool cards, history messages, interaction card.
Tests the full flow after backend restart.
"""
import io
import sys
import time
import json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        viewport={'width': 1280, 'height': 900}
    )

    # Capture console logs
    console_logs = []
    page.on('console', lambda msg: console_logs.append(
        f'[{msg.type}] {msg.text[:200]}'
    ))

    # Navigate to app
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')

    # Login
    username_input = page.locator(
        'input[type="text"], input[name="username"]'
    ).first
    password_input = page.locator('input[type="password"]').first
    username_input.fill('demo')
    password_input.fill('demo12345')
    page.locator('button[type="submit"]').first.click()
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    # Start new chat
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(1)

    # ===== Test 1: Send a message and check thinking/tool =====
    print("\n=== TEST 1: Message with thinking content ===")
    textarea = page.locator('textarea').first
    textarea.fill('请思考一下1+1等于几，详细说明你的思考过程')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent message")

    # Wait for response
    time.sleep(15)
    page.screenshot(
        path='d:/Jianguoyun/Agent os/tmp/test_all_01_thinking.png',
        full_page=True
    )

    # Check for thinking content
    page_content = page.content()
    has_thinking = '思考' in page_content or 'thinking' in page_content.lower()
    has_assistant_response = page_content.count('data-role="assistant"') > 0
    print(f"  Has thinking indicator: {has_thinking}")
    print(f"  Has assistant message: {has_assistant_response}")

    # Check assistant message content
    assistant_msgs = page.locator('[data-role="assistant"]')
    print(f"  Assistant messages: {assistant_msgs.count()}")
    for i in range(min(assistant_msgs.count(), 3)):
        text = assistant_msgs.nth(i).text_content() or ''
        print(f"    Msg {i}: {text.strip()[:200]}")

    # Check for ThinkingDisplay component
    thinking_display = page.locator(
        '[class*="thinking"], [data-testid*="thinking"]'
    )
    print(f"  Thinking display elements: {thinking_display.count()}")

    # ===== Test 2: Interaction card =====
    print("\n=== TEST 2: Interaction card ===")
    textarea = page.locator('textarea').first
    textarea.fill('提交一个任务测试，人类交互工具')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent interaction trigger")

    # Wait for interaction card
    try:
        page.wait_for_selector(
            '.animate-pulse-subtle',
            timeout=30000
        )
        print(f"[{time.strftime('%H:%M:%S')}] Interaction card appeared!")
    except Exception:
        print("  Timeout waiting for interaction card (30s)")

    page.screenshot(
        path='d:/Jianguoyun/Agent os/tmp/test_all_02_interaction.png',
        full_page=True
    )

    # Check waiting text
    page_content = page.content()
    has_waiting = '等待用户响应' in page_content
    print(f"  Has '等待用户响应...': {has_waiting}")

    # Click choice button
    card = page.locator('.animate-pulse-subtle')
    buttons = card.locator('button')
    if buttons.count() > 0:
        first_btn = buttons.first.text_content() or ''
        print(f"  Clicking choice: '{first_btn.strip()}'")
        buttons.first.click()
        time.sleep(5)
    else:
        print("  No choice buttons found")

    page.screenshot(
        path='d:/Jianguoyun/Agent os/tmp/test_all_03_after_choice.png',
        full_page=True
    )

    # ===== Test 3: History messages =====
    print("\n=== TEST 3: History messages ===")

    # Create a new session and check if messages persist
    new_chat2 = page.locator('button:has-text("新会话")')
    if new_chat2.count() > 0:
        new_chat2.first.click()
        time.sleep(1)

    # Go back to the first session
    session_items = page.locator(
        '[data-testid="session-item"], '
        'button:has-text("会话"), '
        '.session-item, '
        '[class*="session"]'
    )
    print(f"  Session items found: {session_items.count()}")

    # Try clicking sidebar session items
    sidebar = page.locator('nav, aside, [class*="sidebar"]')
    if sidebar.count() > 0:
        session_buttons = sidebar.first.locator('button')
        print(f"  Sidebar buttons: {session_buttons.count()}")
        # Click the second session (first chat)
        if session_buttons.count() >= 3:
            session_buttons.nth(2).click()
            time.sleep(3)

    page.screenshot(
        path='d:/Jianguoyun/Agent os/tmp/test_all_04_history.png',
        full_page=True
    )

    # Check if messages loaded
    assistant_msgs = page.locator('[data-role="assistant"]')
    user_msgs = page.locator('[data-role="user"]')
    print(f"  User messages in history: {user_msgs.count()}")
    print(f"  Assistant messages in history: {assistant_msgs.count()}")

    # ===== Summary =====
    print("\n=== SUMMARY ===")
    print(f"Thinking displayed: {has_thinking}")
    print(f"Interaction card worked: {has_waiting}")
    print(f"History messages loaded: {assistant_msgs.count() > 0 or user_msgs.count() > 0}")

    # Print relevant console logs
    ws_logs = [l for l in console_logs if 'thinking' in l.lower() or 'tool_start' in l.lower() or 'interaction' in l.lower()]
    if ws_logs:
        print(f"\nRelevant console logs ({len(ws_logs)}):")
        for log in ws_logs[:10]:
            print(f"  {log}")

    browser.close()
    print("\nDone!")
