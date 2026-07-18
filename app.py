import os
import sys
import json
import traceback
import threading
from flask import Flask, render_template, jsonify, request, Response
from database import init_db, get_all_accounts, get_account, update_account, add_account, delete_account, get_logs
from bot import (
    connect_account, start_automation, stop_automation,
    delete_account_session, logout_account,
    screenshots, last_frame_ts, browser_sessions, take_screenshot
)

app = Flask(__name__)

# Auto install Chromium on Railway (non-fatal — never block startup).
def install_browser():
    try:
        import subprocess
        subprocess.run(["playwright", "install", "chromium"], check=True, capture_output=True)
        print("✓ Chromium installed", flush=True)
    except Exception as e:
        print(f"Browser install note: {e}", flush=True)

try:
    install_browser()
    init_db()
    print("✓ App startup init complete", flush=True)
except Exception as e:
    # Print the FULL traceback so Railway logs show the real launch error
    # instead of a silent "application failed to respond".
    print("!!! STARTUP ERROR !!!", flush=True)
    traceback.print_exc()


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


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
    platform = data.get("platform", "TikTok")
    if platform not in ("TikTok", "YouTube"):
        platform = "TikTok"

    if not username.startswith("@"):
        username = "@" + username

    if add_account(username, category, platform):
        # Store optional Google login credentials if provided.
        email = (data.get("email") or "").strip()
        password = (data.get("password") or "").strip()
        login_method = (data.get("login_method") or "cookie").strip()
        if email or password:
            update_account(username, email=email, password=password, login_method=login_method)
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
        email = (data.get("email") or "").strip()
        password = (data.get("password") or "").strip()
        kwargs = dict(session_data=session_json, status="Session saved", current_task="Ready to connect")
        if email or password:
            kwargs.update(email=email, password=password, login_method=(data.get("login_method") or "cookie").strip())
        update_account(username, **kwargs)
        
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
        update_account(username, enabled=1, status="Running", current_task="Starting automation...")
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


@app.route("/api/logout/<path:username>", methods=["POST"])
def api_logout(username):
    logout_account(username)
    return jsonify({"success": True})


@app.route("/api/logs/<path:username>")
def api_logs(username):
    logs = get_logs(username)
    return jsonify({"success": True, "username": username, "logs": logs or ""})


@app.route("/api/verify_code/<path:username>", methods=["POST"])
def api_verify_code(username):
    import re as _re
    data = request.json or {}
    digits = _re.sub(r"\D", "", (data.get("code") or "").strip())
    if not digits:
        return jsonify({"success": False, "error": "Enter the code digits"})
    update_account(username, verify_code=digits)
    return jsonify({"success": True, "digits": digits})


@app.route("/live/<path:username>")
def live(username):
    # NOTE: we NEVER call Playwright from this Flask thread — doing so triggers
    # "cannot switch to a different thread" greenlet errors. The preview frame is
    # captured by the worker/connect thread (the browser's owner thread) and
    # stored in `screenshots` as a PIL Image; here we just encode it to JPEG.
    from PIL import Image
    from io import BytesIO

    img = screenshots.get(username)
    if not isinstance(img, Image.Image):
        img = Image.new("RGB", (1280, 720), "#111111")
        screenshots[username] = img

    # Defensive: if a raw screenshot (bytes) was ever stored, wrap it in a PIL Image.
    if isinstance(img, (bytes, bytearray)):
        try:
            img = Image.open(BytesIO(img)).convert("RGB")
        except Exception:
            img = Image.new("RGB", (1280, 720), "#111111")
        screenshots[username] = img

    buffer = BytesIO()
    # High quality so the preview stays sharp.
    img.save(buffer, "JPEG", quality=95)
    buffer.seek(0)
    resp = Response(buffer.getvalue(), mimetype="image/jpeg")
    # Never let any proxy/browser cache the frame — otherwise the cam freezes.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/live_meta/<path:username>")
def live_meta(username):
    """Return the timestamp (epoch seconds) of the last captured frame so the
    frontend can show 'updated Ns ago' on the live cam."""
    import time as _time
    ts = last_frame_ts.get(username)
    return jsonify({"username": username, "ts": ts, "now": _time.time()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
