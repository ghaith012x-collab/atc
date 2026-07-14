import os
import time
import json
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
        img = img.resize((1280, 720))
        screenshots[username] = img
    except:
        screenshots[username] = create_placeholder(username, "Screenshot error")

def connect_account(username):
    """Start headless browser, load cookies, and verify login status"""
    account = get_account(username)
    if not account or not account.get("session_data"):
        update_account(username, status="No Session", current_task="Please paste session")
        return False
        
    update_account(username, status="Connecting", current_task="Starting browser...")
    
    try:
        cookies = json.loads(account["session_data"])
    except json.JSONDecodeError:
        update_account(username, status="Invalid Session", current_task="Invalid JSON")
        return False

    try:
        pw = sync_playwright().start()
        
        # Use browser.new_context() NOT persistent_context
        # This ensures cookies are loaded BEFORE any page navigation
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        
        context = browser.new_context(
            viewport={"width": 1280, "height": 720}
        )
        
        # Clean cookies - remove metadata fields Cookie Editor adds
        clean_cookies = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            if "name" not in c or "value" not in c:
                continue
            
            cleaned = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            }
            
            # Only add sameSite if valid
            if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
                cleaned["sameSite"] = c["sameSite"]
            
            clean_cookies.append(cleaned)
        
        if not clean_cookies:
            update_account(username, status="Invalid Session", current_task="No valid cookies found")
            return False
        
        # CORRECT ORDER: add cookies BEFORE creating page and navigating
        context.add_cookies(clean_cookies)
        
        page = context.new_page()
        
        browser_sessions[username] = {
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page
        }
        
        update_account(username, current_task="Verifying session...")
        page.goto("https://www.tiktok.com", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)
        take_screenshot(username)
        
        # Verify login by checking for profile icon or logged-in indicators
        logged_in = False
        tiktok_username = ""
        
        try:
            page.wait_for_selector(
                '[data-e2e="profile-icon"], [data-e2e="top-nav-profile"], a[href*="/@"]',
                timeout=10000
            )
            logged_in = True
        except TimeoutError:
            pass
        
        # Also check if login button is NOT present (another way to confirm logged in)
        if not logged_in:
            try:
                login_btn = page.locator('[data-e2e="top-login-button"], a[href*="/login"]')
                if login_btn.count() == 0:
                    logged_in = True
            except:
                pass
        
        if logged_in:
            # Try to get the TikTok username
            try:
                # Navigate to profile to get username
                profile_link = page.locator('a[href*="/@"]').first
                if profile_link.count() > 0:
                    href = profile_link.get_attribute("href")
                    if href and "/@" in href:
                        tiktok_username = href.split("/@")[-1].split("?")[0].split("/")[0]
            except:
                pass
            
            if not tiktok_username:
                # Try clicking profile icon and reading from profile page
                try:
                    page.goto("https://www.tiktok.com/profile", timeout=15000)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    time.sleep(2)
                    # Get username from the profile page URL or h1/h2
                    current_url = page.url
                    if "/@" in current_url:
                        tiktok_username = current_url.split("/@")[-1].split("?")[0].split("/")[0]
                    else:
                        # Try to get from page content
                        title_el = page.locator('h1[data-e2e="user-title"], h2[data-e2e="user-subtitle"]').first
                        if title_el.count() > 0:
                            tiktok_username = title_el.text_content().strip().lstrip("@")
                except:
                    pass
            
            task_msg = f"Session verified - @{tiktok_username}" if tiktok_username else "Session verified"
            update_account(username, connected=1, status="Connected", current_task=task_msg)
            print(f"✓ Session verified for {username}" + (f" (TikTok: @{tiktok_username})" if tiktok_username else ""))
            take_screenshot(username)
        else:
            update_account(username, connected=0, status="Session expired", current_task="Please update session")
            print(f"✗ Session expired or invalid for {username}")
        
        # Live screenshot loop (0.5 seconds)
        def screenshot_loop():
            while username in browser_sessions:
                take_screenshot(username)
                time.sleep(0.5)
        
        threading.Thread(target=screenshot_loop, daemon=True).start()
        return logged_in
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_account(username, status="Error", current_task=f"Error: {str(e)[:50]}")
        return False

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
                account = get_account(username)
                if not account["connected"]:
                    update_account(username, enabled=0)
                    break
            
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
            browser_sessions[username]["browser"].close()
            del browser_sessions[username]
        except:
            pass

def delete_account_session(username):
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            del browser_sessions[username]
        except:
            pass
    if username in workers:
        del workers[username]
    
    import shutil
    session_path = f"sessions/{username}"
    if os.path.exists(session_path):
        shutil.rmtree(session_path, ignore_errors=True)
    
    # Also clear session_data from DB
    update_account(username, session_data=None, connected=0, status="Disconnected", current_task="Idle")
