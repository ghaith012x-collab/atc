import os
import json
import threading
from flask import Flask, render_template, jsonify, request, Response
from database import init_db, get_all_accounts, get_account, update_account, add_account, delete_account
from bot import (
    connect_account, start_automation, stop_automation, 
    delete_account_session, screenshots, browser_sessions
)

app = Flask(__name__)

# Auto install Chromium on Railway
def install_browser():
    try:
        import subprocess
        subprocess.run(["playwright", "install", "chromium"], check=True, capture_output=True)
        print("✓ Chromium installed")
    except Exception as e:
        print(f"Browser install note: {e}")

install_browser()
init_db()


@app.route("/")
def dashboard():
    accounts = get_all_accounts()
    return render_template("site.html", accounts=accounts)


@app.route("/api/accounts")
def api_accounts():
    # Never send session_data to frontend
    accounts = get_all_accounts()
    for acc in accounts:
        if "session_data" in acc:
            del acc["session_data"]
    return jsonify(accounts)


@app.route("/api/add", methods=["POST"])
def add_new_account():
    data = request.json
    username = data.get("username", "").strip()
    category = data.get("category", "dance")
    
    if not username.startswith("@"):
        username = "@" + username
    
    if add_account(username, category):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Account already exists"})


@app.route("/api/session/<path:username>", methods=["POST"])
def save_session(username):
    data = request.json
    session_json = data.get("session", "").strip()
    
    if not session_json:
        return jsonify({"success": False, "error": "No session data provided"})
        
    try:
        cookies = json.loads(session_json)
        if not isinstance(cookies, list):
            return jsonify({"success": False, "error": "Session must be a JSON array of cookies"})
            
        # Basic validation that it looks like Playwright/EditThisCookie format
        has_name = any("name" in c for c in cookies)
        has_value = any("value" in c for c in cookies)
        
        if not (has_name and has_value):
            return jsonify({"success": False, "error": "Invalid cookie format. Expected array of {name, value, domain} objects."})
            
        # Save to DB
        update_account(username, session_data=session_json, status="Session saved", current_task="Ready to connect")
        
        # Connect to verify
        def connect_thread():
            connect_account(username)
        threading.Thread(target=connect_thread, daemon=True).start()
        
        return jsonify({"success": True, "message": "Session saved and verifying..."})
        
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "Invalid JSON format"})


@app.route("/api/start/<path:username>", methods=["POST"])
def start(username):
    account = get_account(username)
    if account and account["connected"]:
        update_account(username, enabled=1, status="Running")
        start_automation(username)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Account not connected"})


@app.route("/api/stop/<path:username>", methods=["POST"])
def stop(username):
    update_account(username, enabled=0)
    stop_automation(username)
    return jsonify({"success": True})


@app.route("/api/delete/<path:username>", methods=["POST"])
def delete(username):
    delete_account(username)
    delete_account_session(username)
    return jsonify({"success": True})


@app.route("/api/delete_session/<path:username>", methods=["POST"])
def api_delete_session(username):
    delete_account_session(username)
    return jsonify({"success": True})


@app.route("/live/<path:username>")
def live(username):
    if username not in screenshots:
        from PIL import Image
        img = Image.new("RGB", (800, 450), "#111111")
        screenshots[username] = img
    
    from io import BytesIO
    buffer = BytesIO()
    screenshots[username].save(buffer, "JPEG", quality=80)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype="image/jpeg")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
