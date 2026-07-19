import os, threading, json
from datetime import datetime

_lock = threading.RLock()
_pool = None
_accounts = {}

DEFAULT_FIELDS = {
    'platform':'TikTok','category':'dance','session_data':None,'connected':0,'enabled':0,
    'status':'Disconnected','current_task':'Idle','last_post':None,'next_post':None,
    'next_post_ts':None,'logged_in_as':None,'logs':'','verify_code':'','email':None,
    'password':None,'login_method':'cookie','profile_link':None,'channel_link':None,'posted_ids':None,
    'created_at':datetime.now().strftime('%Y-%m-%d %H:%M:%S')
}

def _new_account(username, category, platform):
    a=dict(DEFAULT_FIELDS); a.update(username=username,category=category,platform=platform); return a

def _pg_enabled(): return _pool is not None

def init_db():
    global _pool
    url=os.environ.get('DATABASE_URL')
    if not url: return
    try:
        import psycopg2
        from psycopg2.pool import ThreadedConnectionPool
        _pool=ThreadedConnectionPool(1,8,url,sslmode='require' if 'localhost' not in url else None)
        conn=_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute('''CREATE TABLE IF NOT EXISTS accounts (
                  username TEXT PRIMARY KEY, platform TEXT NOT NULL DEFAULT 'TikTok', category TEXT NOT NULL DEFAULT 'dance',
                  session_data TEXT, connected INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 0,
                  status TEXT, current_task TEXT, last_post TEXT, next_post TEXT, next_post_ts DOUBLE PRECISION,
                  logged_in_as TEXT, logs TEXT, verify_code TEXT, email TEXT, password TEXT, login_method TEXT,
                   profile_link TEXT, channel_link TEXT, posted_ids TEXT, created_at TEXT)''')
                cur.execute('''CREATE TABLE IF NOT EXISTS oauth_tokens (
                  username TEXT PRIMARY KEY REFERENCES accounts(username) ON DELETE CASCADE,
                  token_json TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW())''')
            conn.commit()
        finally: _pool.putconn(conn)
        print('✓ PostgreSQL persistence enabled', flush=True)
    except Exception as e:
        _pool=None
        print(f'PostgreSQL unavailable; using in-memory store: {e}', flush=True)

def _row(cur, row):
    if not row: return None
    cols=[d[0] for d in cur.description]
    a=dict(zip(cols,row))
    for k,v in DEFAULT_FIELDS.items(): a.setdefault(k,v)
    return a

def get_all_accounts():
    if not _pg_enabled():
        with _lock: return [dict(a) for a in _accounts.values()]
    conn=_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM accounts ORDER BY created_at, username')
            return [_row(cur,r) for r in cur.fetchall()]
    finally: _pool.putconn(conn)

def get_account(username):
    if not _pg_enabled():
        with _lock: return dict(_accounts[username]) if username in _accounts else None
    conn=_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT * FROM accounts WHERE username=%s',(username,)); return _row(cur,cur.fetchone())
    finally: _pool.putconn(conn)

def add_account(username, category='dance', platform='TikTok', profile_link=None, channel_link=None):
    if not _pg_enabled():
        with _lock:
            if username in _accounts: return False
            a=_new_account(username,category,platform)
            if profile_link: a['profile_link']=profile_link
            if channel_link: a['channel_link']=channel_link
            _accounts[username]=a; return True
    conn=_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO accounts(username,category,platform,status,current_task,logs,verify_code,login_method,profile_link,channel_link,created_at)
                           VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
                        (username,category,platform,'Disconnected','Idle','','','cookie',profile_link,channel_link,datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            ok=cur.rowcount==1; conn.commit(); return ok
    finally:_pool.putconn(conn)

def update_account(username, **kwargs):
    if not kwargs: return
    allowed=set(DEFAULT_FIELDS)-{'username','created_at'}
    kwargs={k:v for k,v in kwargs.items() if k in allowed}
    if not kwargs: return
    if not _pg_enabled():
        with _lock:
            if username in _accounts: _accounts[username].update(kwargs)
        return
    conn=_pool.getconn()
    try:
        with conn.cursor() as cur:
            keys=list(kwargs); cur.execute('UPDATE accounts SET '+','.join(f'"{k}"=%s' for k in keys)+' WHERE username=%s',[kwargs[k] for k in keys]+[username]); conn.commit()
    finally:_pool.putconn(conn)

def delete_account(username):
    if not _pg_enabled():
        with _lock:_accounts.pop(username,None)
        return
    conn=_pool.getconn()
    try:
        with conn.cursor() as cur: cur.execute('DELETE FROM accounts WHERE username=%s',(username,)); conn.commit()
    finally:_pool.putconn(conn)

def get_posted_ids(username):
    """Return the set of already-posted video ids for an account (persisted in DB)."""
    a=get_account(username)
    raw = (a or {}).get('posted_ids') if a else None
    if not raw: return set()
    try: return set(json.loads(raw) if isinstance(raw,str) else raw)
    except Exception: return set()

def set_posted_ids(username, ids):
    try: update_account(username, posted_ids=json.dumps(sorted(ids)))
    except Exception: pass

def append_log(username,message):
    try:
        line=f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
        a=get_account(username)
        if not a:return
        lines=[x for x in (a.get('logs') or '').split('\n') if x.strip()]+[line]
        update_account(username,logs='\n'.join(lines[-500:]))
    except Exception: pass

def get_logs(username):
    a=get_account(username); return (a or {}).get('logs') or ''
def get_verify_code(username):
    a=get_account(username); return (a or {}).get('verify_code') or ''
def clear_verify_code(username): update_account(username,verify_code='')

def save_oauth_token(username, token):
    raw=json.dumps(token) if isinstance(token,dict) else str(token)
    if not _pg_enabled():
        with _lock:
            if username in _accounts: _accounts[username]['oauth_token']=raw
        return
    conn=_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('''INSERT INTO oauth_tokens(username,token_json) VALUES(%s,%s)
                           ON CONFLICT(username) DO UPDATE SET token_json=EXCLUDED.token_json,updated_at=NOW()''',(username,raw)); conn.commit()
    finally:_pool.putconn(conn)

def get_oauth_token(username):
    """Return the stored OAuth token dict for an account, or None."""
    if not _pg_enabled():
        with _lock:
            raw = (_accounts.get(username) or {}).get('oauth_token')
    else:
        conn=_pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT token_json FROM oauth_tokens WHERE username=%s',(username,))
                row=cur.fetchone(); raw=row[0] if row else None
        finally:_pool.putconn(conn)
    if not raw: return None
    try: return json.loads(raw) if isinstance(raw, str) else raw
    except Exception: return None

def has_oauth_token(username):
    if not _pg_enabled():
        with _lock:return bool((_accounts.get(username) or {}).get('oauth_token'))
    conn=_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM oauth_tokens WHERE username=%s',(username,)); return cur.fetchone() is not None
    finally:_pool.putconn(conn)
