import os
import time
import threading
from datetime import datetime, timedelta
from playwright.sync_api import sync_playwright
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
        img = img.resize((800, 450))
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
        
        update_account(username, current_task="Waiting for login...")
        page.goto("https://www.tiktok.com/login", timeout=30000)
        take_screenshot(username)
        
        # Wait for user to login
        try:
            page.wait_for_selector(
                '[data-e2e="profile-icon"], [data-e2e="top-nav-profile"]',
                timeout=300000
            )
            update_account(username, connected=1, status="Connected", current_task="Session saved")
        except:
            update_account(username, status="Login timeout", current_task="Please login")
        
        # Live screenshot loop
        def screenshot_loop():
            while username in browser_sessions:
                take_screenshot(username)
                time.sleep(1)
        
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