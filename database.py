import os
import psycopg2
import psycopg2.extras
from datetime import datetime

DATABASE_URL = os.environ.get("DATABASE_URL")


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id SERIAL PRIMARY KEY,
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
            next_post_ts BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Idempotently add new columns for existing databases.
    existing_cols_q = """
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'accounts'
    """
    cur.execute(existing_cols_q)
    cols = [r[0] for r in cur.fetchall()]
    for col, sql in [
        ("next_post_ts",  "ALTER TABLE accounts ADD COLUMN next_post_ts BIGINT"),
        ("platform",      "ALTER TABLE accounts ADD COLUMN platform TEXT DEFAULT 'TikTok'"),
        ("logged_in_as",  "ALTER TABLE accounts ADD COLUMN logged_in_as TEXT"),
        ("logs",          "ALTER TABLE accounts ADD COLUMN logs TEXT"),
        ("verify_code",   "ALTER TABLE accounts ADD COLUMN verify_code TEXT"),
        ("email",         "ALTER TABLE accounts ADD COLUMN email TEXT"),
        ("password",      "ALTER TABLE accounts ADD COLUMN password TEXT"),
        ("login_method",  "ALTER TABLE accounts ADD COLUMN login_method TEXT DEFAULT 'cookie'"),
    ]:
        if col not in cols:
            cur.execute(sql)
    conn.commit()
    cur.close()
    conn.close()


def get_all_accounts():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM accounts ORDER BY created_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(row) for row in rows]


def get_account(username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM accounts WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def update_account(username, **kwargs):
    if not kwargs:
        return
    conn = get_db()
    cur = conn.cursor()
    for key, value in kwargs.items():
        cur.execute(
            f"UPDATE accounts SET {key} = %s WHERE username = %s",
            (value, username)
        )
    conn.commit()
    cur.close()
    conn.close()


def add_account(username, category="dance", platform="TikTok"):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO accounts (username, platform, category) VALUES (%s, %s, %s)",
            (username, platform, category)
        )
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def append_log(username, message):
    """Append a timestamped line to the account's rolling log (max ~500 lines)."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT logs FROM accounts WHERE username = %s", (username,))
        row = cur.fetchone()
        existing = (row["logs"] if row and row["logs"] else "")
        lines = [l for l in existing.split("\n") if l.strip()]
        lines.append(line)
        if len(lines) > 500:
            lines = lines[-500:]
        cur.execute(
            "UPDATE accounts SET logs = %s WHERE username = %s",
            ("\n".join(lines), username)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass


def get_logs(username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT logs FROM accounts WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row["logs"] if row and row["logs"] else ""


def get_verify_code(username):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT verify_code FROM accounts WHERE username = %s", (username,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return (row["verify_code"] if row and row["verify_code"] else "").strip()


def clear_verify_code(username):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE accounts SET verify_code = '' WHERE username = %s", (username,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception:
        pass


def delete_account(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM accounts WHERE username = %s", (username,))
    conn.commit()
    cur.close()
    conn.close()
