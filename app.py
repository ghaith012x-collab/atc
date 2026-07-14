import os
import sqlite3
import threading
import time
import io
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response
from PIL import Image
from bot import (
    start_bot, stop_bot, 
    connect_account, delete_account,
    screenshots, get_account_status
)

app = Flask(__name__)
DATABASE = "accounts.db"

def db():
    return sqlite3.connect(DATABASE)

def init_db():
    conn = db()
    # Drop and recreate to fix broken state (safe for prototype)
    conn.execute("DROP TABLE IF EXISTS accounts")
    conn.execute("DROP TABLE IF EXISTS logs")
    
    conn.execute("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            category TEXT DEFAULT 'dance',
            connected INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Stopped',
            task TEXT DEFAULT 'Idle',
            last_post TEXT,
            next_post TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            action TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print("✓ Database initialized")

def get_accounts():
    conn = db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(x) for x in rows]

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

# ==================== ROUTES ====================

@app.route("/")
def home():
    return render_template("site.html", accounts=get_accounts())

@app.route("/api/accounts")
def api_accounts():
    return jsonify(get_accounts())

@app.route("/api/add", methods=["POST"])
def add_account():
    data = request.json
    username = data.get("username", "").strip()
    category = data.get("category", "dance")
    
    if not username.startswith("@"):
        username = "@" + username
    
    try:
        conn = db()
        conn.execute(
            "INSERT INTO accounts (username, category) VALUES (?, ?)",
            (username, category)
        )
        conn.commit()
        conn.close()
        log_action(username, "Account added")
        return {"success": True}
    except sqlite3.IntegrityError:
        return {"success": False, "error": "Account already exists"}

@app.route("/api/delete/<username>", methods=["POST"])
def delete(username):
    delete_account(username)
    return {"success": True}

@app.route("/connect/tiktok/<username>")
def connect_tiktok(username):
    def connect_thread():
        connect_account(username)
    
    thread = threading.Thread(target=connect_thread, daemon=True)
    thread.start()
    return jsonify({"success": True, "message": "Connecting..."})

@app.route("/api/start/<username>", methods=["POST"])
def start(username):
    update_account(username, enabled=1, status="Starting")
    start_bot(username)
    return {"success": True}

@app.route("/api/stop/<username>", methods=["POST"])
def stop(username):
    update_account(username, enabled=0)
    stop_bot(username)
    return {"success": True}

@app.route("/live/<username>")
def live(username):
    if username not in screenshots:
        img = Image.new("RGB", (800, 450), "#111111")
        screenshots[username] = img
    
    buffer = io.BytesIO()
    screenshots[username].save(buffer, "JPEG", quality=80)
    buffer.seek(0)
    return Response(buffer.getvalue(), mimetype="image/jpeg")

# ==================== START ====================

# Initialize database on every start (including gunicorn)
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)