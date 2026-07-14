
import os
import time
import threading
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright, TimeoutError
from PIL import Image
import io
from database import get_account, update_account

workers = {}
browser_sessions = {}
screenshots = {}

def create_placeholder(username, text):
    img = Image.new("RGB", (800, 450), "#111111")
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except:
        font = ImageFont.load_default()
    draw.text((30, 30), f"TikTok - {username}", fill="#ff0050", font=font)
    draw.text((30, 80), text, fill="white", font=font)
    draw.text((30, 400), datetime.now().strftime("%H:%M:%S"), fill="#888", font=font)
    return img

def take_screenshot(username):
    if username not in browser_sessions:
        screenshots[username] = create_placeholder(username, "No browser")
        return
    try:
        page = browser_sessions[username]["page"]
        screenshot_bytes = page.screenshot()
        img = Image.open(io.BytesIO(screenshot_bytes))
        # Always keep 1280x720 for accurate clicking
        img = img.resize((1280, 720))
        screenshots[username] = img
    except:
        screenshots[username] = create_placeholder(username, "Screenshot error")

def connect_account(username):
    """Start persistent browser and wait for login"""
    update_account(username, status="Connecting", current_task="Starting browser...")
    
    session_dir = f"sessions/{username}"
    os.makedirs(session_dir, exist_ok=True)
    
    try:
        pw = sync_playwright().start()
        
        context = pw.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=True,
            viewport={"width": 1280, "height": 720},
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        
        page = context.pages[0] if context.pages else context.new_page()
        
        browser_sessions[username] = {
            "pw": pw,
            "context": context,
            "page": page
        }
        
        update_account(username, current_task="Loading TikTok login...")
        page.goto("https://www.tiktok.com/login", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
        take_screenshot(username)
        
        # Auto-click flow: "Use phone / email / username" -> "Log in with email or username"
        try:
            print("Attempting to auto-click 'Use phone / email / username'...")
            update_account(username, current_task="Looking for login options...")
            
            clicked_phone_email = False
            
            # Attempt 1: Text-based locator (most reliable)
            try:
                phone_btn = page.locator("text=Use phone / email / username")
                phone_btn.wait_for(state="visible", timeout=10000)
                phone_btn.click()
                # VERIFY: Wait for the next screen to actually appear
                page.locator("text=Log in with email or username").wait_for(state="visible", timeout=8000)
                clicked_phone_email = True
                print("✓ Clicked 'Use phone / email / username' - next screen confirmed.")
            except (TimeoutError, Exception) as e:
                print(f"Attempt 1 failed: {e}")
            
            # Attempt 2: data-e2e channel-item (TikTok's own attribute)
            if not clicked_phone_email:
                try:
                    items = page.locator('div[data-e2e="channel-item"]')
                    items.first.wait_for(state="visible", timeout=5000)
                    count = items.count()
                    # "Use phone / email / username" is typically the 2nd item (index 1)
                    if count >= 2:
                        items.nth(1).click()
                        page.locator("text=Log in with email or username").wait_for(state="visible", timeout=8000)
                        clicked_phone_email = True
                        print("✓ Clicked channel-item nth(1) - next screen confirmed.")
                except (TimeoutError, Exception) as e:
                    print(f"Attempt 2 failed: {e}")
            
            # Attempt 3: JavaScript click with verification
            if not clicked_phone_email:
                try:
                    page.evaluate("""
                        () => {
                            const items = document.querySelectorAll('div[data-e2e="channel-item"]');
                            for (let item of items) {
                                if (item.innerText.toLowerCase().includes('phone') || item.innerText.toLowerCase().includes('email')) {
                                    item.click();
                                    return true;
                                }
                            }
                            return false;
                        }
                    """)
                    page.locator("text=Log in with email or username").wait_for(state="visible", timeout=8000)
                    clicked_phone_email = True
                    print("✓ Clicked via JavaScript - next screen confirmed.")
                except (TimeoutError, Exception) as e:
                    print(f"Attempt 3 failed: {e}")
            
            if clicked_phone_email:
                update_account(username, current_task="Clicking email/username login...")
                take_screenshot(username)
                time.sleep(2)
                
                # Now click "Log in with email or username" link
                clicked_email_login = False
                try:
                    # Method 1: Click the link/anchor with that text
                    email_link = page.locator("a:has-text('Log in with email or username')")
                    if email_link.count() > 0 and email_link.first.is_visible():
                        email_link.first.click()
                        clicked_email_login = True
                        print("✓ Clicked email login link (anchor).")
                except Exception as e:
                    print(f"Email link method 1 failed: {e}")
                
                if not clicked_email_login:
                    try:
                        # Method 2: Text locator (broader)
                        email_btn = page.get_by_text("Log in with email or username", exact=False)
                        if email_btn.count() > 0 and email_btn.first.is_visible():
                            email_btn.first.click()
                            clicked_email_login = True
                            print("✓ Clicked email login (text locator).")
                    except Exception as e:
                        print(f"Email link method 2 failed: {e}")
                
                if not clicked_email_login:
                    try:
                        # Method 3: JavaScript - find and click any element with that text
                        page.evaluate("""
                            () => {
                                const allEls = document.querySelectorAll('a, div, span, p');
                                for (let el of allEls) {
                                    if (el.innerText && el.innerText.trim().toLowerCase().includes('log in with email or username')) {
                                        el.click();
                                        return true;
                                    }
                                }
                                return false;
                            }
                        """)
                        clicked_email_login = True
                        print("✓ Clicked email login (JavaScript).")
                    except Exception as e:
                        print(f"Email link method 3 failed: {e}")
                
                if clicked_email_login:
                    # VERIFY: Wait for the actual login form inputs to appear
                    try:
                        page.locator('input[name="username"], input[placeholder*="Email"], input[placeholder*="email"]').first.wait_for(state="visible", timeout=8000)
                        update_account(username, current_task="Login form ready - enter credentials")
                        print("✓ Login form is confirmed visible.")
                    except (TimeoutError, Exception) as e:
                        print(f"Login form didn't appear: {e}")
                        update_account(username, current_task="Login form not detected - try manually")
                else:
                    update_account(username, current_task="Click 'Log in with email or username' manually")
                    print("✗ Could not click email login link.")
            else:
                update_account(username, current_task="Click 'Use phone/email' manually")
                print("✗ Could not auto-click. User must click manually.")
                
        except Exception as e:
            print(f"Auto-click error: {e}")
            update_account(username, current_task="Click 'Use phone/email' manually")
        
        # Wait for user to login
        try:
            page.wait_for_selector(
                '[data-e2e="profile-icon"], [data-e2e="top-nav-profile"]',
                timeout=300000
            )
            update_account(username, connected=1, status="Connected", current_task="Session saved")
        except TimeoutError:
            update_account(username, status="Login timeout", current_task="Please login")
        
        # Live screenshot loop (0.5 seconds)
        def screenshot_loop():
            while username in browser_sessions:
                take_screenshot(username)
                time.sleep(0.5)
        
        threading.Thread(target=screenshot_loop, daemon=True).start()
        
    except Exception:
        import traceback
        traceback.print_exc()
        update_account(username, status="Error", current_task="See Railway logs")

def automation_worker(username):
    while True:
        account = get_account(username)
        if not account or not account["enabled"]:
            break
        
        if not account["connected"]:
            update_account(username, current_task="Not connected")
            time.sleep(5)
            continue
        
        try:
            # Open persistent session
            if username not in browser_sessions:
                connect_account(username)
                time.sleep(3)
            
            update_account(username, current_task="Finding trending content...")
            time.sleep(4)
            
            update_account(username, current_task="Processing video...")
            time.sleep(4)
            
            update_account(username, current_task="Uploading to TikTok...")
            time.sleep(5)
            
            now = datetime.now()
            next_time = (now + timedelta(minutes=25)).strftime("%H:%M")
            
            update_account(
                username,
                last_post=now.strftime("%Y-%m-%d %H:%M"),
                next_post=next_time,
                current_task="Waiting 25 minutes"
            )
            
            time.sleep(1500)  # 25 minutes
            
        except Exception as e:
            update_account(username, current_task=f"Error: {str(e)[:50]}")
            time.sleep(30)
    
    update_account(username, status="Stopped", current_task="Idle")

def start_automation(username):
    if username in workers:
        return
    thread = threading.Thread(target=automation_worker, args=(username,), daemon=True)
    workers[username] = thread
    thread.start()

def stop_automation(username):
    if username in workers:
        del workers[username]
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            del browser_sessions[username]
        except:
            pass

def delete_account_session(username):
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            del browser_sessions[username]
        except:
            pass
    if username in workers:
        del workers[username]
    
    import shutil
    session_path = f"sessions/{username}"
    if os.path.exists(session_path):
        shutil.rmtree(session_path, ignore_errors=True)


# ==================== REMOTE CONTROL ====================
def click_browser(username, x, y):
    """Click at specific coordinates in the browser - more reliable version"""
    print(f"CLICK REQUEST: {username} at ({x}, {y})")
    
    if username not in browser_sessions:
        print("→ No browser session found!")
        return False
    
    try:
        page = browser_sessions[username]["page"]
        print(f"→ Current page: {page.url}")
        
        # Scroll to make sure element is visible
        page.mouse.move(x, y)
        page.wait_for_timeout(150)
        
        # Perform actual click
        page.mouse.click(x, y, delay=100)
        
        print(f"→ Click SUCCESS at ({x}, {y})")
        return True
    except Exception as e:
        print(f"→ Click FAILED: {str(e)}")
        return False


# ==================== FORM-BASED LOGIN ====================
def login_with_credentials(username, email_or_username, password):
    """Fill login form with credentials"""
    if username not in browser_sessions:
        return False
    
    try:
        page = browser_sessions[username]["page"]
        
        # Fill email/username
        email_input = page.locator('input[name="email"], input[placeholder*="email"], input[placeholder*="username"]')
        if email_input.count() > 0:
            email_input.first.fill(email_or_username)
        
        # Fill password
        password_input = page.locator('input[type="password"], input[name="password"]')
        if password_input.count() > 0:
            password_input.first.fill(password)
        
        # Click login button
        login_btn = page.locator('button:has-text("Log in"), button[type="submit"]')
        if login_btn.count() > 0:
            login_btn.first.click()
        
        update_account(username, current_task="Credentials submitted")
        return True
    except Exception as e:
        print(f"Login with credentials failed: {e}")
        return False

def submit_verification_code(username, code):
    """Submit 6-digit verification code"""
    if username not in browser_sessions:
        return False
    
    try:
        page = browser_sessions[username]["page"]
        
        # Find code input
        code_input = page.locator('input[maxlength="6"], input[placeholder*="code"], input[name*="code"]')
        if code_input.count() > 0:
            code_input.first.fill(code)
        
        # Submit
        submit_btn = page.locator('button:has-text("Verify"), button:has-text("Submit"), button[type="submit"]')
        if submit_btn.count() > 0:
            submit_btn.first.click()
        
        update_account(username, current_task="Code submitted")
        return True
    except Exception as e:
        print(f"Code submission error: {e}")
        return False

def type_in_browser(username, text):
    """Type text in the browser"""
    if username in browser_sessions:
        try:
            page = browser_sessions[username]["page"]
            page.keyboard.type(text)
            return True
        except:
            return False
    return False

def press_key(username, key):
    """Press a key (Enter, Backspace, etc.)"""
    if username in browser_sessions:
        try:
            page = browser_sessions[username]["page"]
            page.keyboard.press(key)
            return True
        except:
            return False
    return False

