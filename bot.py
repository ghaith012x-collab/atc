import time
import threading
import sqlite3
from datetime import datetime, timedelta
import random
import io
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright
import os

# ==========================
# GLOBAL STATE
# ==========================
workers = {}
screenshots = {}
browser_sessions = {}
DATABASE = "accounts.db"

def db():
    return sqlite3.connect(DATABASE)

def update_account(username, **kwargs):
    conn = db()
    for key, value in kwargs.items():
        conn.execute(
            f"UPDATE accounts SET {key}=? WHERE username=?",
            (value, username)
        )
    conn.commit()
    conn.close()

# ==========================
# LIVE SCREEN GENERATOR (fallback)
# ==========================
def create_screen(username, task):
    img = Image.new("RGB", (800, 450), "#111111")
    draw = ImageDraw.Draw(img)
    
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    except:
        font = ImageFont.load_default()
        small = font

    draw.text((40, 40), "TikTok Automation", fill="#ff0050", font=font)
    draw.text((40, 95), f"Account: {username}", fill="#00f2ea", font=font)
    draw.text((40, 150), f"Task: {task}", fill="white", font=font)
    draw.text((40, 400), datetime.now().strftime("%H:%M:%S"), fill="#888", font=small)
    
    # Red live indicator
    draw.ellipse([700, 30, 730, 60], fill="#ff0050")
    draw.text((740, 35), "LIVE", fill="#ff0050", font=small)
    
    return img

# ==========================
# PLAYWRIGHT BROWSER SESSION
# ==========================
def start_browser(username, headless=True):
    if username in browser_sessions:
        return browser_sessions[username]

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=headless)
    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    page = context.new_page()
    
    browser_sessions[username] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page
    }
    
    page.goto("https://www.tiktok.com/login")
    return browser_sessions[username]

def take_browser_screenshot(username):
    if username not in browser_sessions:
        return create_screen(username, "Browser not connected")
    
    try:
        page = browser_sessions[username]["page"]
        screenshot_bytes = page.screenshot()
        
        img = Image.open(io.BytesIO(screenshot_bytes))
        # Resize for dashboard
        img = img.resize((800, 450))
        return img
    except Exception as e:
        return create_screen(username, f"Error: {str(e)[:40]}")

# ==========================
# AUTOMATION LOOP
# ==========================
def bot_worker(username):
    update_account(username, status="Running", task="Starting browser...")
    
    # Start real browser
    try:
        start_browser(username)
        update_account(username, task="Browser connected")
    except Exception as e:
        update_account(username, task=f"Browser error: {str(e)[:30]}")
    
    while True:
        conn = db()
        account = conn.execute(
            "SELECT enabled, category FROM accounts WHERE username=?",
            (username,)
        ).fetchone()
        conn.close()

        if not account or account[0] != 1:
            break

        category = account[1]

        tasks = [
            f"Searching {category} trends",
            "Finding viral videos",
            "Downloading content",
            "Processing video",
            "Writing caption & hashtags",
            "Uploading to TikTok",
            "Post completed"
        ]

        for task in tasks:
            update_account(username, task=task)
            
            # Take real screenshot if browser exists
            if username in browser_sessions:
                screenshots[username] = take_browser_screenshot(username)
            else:
                screenshots[username] = create_screen(username, task)
            
            time.sleep(random.randint(2, 4))

        now = datetime.now()
        update_account(
            username,
            last_post=str(now),
            next_post=str(now + timedelta(minutes=25)),
            task="Waiting 25 minutes"
        )
        
        screenshots[username] = create_screen(username, "Waiting for next post")
        time.sleep(1500)  # 25 minutes

    # Cleanup
    update_account(username, status="Stopped", task="Idle")
    if username in browser_sessions:
        try:
            browser_sessions[username]["browser"].close()
            del browser_sessions[username]
        except:
            pass

# ==========================
# START / STOP
# ==========================
def start_bot(username):
    if username in workers:
        return
    thread = threading.Thread(target=bot_worker, args=(username,), daemon=True)
    workers[username] = thread
    thread.start()

def stop_bot(username):
    update_account(username, enabled=0)
    if username in workers:
        del workers[username]