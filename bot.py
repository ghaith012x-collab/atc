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

def start_browser_for_login(username):
    """Start browser and navigate to email login page. Returns True when ready."""
    try:
        session_dir = f"sessions/{username}"
        os.makedirs(session_dir, exist_ok=True)
        
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
        
        update_account(username, status="Connecting", current_task="Loading login page...")
        page.goto("https://www.tiktok.com/login/phone-or-email/email", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
        take_screenshot(username)
        
        # Start screenshot loop
        def screenshot_loop():
            while username in browser_sessions:
                take_screenshot(username)
                time.sleep(0.5)
        threading.Thread(target=screenshot_loop, daemon=True).start()
        
        # Verify login form is visible
        try:
            page.locator('input[name="username"], input[placeholder*="Email or username"]').first.wait_for(state="visible", timeout=10000)
            update_account(username, current_task="Login form ready")
            print(f"✓ Browser ready for {username}")
            return True
        except (TimeoutError, Exception) as e:
            print(f"Login page didn't load for {username}: {e}")
            update_account(username, current_task="Login page failed to load")
            return True  # Still return True - the session exists, credentials might still work
    except Exception as e:
        print(f"start_browser_for_login failed: {e}")
        import traceback
        traceback.print_exc()
        return False


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
        # Go directly to the email login page - skip all intermediate clicks
        page.goto("https://www.tiktok.com/login/phone-or-email/email", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(2)
        take_screenshot(username)
        
        # Verify we're on the email login form
        try:
            print("Navigated directly to email login page...")
            page.locator('input[name="username"], input[placeholder*="Email or username"]').first.wait_for(state="visible", timeout=10000)
            update_account(username, current_task="Login form ready - enter credentials")
            print("✓ Login form confirmed visible.")
            take_screenshot(username)
        except (TimeoutError, Exception) as e:
            print(f"Email login page didn't load properly: {e}")
            update_account(username, current_task="Login page failed to load - retrying...")
            # Fallback: try the main login page and click through
            try:
                page.goto("https://www.tiktok.com/login", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(3)
                take_screenshot(username)
                update_account(username, current_task="On login page - click 'Use phone/email' then 'email or username'")
            except Exception as e2:
                print(f"Fallback also failed: {e2}")
                update_account(username, current_task="Login page error - check logs")
        
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
        print(f"login_with_credentials: No browser session for {username}")
        print(f"Active sessions: {list(browser_sessions.keys())}")
        return False
    
    try:
        page = browser_sessions[username]["page"]
        print(f"login_with_credentials: Page URL = {page.url}")
        
        # Fill email/username - TikTok uses name="username" and placeholder="Email or username"
        email_input = page.locator('input[name="username"], input[placeholder*="Email or username"], input[placeholder*="email"], input[placeholder*="username"]')
        print(f"Email input count: {email_input.count()}")
        if email_input.count() > 0:
            email_input.first.click()
            email_input.first.fill(email_or_username)
            print(f"Filled email: {email_or_username}")
        else:
            print("ERROR: No email input found!")
            return False
        
        time.sleep(0.5)
        
        # Fill password
        password_input = page.locator('input[type="password"], input[placeholder*="Password"]')
        print(f"Password input count: {password_input.count()}")
        if password_input.count() > 0:
            password_input.first.click()
            password_input.first.fill(password)
            print("Filled password")
        else:
            print("ERROR: No password input found!")
            return False
        
        time.sleep(0.5)
        
        # Click login button
        login_btn = page.locator('button[data-e2e="login-button"], button:has-text("Log in"), button[type="submit"]')
        print(f"Login button count: {login_btn.count()}")
        if login_btn.count() > 0:
            login_btn.first.click()
            print("Clicked login button")
        else:
            print("WARNING: No login button found, pressing Enter instead")
            page.keyboard.press("Enter")
        
        time.sleep(3)
        take_screenshot(username)
        update_account(username, current_task="Credentials submitted - waiting...")
        return True
    except Exception as e:
        print(f"Login with credentials failed: {e}")
        import traceback
        traceback.print_exc()
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
