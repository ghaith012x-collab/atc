import os
import threading
from flask import Flask, render_template, jsonify, request, Response
from database import init_db, get_all_accounts, get_account, update_account, add_account, delete_account
from bot import (
    connect_account, start_automation, stop_automation, 
    delete_account_session, screenshots,
    click_browser, type_in_browser, press_key,
    login_with_credentials, submit_verification_code
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
    return jsonify(get_all_accounts())


@app.route("/api/add", methods=["POST"])
def add_new_account():
    data = request.json
    username = data.get("username", "").strip()
    category = data.get("category", "dance")
    
    if not username.startswith("@"):
        username = "@" + username
    
    if add_account(username, category):
        # Auto start browser connection when account is added
        def auto_connect():
            connect_account(username)
        threading.Thread(target=auto_connect, daemon=True).start()
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Account already exists"})


@app.route("/connect/<username>")
def connect(username):
    def connect_thread():
        connect_account(username)
    threading.Thread(target=connect_thread, daemon=True).start()
    return jsonify({"success": True, "message": "Connecting..."})


@app.route("/api/start/<username>", methods=["POST"])
def start(username):
    account = get_account(username)
    if account and account["connected"]:
        update_account(username, enabled=1, status="Running")
        start_automation(username)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Account not connected"})


@app.route("/api/stop/<username>", methods=["POST"])
def stop(username):
    update_account(username, enabled=0)
    stop_automation(username)
    return jsonify({"success": True})


@app.route("/api/delete/<username>", methods=["POST"])
def delete(username):
    delete_account(username)
    delete_account_session(username)
    return jsonify({"success": True})


@app.route("/live/<username>")
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


# ==================== REMOTE CONTROL ROUTES ====================
@app.route("/api/click/<path:username>", methods=["POST"])
def api_click(username):
    data = request.json
    x = data.get("x", 0)
    y = data.get("y", 0)
    success = click_browser(username, x, y)
    return jsonify({"success": success})


@app.route("/api/type/<username>", methods=["POST"])
def api_type(username):
    data = request.json
    text = data.get("text", "")
    success = type_in_browser(username, text)
    return jsonify({"success": success})


@app.route("/api/key/<username>", methods=["POST"])
def api_key(username):
    data = request.json
    key = data.get("key", "Enter")
    success = press_key(username, key)
    return jsonify({"success": success})


# ==================== FORM LOGIN ROUTES ====================
@app.route("/api/login/<username>", methods=["POST"])
def api_login(username):
    data = request.json
    email = data.get("email", "")
    password = data.get("password", "")
    
    success = login_with_credentials(username, email, password)
    return jsonify({"success": success})


@app.route("/api/verify-code/<username>", methods=["POST"])
def api_verify_code(username):
    data = request.json
    code = data.get("code", "")
    
    success = submit_verification_code(username, code)
    return jsonify({"success": success})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)