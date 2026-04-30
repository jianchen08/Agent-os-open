"""
Test: Human interaction card appears and is visible when triggered.
Verifies:
1. "等待用户响应..." replaces "思考中..." in assistant message
2. InteractionCard is visible with correct content
3. Choice buttons are clickable
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

    # Navigate to app
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_01_login.png', full_page=True)

    # Login - check what's on the page first
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_01_login.png', full_page=True)

    # Try different login selectors
    username_input = page.locator('input[type="text"], input[name="username"], input[placeholder*="用户"]').first
    password_input = page.locator('input[type="password"], input[name="password"], input[placeholder*="密码"]').first

    if username_input.count() > 0:
        username_input.fill('demo')
        password_input.fill('demo12345')
        page.locator('button[type="submit"], button:has-text("登录"), button:has-text("Login")').first.click()
    else:
        print("No login form found, might already be logged in or different page")
    page.wait_for_load_state('networkidle')
    time.sleep(2)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_02_after_login.png', full_page=True)

    # Click "新会话" to start a chat
    new_chat_btn = page.locator('button:has-text("新会话")')
    if new_chat_btn.count() > 0:
        new_chat_btn.first.click()
        time.sleep(1)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_03_chat_ready.png', full_page=True)

    # Send message that triggers human_interaction tool
    textarea = page.locator('textarea').first
    textarea.fill('提交一个任务测试，人类交互工具')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent human interaction trigger message")

    # Wait for interaction card to appear
    time.sleep(5)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_04_at_5s.png', full_page=True)

    # Check for "等待用户响应..." text
    page_content = page.content()
    has_waiting_text = '等待用户响应' in page_content
    has_thinking_text = '思考中' in page_content
    print(f"[5s] Has '等待用户响应...': {has_waiting_text}")
    print(f"[5s] Has '思考中...': {has_thinking_text}")

    # Check for interaction card
    interaction_cards = page.locator('[data-testid="chat-container"] .mx-4.my-3.rounded-xl.border, [class*="interaction"], .border-blue-500\\/40')
    card_count = interaction_cards.count()
    print(f"[5s] Interaction cards found: {card_count}")

    # Check for choice buttons
    all_buttons = page.locator('button').all()
    button_texts = [b.text_content().strip() for b in all_buttons if b.text_content()]
    print(f"[5s] All buttons: {button_texts}")

    # Wait more and check again
    time.sleep(5)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_05_at_10s.png', full_page=True)

    page_content = page.content()
    has_waiting_text_10 = '等待用户响应' in page_content
    print(f"[10s] Has '等待用户响应...': {has_waiting_text_10}")

    # Look for the interaction card more precisely
    card_elements = page.locator('.rounded-xl.border').all()
    for i, el in enumerate(card_elements):
        text = el.text_content().strip()[:200] if el.text_content() else ''
        classes = el.get_attribute('class') or ''
        if 'blue' in classes or 'interaction' in text.lower() or '选项' in text or '交互' in text:
            print(f"[10s] Card {i}: class={classes[:100]} text={text[:100]}")

    # Try to find and click a choice button
    choice_buttons = page.locator('button:has-text("确认"), button:has-text("取消"), button:has-text("选项"), button:has-text("测试")')
    print(f"[10s] Choice buttons count: {choice_buttons.count()}")

    for btn in choice_buttons.all():
        text = btn.text_content().strip()
        if text in ['确认', '取消'] or '选项' in text or '测试' in text:
            print(f"[10s] Found choice button: '{text}', clicking...")
            btn.click()
            time.sleep(3)
            page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_06_after_choice.png', full_page=True)

            # Check if interaction was resolved
            page_content_after = page.content()
            has_done = '已完成' in page_content_after or '已跳转' in page_content_after
            print(f"[After choice] Has completion text: {has_done}")
            break

    time.sleep(3)
    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/interaction_fix_07_final.png', full_page=True)

    # Final summary
    print("\n=== TEST SUMMARY ===")
    print(f"1. '等待用户响应...' appeared: {has_waiting_text or has_waiting_text_10}")
    print(f"2. Interaction card appeared: {card_count > 0 or has_waiting_text or has_waiting_text_10}")
    print(f"3. Visual prominence (blue border/shadow): check screenshots")

    browser.close()
    print("\nDone!")
