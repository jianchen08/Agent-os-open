"""Debug: check what the page looks like after login."""
import io, sys, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')

    # Login
    page.locator('input[type="text"], input[name="username"]').first.fill('demo')
    page.locator('input[type="password"]').first.fill('demo12345')
    page.locator('button[type="submit"]').first.click()
    page.wait_for_load_state('networkidle')
    time.sleep(2)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/debug_01_after_login.png', full_page=True)

    # Check what's on page
    textareas = page.locator('textarea').count()
    buttons = page.locator('button').all()
    btn_texts = [b.text_content().strip() for b in buttons[:15]]
    print(f"Textareas: {textareas}")
    print(f"Buttons: {btn_texts}")

    # Click new chat if needed
    new_chat = page.locator('button:has-text("新会话")')
    if new_chat.count() > 0:
        new_chat.first.click()
        time.sleep(1)

    page.screenshot(path='d:/Jianguoyun/Agent os/tmp/debug_02_after_newchat.png', full_page=True)
    textareas = page.locator('textarea').count()
    print(f"Textareas after new chat: {textareas}")

    browser.close()
