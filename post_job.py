"""One-off source-link posting flow; isolated from the existing scheduler."""
import os, re, subprocess, tempfile, threading
from database import get_account, update_account
from bot import upload_video_to_tiktok, upload_video_to_youtube, browser_sessions, _init_worker_browser


def _safe_title(url):
    return "ATC video " + re.sub(r"[^A-Za-z0-9]+", " ", url)[-48:].strip()


def post_from_link(username, source_url, captions):
    account = get_account(username)
    if not account:
        raise ValueError("Account not found")
    if account.get("platform") == "YouTube":
        from app import has_oauth_token
        if not has_oauth_token(username):
            raise ValueError("Connect Google / YouTube first")
    elif not account.get("connected"):
        raise ValueError("Connect the TikTok session first")
    if not re.match(r"^https?://", source_url, re.I):
        raise ValueError("Enter a complete video URL")
    update_account(username, status="Posting", current_task="Downloading source video in best quality...")
    work = tempfile.mkdtemp(prefix="atc-post-")
    output = os.path.join(work, "video.%(ext)s")
    try:
        # yt-dlp selects the highest quality video+audio streams and merges MP4.
        subprocess.run(["yt-dlp", "--no-playlist", "-f", "bv*+ba/b", "--merge-output-format", "mp4",
                        "-o", output, source_url], check=True, timeout=900)
        files = [os.path.join(work, f) for f in os.listdir(work) if f.endswith((".mp4", ".mkv", ".webm"))]
        if not files:
            raise RuntimeError("The source did not provide a downloadable video")
        video = files[0]
        update_account(username, current_task="Uploading video...")
        # Use the same browser/session initialization as the category uploader.
        # The one-off flow may have no request-thread page to reuse, so create
        # the worker browser here instead of waiting forever on an empty session.
        import time
        session = browser_sessions.get(username)
        if not session or not session.get("page") or session["page"].is_closed():
            update_account(username, current_task="Starting TikTok upload browser...")
            if not _init_worker_browser(username, account):
                raise RuntimeError("Could not start the upload browser")
        deadline = time.time() + 30
        while time.time() < deadline:
            session = browser_sessions.get(username)
            if session and session.get("page") and not session["page"].is_closed():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Upload browser did not become ready")
        caption = captions.strip()
        if account.get("platform") == "YouTube":
            ok = upload_video_to_youtube(username, video, caption, _safe_title(source_url))
        else:
            ok = upload_video_to_tiktok(username, video, caption)
        if not ok:
            raise RuntimeError("The platform upload did not complete: TikTok did not confirm the upload. Check that the session is valid and the account is still logged in.")
        update_account(username, status="Connected", current_task="Ready", last_post="Just now")
    except Exception as exc:
        update_account(username, status="Error", current_task=str(exc)[:180])
        raise
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


def start_post(username, source_url, captions):
    thread = threading.Thread(target=post_from_link, args=(username, source_url, captions), daemon=True)
    thread.start()
