from flask import Flask, render_template, jsonify, request, Response
import threading
import time
import random
from datetime import datetime, timedelta
import os
from PIL import Image, ImageDraw, ImageFont
import io

app = Flask(__name__)

# In-memory state
accounts = {
    "@coolcreator1": {
        "username": "@coolcreator1",
        "status": "stopped",
        "current_task": "Idle",
        "last_post": None,
        "next_post": None,
        "enabled": False,
        "worker_thread": None
    },
    "@trendsetter99": {
        "username": "@trendsetter99",
        "status": "stopped",
        "current_task": "Idle",
        "last_post": None,
        "next_post": None,
        "enabled": False,
        "worker_thread": None
    }
}

# Store latest live screenshots per account
screenshots = {}

categories = ["dance", "comedy", "gaming", "food", "fashion", "tech"]

def create_live_screenshot(username, task_text):
    """Generate a realistic browser screenshot"""
    width, height = 640, 360
    img = Image.new('RGB', (width, height), color=(17, 17, 17))
    draw = ImageDraw.Draw(img)
    
    # Try to use a basic font
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except:
        font = ImageFont.load_default()
        small_font = font
    
    # Browser chrome
    draw.rectangle([0, 0, width, 28], fill=(30, 30, 30))
    draw.text((15, 6), "TikTok - Chrome", fill=(200, 200, 200), font=small_font)
    
    # Main content area
    draw.rectangle([0, 28, width, height], fill=(20, 20, 20))
    
    # Status text
    draw.text((20, 50), f"Account: {username}", fill=(255, 255, 255), font=font)
    draw.text((20, 85), f"Task: {task_text}", fill=(0, 200, 255), font=font)
    
    # Fake browser elements
    draw.rectangle([20, 130, 620, 200], fill=(40, 40, 40), outline=(60, 60, 60))
    draw.text((30, 145), "https://www.tiktok.com/upload", fill=(100, 200, 255), font=small_font)
    
    # Red recording dot
    draw.ellipse([560, 50, 580, 70], fill=(255, 0, 80))
    draw.text((590, 52), "LIVE", fill=(255, 0, 80), font=small_font)
    
    # Timestamp
    draw.text((20, 320), datetime.now().strftime("%H:%M:%S"), fill=(150, 150, 150), font=small_font)
    
    return img

def get_screenshot_bytes(username):
    """Return the latest screenshot for an account"""
    if username not in screenshots:
        # Create initial screenshot
        img = create_live_screenshot(username, "Waiting to start...")
        screenshots[username] = img
    
    buf = io.BytesIO()
    screenshots[username].save(buf, format='JPEG', quality=85)
    buf.seek(0)
    return buf.getvalue()

def automation_worker(username):
    account = accounts[username]
    
    while account["enabled"]:
        try:
            # Step 1
            account["current_task"] = "Starting browser session..."
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            time.sleep(1)
            
            # Step 2
            category = random.choice(categories)
            account["current_task"] = f"Monitoring {category} category..."
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            time.sleep(2)
            
            # Step 3
            account["current_task"] = "Finding trending content..."
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            time.sleep(2)
            
            # Step 4
            account["current_task"] = "Processing video..."
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            time.sleep(3)
            
            # Step 5
            account["current_task"] = "Preparing title, caption & hashtags..."
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            time.sleep(1)
            
            # Step 6
            account["current_task"] = "Uploading video..."
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            time.sleep(4)
            
            # Step 7
            account["last_post"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            account["current_task"] = "Post successful!"
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            account["status"] = "Running"
            
            # Step 8 - wait 25 minutes
            account["next_post"] = (datetime.now() + timedelta(minutes=25)).strftime("%H:%M")
            account["current_task"] = "Waiting for next cycle..."
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            
            time.sleep(1500)
            
        except Exception as e:
            account["current_task"] = f"Error occurred"
            screenshots[username] = create_live_screenshot(username, account["current_task"])
            time.sleep(30)
    
    account["status"] = "stopped"
    account["current_task"] = "Idle"
    account["next_post"] = None
    if username in screenshots:
        screenshots[username] = create_live_screenshot(username, "Automation stopped")

@app.route('/')
def dashboard():
    return render_template('site.html', accounts=accounts)

@app.route('/api/accounts')
def get_accounts():
    clean = {}
    for username, acc in accounts.items():
        clean_acc = acc.copy()
        clean_acc.pop('worker_thread', None)
        clean[username] = clean_acc
    return jsonify(clean)

@app.route('/api/start/<username>', methods=['POST'])
def start_automation(username):
    if username not in accounts:
        return jsonify({"error": "Account not found"}), 404
    
    account = accounts[username]
    if not account["enabled"]:
        account["enabled"] = True
        account["status"] = "Running"
        account["current_task"] = "Starting automation..."
        
        thread = threading.Thread(target=automation_worker, args=(username,), daemon=True)
        account["worker_thread"] = thread
        thread.start()
    
    return jsonify({"success": True})

@app.route('/api/stop/<username>', methods=['POST'])
def stop_automation(username):
    if username not in accounts:
        return jsonify({"error": "Account not found"}), 404
    
    account = accounts[username]
    account["enabled"] = False
    account["status"] = "stopped"
    account["current_task"] = "Stopping..."
    
    return jsonify({"success": True})

@app.route('/live/<username>')
def live_screenshot(username):
    if username not in accounts:
        return "Not found", 404
    
    img_bytes = get_screenshot_bytes(username)
    return Response(img_bytes, mimetype='image/jpeg')

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print("TikTok Auto Poster started on Railway")
    print(f"Dashboard: http://0.0.0.0:{port}")
    app.run(host='0.0.0.0', port=port, debug=False)