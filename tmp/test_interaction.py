"""Test: Send interaction-triggering message and monitor for InteractionPanel."""
import sys, io, json, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

SCREENSHOT_DIR = r"d:\Jianguoyun\Agent os\tmp"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    
    # Capture console logs
    console_logs = []
    page.on("console", lambda msg: console_logs.append("[{}] {}".format(msg.type, msg.text[:200])))
    
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
    
    # Send interaction-triggering message
    page.locator('textarea').first.fill('提交一个任务测试，人类交互工具')
    page.locator('button[aria-label*="发送"]').first.click()
    print("Message sent!")
    
    # Wait and monitor
    for i in range(10):
        page.wait_for_timeout(3000)
        
        thinking = page.locator('text=思考中')
        
        # Check for ANY new elements that appeared after sending
        all_buttons = page.locator('button:visible')
        button_texts = []
        for j in range(all_buttons.count()):
            try:
                button_texts.append(all_buttons.nth(j).inner_text()[:30])
            except:
                pass
        
        # Check DOM for interaction-related elements
        interaction_els = page.evaluate("""() => {
            const results = [];
            // Check for interaction store data
            const allDivs = document.querySelectorAll('div');
            for (const div of allDivs) {
                const text = div.textContent || '';
                if (text.includes('人类交互') || text.includes('interaction') || 
                    text.includes('选择模式') || text.includes('对话模式') ||
                    text.includes('请选择') || text.includes('请确认')) {
                    results.push({
                        tag: div.tagName,
                        className: (div.className || '').substring(0, 80),
                        text: text.substring(0, 100),
                    });
                }
            }
            return results;
        }""")
        
        print("  {}s: thinking={} buttons={}".format(
            (i+1)*3, thinking.count(), len(button_texts)))
        
        if interaction_els:
            print("  Interaction elements found:")
            for el in interaction_els[:5]:
                print("    class={} text={}".format(el['className'][:60], el['text'][:80]))
        
        if i in [1, 3, 6]:
            path = os.path.join(SCREENSHOT_DIR, 'interaction_{}s.png'.format((i+1)*3))
            page.screenshot(path=path, full_page=True)
        
        # If thinking gone, check what happened
        if thinking.count() == 0 and i > 2:
            print("  Thinking stopped")
            path = os.path.join(SCREENSHOT_DIR, 'interaction_done.png')
            page.screenshot(path=path, full_page=True)
            break
    
    # Print relevant console logs
    print("\n=== Console Logs ({} total) ===".format(len(console_logs)))
    for log in console_logs[-30:]:
        if any(kw in log.lower() for kw in ['interaction', 'error', 'warn', 'pending', 'parse', 'store']):
            print("  {}".format(log))
    
    # Get the page body text for final state
    body = page.locator('[data-testid="message-list"]')
    if body.count() > 0:
        text = body.first.inner_text()
        print("\n=== Message List Text ===")
        print(text[:1000])
    
    browser.close()
    print("\nDone!")
