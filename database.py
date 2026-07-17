import sqlite3
from datetime import datetime

DATABASE = "accounts.db"

def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            platform TEXT DEFAULT 'TikTok',
            category TEXT DEFAULT 'dance',
            session_data TEXT,
            connected INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Disconnected',
            current_task TEXT DEFAULT 'Idle',
            last_post TEXT,
            next_post TEXT,
            next_post_ts INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Idempotently add new columns for existing databases.
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()]
        for col, sql in [
            ("next_post_ts", "ALTER TABLE accounts ADD COLUMN next_post_ts INTEGER"),
            ("platform", "ALTER TABLE accounts ADD COLUMN platform TEXT DEFAULT 'TikTok'"),
            ("logged_in_as", "ALTER TABLE accounts ADD COLUMN logged_in_as TEXT"),
            ("logs", "ALTER TABLE accounts ADD COLUMN logs TEXT"),
        ]:
            if col not in cols:
                conn.execute(sql)
    except Exception:
        pass
    conn.commit()
    conn.close()

def get_all_accounts():
    conn = get_db()
    rows = conn.execute("SELECT * FROM accounts ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(row) for row in rows]

def get_account(username):
    conn = get_db()
    row = conn.execute("SELECT * FROM accounts WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None

def update_account(username, **kwargs):
    conn = get_db()
    for key, value in kwargs.items():
        conn.execute(f"UPDATE accounts SET {key} = ? WHERE username = ?", (value, username))
    conn.commit()
    conn.close()

def add_account(username, category="dance", platform="TikTok"):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO accounts (username, platform, category) VALUES (?, ?, ?)",
            (username, platform, category)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def delete_account(username):
    conn = get_db()
    conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
    conn.commit()
    conn.close()


def append_log(username, message):
    """Append a timestamped line to the account's rolling log (max ~500 lines)."""
    try:
        from datetime import datetime
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        conn = get_db()
        row = conn.execute("SELECT logs FROM accounts WHERE username = ?", (username,)).fetchone()
        existing = (row["logs"] if row and row["logs"] else "")
        lines = [l for l in existing.split("\n") if l.strip()]
        lines.append(line)
        if len(lines) > 500:
            lines = lines[-500:]
        conn.execute("UPDATE accounts SET logs = ? WHERE username = ?", ("\n".join(lines), username))
        conn.commit()
        conn.close()
    except Exception:
        pass


def get_logs(username):
    conn = get_db()
    row = conn.execute("SELECT logs FROM accounts WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row["logs"] if row and row["logs"] else ""
