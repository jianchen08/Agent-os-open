"""Test clicking interaction card choice button."""
import io
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        viewport={'width': 1280, 'height': 900}
    )

    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')

    # Login
    username_input = page.locator(
        'input[type="text"], input[name="username"]'
    ).first
    password_input = page.locator(
        'input[type="password"]'
    ).first
    username_input.fill('demo')
    password_input.fill('demo12345')
    page.locator(
        'button[type="submit"]'
    ).first.click()
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    # Start new chat
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(1)

    # Send interaction trigger
    textarea = page.locator('textarea').first
    textarea.fill('提交一个任务测试，人类交互工具')
    textarea.press('Enter')
    print(f"[{time.strftime('%H:%M:%S')}] Sent message")

    # Wait for interaction card
    print("Waiting for interaction card...")
    try:
        page.wait_for_selector(
            '.animate-pulse-subtle',
            timeout=30000
        )
        print(f"[{time.strftime('%H:%M:%S')}] "
              "Interaction card appeared!")
    except Exception as e:
        print(f"Timeout waiting for card: {e}")
        page.screenshot(
            path='d:/Jianguoyun/Agent os/tmp/'
            'click_test_timeout.png',
            full_page=True
        )
        browser.close()
        sys.exit(1)

    page.screenshot(
        path='d:/Jianguoyun/Agent os/tmp/'
        'click_test_01_card_visible.png',
        full_page=True
    )

    # Find and click the first choice button
    # inside the interaction card
    card = page.locator('.animate-pulse-subtle')
    buttons = card.locator('button')
    btn_count = buttons.count()
    print(f"Buttons in card: {btn_count}")

    for i in range(btn_count):
        text = buttons.nth(i).text_content() or ''
        print(f"  Button {i}: '{text.strip()}'")

    if btn_count > 0:
        first_btn_text = (
            buttons.first.text_content() or ''
        ).strip()
        print(f"Clicking: '{first_btn_text}'")
        buttons.first.click()
        time.sleep(3)
        page.screenshot(
            path='d:/Jianguoyun/Agent os/tmp/'
            'click_test_02_after_click.png',
            full_page=True
        )

        # Check completion
        content = page.content()
        if '已完成' in content:
            print("SUCCESS: Card shows completed")
        else:
            print("Card may still be processing")

        time.sleep(5)
        page.screenshot(
            path='d:/Jianguoyun/Agent os/tmp/'
            'click_test_03_final.png',
            full_page=True
        )

        # Check if assistant continued
        msgs = page.locator(
            '[data-role="assistant"]'
        )
        print(f"Assistant messages: {msgs.count()}")

        for i in range(msgs.count()):
            text = msgs.nth(i).text_content() or ''
            print(f"  Msg {i}: {text.strip()[:200]}")

    browser.close()
    print("\nDone!")
