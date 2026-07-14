import os
import sqlite3
import threading
import io
from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    Response
)
from PIL import Image
from bot import (
    start_bot,
    stop_bot,
    screenshots,
    start_browser,
    browser_sessions
)
from playwright.sync_api import sync_playwright

app = Flask(__name__)
DATABASE = "accounts.db"

# ==========================
# DATABASE
# ==========================
def db():
    return sqlite3.connect(DATABASE)

def init_db():
    conn = db()
    conn.execute("""
    CREATE TABLE IF NOT EXISTS accounts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        category TEXT,
        enabled INTEGER DEFAULT 0,
        status TEXT DEFAULT 'Stopped',
        task TEXT DEFAULT 'Idle',
        last_post TEXT,
        next_post TEXT
    )
    """)
    conn.commit()
    conn.close()

def get_accounts():
    conn = db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM accounts").fetchall()
    conn.close()
    return [dict(x) for x in rows]

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
# DASHBOARD
# ==========================
@app.route("/")
def home():
    return render_template("site.html", accounts=get_accounts())

@app.route("/api/accounts")
def api_accounts():
    return jsonify(get_accounts())

# ==========================
# ADD ACCOUNT
# ==========================
@app.route("/api/add", methods=["POST"])
def add_account():
    data = request.json
    username = data["username"]
    category = data.get("category", "horror")
    conn = db()
    conn.execute(
        "INSERT INTO accounts (username, category) VALUES (?,?)",
        (username, category)
    )
    conn.commit()
    conn.close()
    return {"success": True}

# ==========================
# CONNECT TIKTOK (with Playwright)
# ==========================
@app.route("/connect/tiktok/<username>")
def connect_tiktok(username):
    try:
        session = start_browser(username)
        return jsonify({
            "success": True,
            "message": "Browser session started",
            "account": username
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        })

# ==========================
# START / STOP BOT
# ==========================
@app.route("/api/start/<username>", methods=["POST"])
def start(username):
    update_account(username, enabled=1, status="Starting")
    start_bot(username)
    return {"success": True}

@app.route("/api/stop/<username>", methods=["POST"])
def stop(username):
    update_account(username, enabled=0, status="Stopping")
    stop_bot(username)
    return {"success": True}

# ==========================
# LIVE SCREEN
# ==========================
@app.route("/live/<username>")
def live(username):
    if username not in screenshots:
        img = Image.new("RGB", (800, 450), "#111")
        screenshots[username] = img
    
    buffer = io.BytesIO()
    screenshots[username].save(buffer, "JPEG", quality=85)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype="image/jpeg")

# ==========================
# START SERVER
# ==========================
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)