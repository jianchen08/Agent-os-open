"""Full E2E test: login → create session → send message → verify AI response."""
from playwright.sync_api import sync_playwright
import os

SCREENSHOT_DIR = r"d:\Jianguoyun\Agent os\tmp"

def screenshot(page, name):
    path = os.path.join(SCREENSHOT_DIR, name)
    page.screenshot(path=path, full_page=True)
    print(f"  Screenshot: {name} ({os.path.getsize(path)} bytes)")
    return path

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    
    # --- LOGIN ---
    print("[1] Navigating to app...")
    page.goto('http://localhost:5188')
    page.wait_for_load_state('networkidle')
    
    print("[2] Logging in...")
    page.locator('input[type="text"]').first.fill('demo')
    page.locator('input[type="password"]').first.fill('demo12345')
    page.locator('button[type="submit"]').first.click()
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(2000)
    screenshot(page, 'flow_1_after_login.png')
    
    # --- CREATE SESSION ---
    print("[3] Creating new session...")
    new_session_btn = page.locator('button:has-text("新会话")').first
    if new_session_btn.is_visible():
        new_session_btn.click()
        page.wait_for_timeout(2000)
        print("  Clicked '新会话' button")
    else:
        print("  ERROR: '新会话' button not found!")
    
    screenshot(page, 'flow_2_new_session.png')
    
    # Check if textarea appeared
    textareas = page.locator('textarea')
    print(f"  Textareas after new session: {textareas.count()}")
    
    if textareas.count() > 0:
        ph = textareas.first.get_attribute('placeholder') or ''
        print(f"  Textarea placeholder: '{ph}'")
    
    # --- SEND MESSAGE ---
    if textareas.count() > 0:
        print("[4] Sending message 'hello'...")
        textareas.first.click()
        page.wait_for_timeout(300)
        textareas.first.fill('hello')
        page.wait_for_timeout(500)
        screenshot(page, 'flow_3_typed.png')
        
        # Find send button - look for button near textarea with SVG
        # Try multiple strategies
        sent = False
        
        # Strategy 1: aria-label
        send_btn = page.locator('button[aria-label*="发送"], button[aria-label*="send"]').first
        if send_btn.is_visible():
            send_btn.click()
            sent = True
            print("  Sent via aria-label button")
        
        if not sent:
            # Strategy 2: Look for the submit button (usually has an arrow/Send icon)
            # Get all visible buttons and check their HTML for send-related SVGs
            buttons = page.locator('button:visible')
            for i in range(buttons.count()):
                btn = buttons.nth(i)
                html = btn.evaluate('el => el.outerHTML')
                # The send button typically has a Send/ArrowUp icon
                if 'send' in html.lower() or 'arrowup' in html.lower() or 'arrow-up' in html.lower():
                    btn.click()
                    sent = True
                    print(f"  Sent via button [{i}] (contains send/arrow icon)")
                    break
        
        if not sent:
            # Strategy 3: Press Enter
            print("  Trying Enter key...")
            textareas.first.press('Enter')
            sent = True
        
        page.wait_for_timeout(3000)
        screenshot(page, 'flow_4_after_send.png')
        
        # --- WAIT FOR AI RESPONSE ---
        print("[5] Waiting for AI response...")
        for i in range(10):
            page.wait_for_timeout(3000)
            
            # Check message roles
            assistant_msgs = page.locator('[data-role="assistant"]')
            user_msgs = page.locator('[data-role="user"]')
            thinking = page.locator('text=思考中')
            spinners = page.locator('.animate-spin')
            
            a_count = assistant_msgs.count()
            u_count = user_msgs.count()
            t_count = thinking.count()
            s_count = spinners.count()
            
            print(f"  {(i+1)*3}s: user_msgs={u_count} assistant_msgs={a_count} thinking={t_count} spinners={s_count}")
            
            if i in [0, 1, 2, 4, 7]:
                screenshot(page, f'flow_5_wait_{(i+1)*3}s.png')
            
            # If we got an AI response and thinking is done
            if a_count > 0 and t_count == 0 and s_count == 0 and i > 1:
                print("  AI response received and thinking completed!")
                break
        
        screenshot(page, 'flow_6_final.png')
        
        # --- ANALYZE FINAL STATE ---
        print("\n--- Final State Analysis ---")
        
        # Check message area for AI content
        msg_list = page.locator('[data-testid="message-list"]')
        if msg_list.count() > 0:
            text = msg_list.first.inner_text()
            print(f"Message list text ({len(text)} chars): {text[:500]}")
        
        # Check input toolbar area for residual thinking indicator
        input_area = page.locator('[class*="input-container"], [class*="chat-input"]').first
        if input_area.count() > 0:
            input_text = input_area.inner_text()
            has_thinking = '思考中' in input_text
            has_model = '灵汐' in input_text or 'model' in input_text.lower()
            print(f"\nInput area: has_思考中={has_thinking}, has_model_info={has_model}")
            print(f"Input text snippet: {input_text[:200]}")
        
        # Check where thinking indicators are positioned
        for i in range(thinking.count()):
            el = thinking.nth(i)
            parent_role = el.evaluate('el => el.closest("[data-role]")?.getAttribute("data-role") || "none"')
            in_message_area = el.evaluate('el => !!el.closest("[data-testid=\\"message-list\\"]")')
            in_input = el.evaluate('el => !!el.closest("[class*=\\"input\\"]")')
            print(f"\nThinking indicator [{i}]: parent_role={parent_role} in_message_area={in_message_area} in_input={in_input}")
    
    else:
        print("[ERROR] No textarea found after creating session!")
        screenshot(page, 'flow_error_no_textarea.png')
    
    print("\nDone!")
    browser.close()
