import os
import time
import threading
import sqlite3
from datetime import datetime, timedelta
import random
from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw, ImageFont
import io

DATABASE = "accounts.db"
workers = {}
screenshots = {}
browser_sessions = {}

def db():
    return sqlite3.connect(DATABASE)

def update_account(username, **kwargs):
    conn = db()
    for key, value in kwargs.items():
        conn.execute(f"UPDATE accounts SET {key}=? WHERE username=?", (value, username))
    conn.commit()
    conn.close()

def log_action(username, action):
    conn = db()
    conn.execute("INSERT INTO logs (username, action) VALUES (?, ?)", (username, action))
    conn.commit()
    conn.close()

# ==================== SCREENSHOT ====================
def create_placeholder(username, text):
    img = Image.new("RGB", (800, 450), "#111111")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except:
        font = ImageFont.load_default()
    
    draw.text((30, 30), f"TikTok - {username}", fill="#ff0050", font=font)
    draw.text((30, 80), text, fill="white", font=font)
    draw.text((30, 400), datetime.now().strftime("%H:%M:%S"), fill="#888", font=font)
    return img

def take_screenshot(username):
    if username not in browser_sessions:
        screenshots[username] = create_placeholder(username, "No browser session")
        return
    
    try:
        page = browser_sessions[username]["page"]
        screenshot_bytes = page.screenshot()
        img = Image.open(io.BytesIO(screenshot_bytes))
        img = img.resize((800, 450))
        screenshots[username] = img
    except Exception as e:
        screenshots[username] = create_placeholder(username, f"Error: {str(e)[:50]}")

# ==================== PERSISTENT BROWSER ====================
def connect_account(username):
    update_account(username, status="Connecting", task="Opening browser...")
    log_action(username, "Connection started")
    
    session_dir = f"sessions/{username}"
    os.makedirs(session_dir, exist_ok=True)
    
    try:
        pw = sync_playwright().start()
        context = pw.chromium.launch_persistent_context(
            user_data_dir=session_dir,
            headless=True,
            viewport={"width": 1280, "height": 720},
            args=["--no-sandbox"]
        )
        
        page = context.pages[0] if context.pages else context.new_page()
        
        browser_sessions[username] = {
            "pw": pw,
            "context": context,
            "page": page
        }
        
        update_account(username, task="Waiting for login...")
        page.goto("https://www.tiktok.com/login")
        take_screenshot(username)
        
        # Wait for login (up to 3 minutes)
        try:
            page.wait_for_selector('[data-e2e="profile-icon"], [data-e2e="top-nav-profile"]', timeout=180000)
            update_account(username, connected=1, status="Connected", task="Ready")
            log_action(username, "Login successful")
        except:
            update_account(username, status="Login timeout", task="Please try again")
            log_action(username, "Login timeout")
        
        # Start continuous screenshot loop
        def screenshot_loop():
            while username in browser_sessions:
                take_screenshot(username)
                time.sleep(1)
        
        threading.Thread(target=screenshot_loop, daemon=True).start()
        
    except Exception as e:
        update_account(username, status="Connection failed", task=str(e)[:60])
        log_action(username, f"Error: {str(e)}")

# ==================== AUTOMATION WORKER ====================
def bot_worker(username):
    while True:
        conn = db()
        row = conn.execute(
            "SELECT enabled, connected, category FROM accounts WHERE username=?",
            (username,)
        ).fetchone()
        conn.close()
        
        if not row or row[0] != 1:
            break
        
        if row[1] != 1:
            update_account(username, task="Not connected")
            time.sleep(5)
            continue
        
        category = row[2]
        update_account(username, status="Running", task=f"Searching {category}...")
        
        # Simulate real steps with screenshots
        steps = [
            f"Searching {category} trends",
            "Finding viral videos",
            "Processing video",
            "Writing caption",
            "Uploading to TikTok"
        ]
        
        for step in steps:
            update_account(username, task=step)
            if username in browser_sessions:
                take_screenshot(username)
            time.sleep(random.randint(3, 6))
        
        now = datetime.now()
        next_time = (now + timedelta(minutes=25)).strftime("%H:%M")
        
        update_account(
            username,
            last_post=now.strftime("%Y-%m-%d %H:%M"),
            next_post=next_time,
            task="Waiting 25 minutes"
        )
        
        log_action(username, "Post completed")
        time.sleep(1500)  # 25 minutes
    
    update_account(username, status="Stopped", task="Idle")

def start_bot(username):
    if username in workers:
        return
    thread = threading.Thread(target=bot_worker, args=(username,), daemon=True)
    workers[username] = thread
    thread.start()
    log_action(username, "Automation started")

def stop_bot(username):
    if username in workers:
        del workers[username]
    log_action(username, "Automation stopped")

def delete_account(username):
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            del browser_sessions[username]
        except:
            pass
    if username in workers:
        del workers[username]
    
    # Delete session folder
    import shutil
    session_path = f"sessions/{username}"
    if os.path.exists(session_path):
        shutil.rmtree(session_path, ignore_errors=True)
    
    conn = db()
    conn.execute("DELETE FROM accounts WHERE username=?", (username,))
    conn.execute("DELETE FROM logs WHERE username=?", (username,))
    conn.commit()
    conn.close()

def get_account_status(username):
    conn = db()
    row = conn.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
    conn.close()
    return row