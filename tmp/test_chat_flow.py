"""Debug test: Chat flow with screenshots saved to project dir."""
from playwright.sync_api import sync_playwright
import os

SCREENSHOT_DIR = r"d:\Jianguoyun\Agent os\tmp"

def screenshot(page, name):
    path = os.path.join(SCREENSHOT_DIR, name)
    page.screenshot(path=path, full_page=True)
    print(f"Screenshot saved: {path} (size={os.path.getsize(path)})")
    return path

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    
    # Step 1: Navigate
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')
    screenshot(page, 'step0_landing.png')
    
    # Step 2: Login
    page.locator('input[type="text"]').first.fill('demo')
    page.locator('input[type="password"]').first.fill('demo12345')
    page.locator('button[type="submit"]').first.click()
    
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(3000)
    screenshot(page, 'step1_after_login.png')
    print(f"URL after login: {page.url}")
    
    # Step 3: Check what's visible
    print(f"\nTextareas: {page.locator('textarea').count()}")
    print(f"Contenteditable: {page.locator('[contenteditable]').count()}")
    
    # Look for any session/chat links in sidebar
    sidebar_links = page.locator('nav a, aside a, [class*="sidebar"] a, [class*="session"] a')
    print(f"Sidebar links: {sidebar_links.count()}")
    for i in range(min(sidebar_links.count(), 5)):
        text = sidebar_links.nth(i).inner_text()[:40]
        href = sidebar_links.nth(i).get_attribute('href') or ''
        print(f"  [{i}] text='{text}' href='{href}'")
    
    # Look for any buttons
    all_btns = page.locator('button:visible')
    print(f"\nVisible buttons: {all_btns.count()}")
    for i in range(min(all_btns.count(), 15)):
        text = all_btns.nth(i).inner_text()[:40]
        aria = all_btns.nth(i).get_attribute('aria-label') or ''
        print(f"  [{i}] text='{text}' aria='{aria}'")
    
    # Try navigating to a known route
    page.goto('http://localhost:5188/chat')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(2000)
    screenshot(page, 'step2_chat_route.png')
    print(f"\nURL after /chat: {page.url}")
    print(f"Textareas at /chat: {page.locator('textarea').count()}")
    
    # Check the page content
    body_text = page.locator('body').inner_text()
    print(f"Body text (first 500): {body_text[:500]}")
    
    browser.close()
    print("\nDone!")
