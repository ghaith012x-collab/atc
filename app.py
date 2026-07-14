import os
import requests
from flask import Flask, render_template, redirect, request, jsonify, url_for
from database import init_db, get_all_accounts, get_account, update_account, add_account, delete_account
from bot import start_automation, stop_automation
import urllib.parse

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key")

# TikTok OAuth Config
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET")
TIKTOK_REDIRECT_URI = os.environ.get("TIKTOK_REDIRECT_URI", "https://your-app.railway.app/callback/tiktok")

TIKTOK_AUTH_URL = "https://open-api.tiktok.com/platform/oauth/connect/"
TIKTOK_TOKEN_URL = "https://open-api.tiktok.com/oauth/access_token/"

init_db()


@app.route("/")
def dashboard():
    accounts = get_all_accounts()
    return render_template("site.html", accounts=accounts)


# TikTok Verification Route (Very Reliable)
@app.route("/tiktok-verify")
def tiktok_verify():
    return '''<!DOCTYPE html>
<html>
<head>
    <meta name="tiktok-developers-site-verification" content="9zDEz8Tl3nAHkVKtlV1PzqUKojknYUbF">
    <title>TikTok Verification</title>
</head>
<body>
    <h1>TikTok Developer Verification</h1>
    <p>Verification tag is present.</p>
</body>
</html>''', 200, {'Content-Type': 'text/html'}


@app.route("/api/accounts")
def api_accounts():
    return jsonify(get_all_accounts())


# ==================== OAUTH FLOW ====================

@app.route("/connect/tiktok")
def connect_tiktok():
    username = request.args.get("username")
    if not username:
        return "Missing username", 400

    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "response_type": "code",
        "scope": "user.info.basic,video.publish",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": username
    }
    auth_url = f"{TIKTOK_AUTH_URL}?{urllib.parse.urlencode(params)}"
    return redirect(auth_url)


@app.route("/callback/tiktok")
def tiktok_callback():
    code = request.args.get("code")
    state = request.args.get("state")  # username
    error = request.args.get("error")

    if error:
        return f"TikTok authorization failed: {error}"

    if not code or not state:
        return "Missing code or state", 400

    # Exchange code for token
    token_data = {
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TIKTOK_REDIRECT_URI
    }

    response = requests.post(TIKTOK_TOKEN_URL, data=token_data)
    token_json = response.json()

    if "access_token" not in token_json:
        return f"Failed to get token: {token_json}", 400

    access_token = token_json["access_token"]
    refresh_token = token_json.get("refresh_token")
    expires_in = token_json.get("expires_in", 86400)

    # Get TikTok username from API
    user_info = requests.get(
        "https://open-api.tiktok.com/user/info/",
        headers={"Authorization": f"Bearer {access_token}"}
    ).json()

    tiktok_username = user_info.get("data", {}).get("user", {}).get("username", state)

    # Save to database
    update_account(tiktok_username, 
                   access_token=access_token,
                   refresh_token=refresh_token,
                   expires_at=int(time.time()) + expires_in,
                   connected=1,
                   status="Connected")

    return redirect(url_for("dashboard"))


@app.route("/disconnect/<username>", methods=["POST"])
def disconnect(username):
    update_account(username, 
                   access_token=None,
                   refresh_token=None,
                   connected=0,
                   status="Disconnected",
                   enabled=0)
    stop_automation(username)
    return jsonify({"success": True})


# ==================== AUTOMATION ====================

@app.route("/api/add", methods=["POST"])
def add_new_account():
    data = request.json
    username = data.get("username")
    category = data.get("category", "dance")
    
    if username and add_account(username, category):
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Account already exists"})


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
    update_account(username, enabled=0, status="Stopped")
    stop_automation(username)
    return jsonify({"success": True})


@app.route("/api/delete/<username>", methods=["POST"])
def delete(username):
    delete_account(username)
    stop_automation(username)
    return jsonify({"success": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)