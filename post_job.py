"""One-off source-link posting flow; isolated from the existing scheduler."""
import os, re, subprocess, tempfile, threading
from database import get_account, update_account, delete_account
from bot import upload_video_to_tiktok, upload_video_to_youtube, browser_sessions


def _safe_title(url):
    return "ATC video " + re.sub(r"[^A-Za-z0-9]+", " ", url)[-48:].strip()


def post_from_link(username, source_url, captions):
    account = get_account(username)
    if not account:
        raise ValueError("Account not found")
    platform = account.get("platform") or "TikTok"
    if platform == "YouTube":
        from app import has_oauth_token
        if not has_oauth_token(username):
            raise ValueError("Connect Google / YouTube first")
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
        caption = captions.strip()

        if platform == "YouTube":
            ok = upload_video_to_youtube(username, video, caption, _safe_title(source_url))
        else:
            # TikTok: open our OWN worker browser from the stored cookie so the
            # upload has a live page. (connect_account verifies then CLOSES the
            # browser, so we must not reuse it.) Fail early if there is no cookie.
            if not account.get("session_data"):
                raise ValueError("Connect the TikTok session first")
            if username not in browser_sessions:
                from bot import _init_worker_browser
                if not _init_worker_browser(username, account):
                    raise RuntimeError("Could not start a TikTok browser session from the provided cookie")
            ok = upload_video_to_tiktok(username, video, caption)

        if not ok:
            raise RuntimeError("The platform upload did not complete")
        update_account(username, status="Connected", current_task="Ready", last_post="Just now")
    except Exception as exc:
        update_account(username, status="Error", current_task=str(exc)[:180])
        raise
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
        # Tear down the throwaway browser used for this one-off post.
        if username in browser_sessions:
            try:
                s = browser_sessions.pop(username)
                for closer in ("context", "browser"):
                    try:
                        getattr(s.get(closer), "close")()
                    except Exception:
                        pass
                try:
                    s.get("pw").stop()
                except Exception:
                    pass
            except Exception:
                pass
        # Remove the temporary TikTok destination so it doesn't pile up.
        if username.startswith("__post_tiktok_"):
            try:
                delete_account(username)
            except Exception:
                pass


def start_post(username, source_url, captions):
    thread = threading.Thread(target=post_from_link, args=(username, source_url, captions), daemon=True)
    thread.start()
