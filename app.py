import os
import sys
import json
import traceback
import threading
from flask import Flask, render_template, jsonify, request, Response, redirect, session, url_for
from database import init_db, get_all_accounts, get_account, update_account, add_account, delete_account, get_logs, save_oauth_token, has_oauth_token
from bot import (
    connect_account, start_automation, stop_automation,
    delete_account_session, logout_account,
    screenshots, last_frame_ts, browser_sessions, take_screenshot
)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or os.urandom(32)
OAUTH_SCOPES = "https://www.googleapis.com/auth/youtube.upload"
DEFAULT_OAUTH_REDIRECT = "https://web-production-d8fdaf.up.railway.app/oauth2callback"

def oauth_redirect_uri():
    return os.environ.get("OAUTH_REDIRECT_URI") or DEFAULT_OAUTH_REDIRECT

def oauth_ready():
    return bool(os.environ.get("CLIENT_ID") and os.environ.get("CLIENT_SECRET"))

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


@app.route("/oauth2login")
def oauth2login():
    """Start phone-friendly Google OAuth. No password or verification code is handled by this app."""
    username = (request.args.get("username") or "").strip()
    account = get_account(username)
    if not account or account.get("platform") != "YouTube":
        return "Choose an existing YouTube account first.", 400
    if not oauth_ready():
        return "OAuth is not configured yet. Add CLIENT_ID and CLIENT_SECRET in Railway Variables.", 503
    import secrets, urllib.parse
    state = secrets.token_urlsafe(32)
    session["oauth_state"] = state
    session["oauth_username"] = username
    params = {"client_id": os.environ["CLIENT_ID"], "redirect_uri": oauth_redirect_uri(),
              "response_type":"code", "scope":OAUTH_SCOPES, "access_type":"offline",
              "prompt":"consent", "state":state}
    return redirect("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params))

@app.route("/oauth2callback")
def oauth2callback():
    if request.args.get("error"):
        return f"Google authorization was not completed: {request.args.get('error')}", 400
    state=request.args.get("state")
    if not state or state != session.pop("oauth_state", None):
        return "Authorization expired or invalid. Start again from the dashboard.", 400
    username=session.pop("oauth_username", None)
    code=request.args.get("code")
    if not username or not code: return "Missing authorization details.", 400
    import requests as _requests
    try:
        r=_requests.post("https://oauth2.googleapis.com/token", data={
            "code":code,"client_id":os.environ["CLIENT_ID"],"client_secret":os.environ["CLIENT_SECRET"],
            "redirect_uri":oauth_redirect_uri(),"grant_type":"authorization_code"}, timeout=20)
        r.raise_for_status(); token=r.json()
        save_oauth_token(username, token)
        update_account(username, connected=1, enabled=0, status="Google connected", current_task="Ready to start")
        return "<h2>Google connected successfully ✅</h2><p>You can return to the ATC dashboard. Your authorization is stored privately.</p><p><a href='/'>Back to dashboard</a></p>"
    except Exception as e:
        print(f"[oauth] token exchange failed: {e}", flush=True)
        return "Google authorization could not be completed. Check the Railway logs and OAuth redirect URI.", 502

@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/")
def dashboard():
    accounts = get_all_accounts()
    for acc in accounts:
        if acc.get("platform") == "YouTube":
            acc["oauth_connected"] = has_oauth_token(acc.get("username"))
    return render_template("site.html", accounts=accounts)


@app.route("/api/accounts")
def api_accounts():
    # Never send session_data to frontend
    accounts = get_all_accounts()
    for acc in accounts:
        if acc.get("platform") == "YouTube":
            acc["oauth_connected"] = has_oauth_token(acc.get("username"))
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
        # Store optional settings if provided.
        kwargs = {}
        login_method = (data.get("login_method") or "cookie").strip()
        if login_method:
            kwargs["login_method"] = login_method
        profile_link = (data.get("profile_link") or "").strip()
        if profile_link:
            kwargs["profile_link"] = profile_link
        if kwargs:
            update_account(username, **kwargs)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Account already exists"})


@app.route("/api/session/<path:username>", methods=["POST"])
def save_session(username):
    account = get_account(username)
    if account and account.get("platform") == "YouTube":
        return jsonify({"success": False, "error": "YouTube uses Google OAuth; cookie sessions are disabled."}), 403
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
        kwargs = dict(session_data=session_json, status="Session saved", current_task="Ready to connect")
        login_method = (data.get("login_method") or "cookie").strip()
        if login_method:
            kwargs.update(login_method=login_method)
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
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    if account.get("platform") == "YouTube":
        if not has_oauth_token(username):
            return jsonify({"success": False, "error": "Connect Google / YouTube first"}), 403
    elif not account.get("connected"):
        return jsonify({"success": False, "error": "Account not connected"}), 403
    update_account(username, enabled=1, connected=1, status="Running", current_task="Starting automation...")
    start_automation(username)
    return jsonify({"success": True})


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


# Cache the last encoded JPEG per account so we only re-encode when the frame
# actually changes. Re-encoding a 1280x720 JPEG on EVERY poll (multiple accounts
# × ~1s) was the main source of dashboard lag.
_live_cache = {}  # username -> (frame_ts, jpeg_bytes)


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

    frame_ts = last_frame_ts.get(username)
    cached = _live_cache.get(username)
    if cached is not None and cached[0] == frame_ts and frame_ts is not None:
        jpeg = cached[1]
    else:
        buffer = BytesIO()
        # High quality so the preview stays sharp.
        img.save(buffer, "JPEG", quality=95)
        jpeg = buffer.getvalue()
        _live_cache[username] = (frame_ts, jpeg)

    resp = Response(jpeg, mimetype="image/jpeg")
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
    # threaded=True: handle the dashboard's many concurrent pollers (/live,
    # /api/live_meta, /api/accounts) in parallel instead of serializing them on a
    # single worker — this was the main cause of the "site lag / not loading".
    # use_reloader=False: avoid the dev-server watchdog double-init overhead.
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
