import os
import threading
from datetime import datetime

# ---------------------------------------------------------------------------
# TEMPORARY: in-memory store.
# The Postgres backend was disabled so the app does NOT persist sessions across
# restarts (no old sessions saved). All accounts/logs live only in this process
# and are wiped on restart. To restore persistence, bring back the Postgres
# implementation and set DATABASE_URL.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_accounts = {}

DEFAULT_FIELDS = {
    "platform": "TikTok",
    "category": "dance",
    "session_data": None,
    "connected": 0,
    "enabled": 0,
    "status": "Disconnected",
    "current_task": "Idle",
    "last_post": None,
    "next_post": None,
    "next_post_ts": None,
    "logged_in_as": None,
    "logs": "",
    "verify_code": "",
    "email": None,
    "password": None,
    "login_method": "cookie",
    "profile_link": None,
    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}


def _new_account(username, category, platform):
    acc = dict(DEFAULT_FIELDS)
    acc["username"] = username
    acc["category"] = category
    acc["platform"] = platform
    return acc


def init_db():
    # No-op for the in-memory store.
    return


def get_all_accounts():
    with _lock:
        return [dict(a) for a in _accounts.values()]


def get_account(username):
    with _lock:
        acc = _accounts.get(username)
        return dict(acc) if acc else None


def update_account(username, **kwargs):
    if not kwargs:
        return
    with _lock:
        acc = _accounts.get(username)
        if not acc:
            return
        for key, value in kwargs.items():
            acc[key] = value


def add_account(username, category="dance", platform="TikTok"):
    with _lock:
        if username in _accounts:
            return False
        _accounts[username] = _new_account(username, category, platform)
        return True


def append_log(username, message):
    """Append a timestamped line to the account's rolling log (max ~500 lines)."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {message}"
        with _lock:
            acc = _accounts.get(username)
            if not acc:
                return
            existing = acc.get("logs") or ""
            lines = [l for l in existing.split("\n") if l.strip()]
            lines.append(line)
            if len(lines) > 500:
                lines = lines[-500:]
            acc["logs"] = "\n".join(lines)
    except Exception:
        pass


def get_logs(username):
    with _lock:
        acc = _accounts.get(username)
        return acc.get("logs") or "" if acc else ""


def get_verify_code(username):
    with _lock:
        acc = _accounts.get(username)
        return (acc.get("verify_code") or "").strip() if acc else ""


def clear_verify_code(username):
    with _lock:
        acc = _accounts.get(username)
        if acc:
            acc["verify_code"] = ""


def delete_account(username):
    with _lock:
        _accounts.pop(username, None)
