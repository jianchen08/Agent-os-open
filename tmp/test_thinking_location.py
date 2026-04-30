"""Test: Verify thinking indicator DOM location at 3 seconds."""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright
import os

SCREENSHOT_DIR = r"d:\Jianguoyun\Agent os\tmp"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    
    # Login
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')
    page.locator('input[type="text"]').first.fill('demo')
    page.locator('input[type="password"]').first.fill('demo12345')
    page.locator('button[type="submit"]').first.click()
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(2000)
    
    # Create session
    page.locator('button:has-text("新会话")').first.click()
    page.wait_for_timeout(2000)
    
    # Send message
    page.locator('textarea').first.fill('hello')
    page.locator('button[aria-label*="发送"]').first.click()
    
    # Wait for stream_start to create assistant message
    page.wait_for_timeout(3000)
    
    # Analyze DOM
    print("=== Thinking Indicator DOM Analysis ===")
    
    # Find all elements containing "思考中"
    thinking_els = page.evaluate("""() => {
        const results = [];
        const walker = document.createTreeWalker(
            document.body,
            NodeFilter.SHOW_TEXT,
            null
        );
        while (walker.nextNode()) {
            if (walker.currentNode.textContent.includes('思考中')) {
                const el = walker.currentNode.parentElement;
                const dataRole = el.closest('[data-role]')?.getAttribute('data-role') || 'none';
                const inMessageList = !!el.closest('[data-testid="message-list"]');
                const inInput = !!el.closest('[class*="input"]');
                const inChatInput = !!el.closest('[class*="ChatInput"]') || !!el.closest('[class*="chat-input"]');
                const classes = el.className || '';
                const tag = el.tagName;
                const parentClasses = el.parentElement?.className || '';
                const grandparentClasses = el.parentElement?.parentElement?.className || '';
                
                results.push({
                    tag,
                    dataRole,
                    inMessageList,
                    inInput,
                    inChatInput,
                    classes: classes.substring(0, 100),
                    parentClasses: parentClasses.substring(0, 100),
                    grandparentClasses: grandparentClasses.substring(0, 100),
                });
            }
        }
        return results;
    }""")
    
    for i, el in enumerate(thinking_els):
        print(f"\nThinking element [{i}]:")
        print(f"  tag: {el['tag']}")
        print(f"  data-role: {el['dataRole']}")
        print(f"  inMessageList: {el['inMessageList']}")
        print(f"  inInput: {el['inInput']}")
        print(f"  inChatInput: {el['inChatInput']}")
        print(f"  classes: {el['classes']}")
        print(f"  parentClasses: {el['parentClasses']}")
        print(f"  grandparentClasses: {el['grandparentClasses']}")
    
    # Check assistant message content
    assistant_msg = page.locator('[data-role="assistant"]')
    print(f"\n=== Assistant Messages: {assistant_msg.count()} ===")
    for i in range(assistant_msg.count()):
        el = assistant_msg.nth(i)
        inner = el.inner_html()[:500]
        print(f"  [{i}] innerHTML: {inner}")
    
    # Check Footer (below messages)
    footer = page.locator('[data-testid="message-list"] [class*="Footer"], [data-testid="message-list"] > div > div:last-child')
    if footer.count() > 0:
        footer_html = footer.first.inner_html()[:200]
        print(f"\nFooter HTML: {footer_html}")
    
    # Screenshot for reference
    path = os.path.join(SCREENSHOT_DIR, 'thinking_location.png')
    page.screenshot(path=path, full_page=True)
    
    browser.close()
    print("\nDone!")
