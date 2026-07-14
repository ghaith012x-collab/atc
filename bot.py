import os
import re
import time
import json
import random
import threading
import urllib.parse
from datetime import datetime, timedelta

import requests
from playwright.sync_api import sync_playwright, TimeoutError
from PIL import Image
import io
from database import get_account, update_account

workers = {}
browser_sessions = {}
screenshots = {}

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

TIKWM_API = "https://www.tikwm.com/api/"
TIKWM_SEARCH_API = "https://www.tikwm.com/api/feed/search"

# Hashtag pools per category used to enrich captions
CATEGORY_HASHTAGS = {
    "horror": ["#horror", "#scary", "#horrortok", "#creepy", "#scarystories", "#fyp", "#viral"],
    "dance": ["#dance", "#dancechallenge", "#dancer", "#trending", "#fyp", "#viral"],
    "comedy": ["#comedy", "#funny", "#lol", "#humor", "#fyp", "#viral"],
    "food": ["#food", "#foodtok", "#recipe", "#cooking", "#foodie", "#fyp", "#viral"],
    "fitness": ["#fitness", "#gym", "#workout", "#fitnessmotivation", "#fyp", "#viral"],
    "gaming": ["#gaming", "#gamer", "#gamingtok", "#videogames", "#fyp", "#viral"],
    "pets": ["#pets", "#dogsoftiktok", "#catsoftiktok", "#animals", "#fyp", "#viral"],
    "motivation": ["#motivation", "#mindset", "#success", "#inspiration", "#fyp", "#viral"],
}


def create_placeholder(username, text):
    img = Image.new("RGB", (800, 450), "#111111")
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except:
        font = ImageFont.load_default()
    draw.text((30, 30), f"TikTok - {username}", fill="#ff0050", font=font)
    draw.text((30, 80), text, fill="white", font=font)
    draw.text((30, 400), datetime.now().strftime("%H:%M:%S"), fill="#888", font=font)
    return img


def take_screenshot(username):
    """Resilient screenshot: never crashes, keeps last good frame when possible."""
    session = browser_sessions.get(username)
    if not session:
        screenshots[username] = create_placeholder(username, "No browser")
        return
    try:
        page = session.get("page")
        if page is None or page.is_closed():
            # Try to recover another open page from the same context
            try:
                ctx_pages = session["context"].pages
                if ctx_pages:
                    page = ctx_pages[-1]
                    session["page"] = page
                else:
                    screenshots[username] = create_placeholder(username, "Page closed")
                    return
            except Exception:
                screenshots[username] = create_placeholder(username, "Browser closed")
                return
        screenshot_bytes = page.screenshot(timeout=8000)
        img = Image.open(io.BytesIO(screenshot_bytes))
        screenshots[username] = img
    except Exception as e:
        err = str(e).split("\n")[0][:60]
        # Keep the last good frame instead of replacing it with an error card
        if username not in screenshots:
            screenshots[username] = create_placeholder(username, f"Screenshot error: {err}")
        print(f"[{username}] screenshot error: {err}")


def connect_account(username):
    """Start headless browser, load cookies, and verify login status"""
    account = get_account(username)
    if not account or not account.get("session_data"):
        update_account(username, status="No Session", current_task="Please paste session")
        return False

    update_account(username, status="Connecting", current_task="Starting browser...")

    try:
        cookies = json.loads(account["session_data"])
    except json.JSONDecodeError:
        update_account(username, status="Invalid Session", current_task="Invalid JSON")
        return False

    try:
        pw = sync_playwright().start()

        # Use browser.new_context() NOT persistent_context
        # This ensures cookies are loaded BEFORE any page navigation
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )

        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        # Clean cookies - remove metadata fields Cookie Editor adds
        clean_cookies = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            if "name" not in c or "value" not in c:
                continue

            cleaned = {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain", ".tiktok.com"),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            }

            # Only add sameSite if valid
            if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
                cleaned["sameSite"] = c["sameSite"]

            clean_cookies.append(cleaned)

        if not clean_cookies:
            update_account(username, status="Invalid Session", current_task="No valid cookies found")
            return False

        # CORRECT ORDER: add cookies BEFORE creating page and navigating
        context.add_cookies(clean_cookies)

        page = context.new_page()

        browser_sessions[username] = {
            "pw": pw,
            "browser": browser,
            "context": context,
            "page": page
        }

        update_account(username, current_task="Verifying session...")
        page.goto("https://www.tiktok.com", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except TimeoutError:
            pass
        time.sleep(3)
        take_screenshot(username)

        # Verify login by checking for profile icon or logged-in indicators
        logged_in = False
        tiktok_username = ""

        try:
            page.wait_for_selector(
                '[data-e2e="profile-icon"], [data-e2e="top-nav-profile"], a[href*="/@"]',
                timeout=10000
            )
            logged_in = True
        except TimeoutError:
            pass

        # Also check if login button is NOT present (another way to confirm logged in)
        if not logged_in:
            try:
                login_btn = page.locator('[data-e2e="top-login-button"], a[href*="/login"]')
                if login_btn.count() == 0:
                    logged_in = True
            except:
                pass

        if logged_in:
            # Get the TikTok username by navigating to profile page
            try:
                page.goto("https://www.tiktok.com/profile", timeout=15000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except TimeoutError:
                    pass
                time.sleep(2)

                # The profile page URL redirects to /@username
                current_url = page.url
                if "/@" in current_url:
                    tiktok_username = current_url.split("/@")[-1].split("?")[0].split("/")[0]

                # Fallback: read from page content
                if not tiktok_username:
                    title_el = page.locator('h1[data-e2e="user-title"], h2[data-e2e="user-subtitle"], [data-e2e="user-title"]').first
                    if title_el.count() > 0:
                        tiktok_username = title_el.text_content().strip().lstrip("@")
            except:
                pass

            # FIX: navigate BACK to tiktok.com so the page is left in a good,
            # known state (prevents stale-page "Screenshot error" after verify)
            try:
                page.goto("https://www.tiktok.com", timeout=30000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except TimeoutError:
                    pass
                time.sleep(2)
            except Exception as e:
                print(f"[{username}] warning: could not return to home page: {e}")

            task_msg = f"Session verified - @{tiktok_username}" if tiktok_username else "Session verified"
            update_account(username, connected=1, status="Connected", current_task=task_msg)
            print(f"✓ Session verified for {username}" + (f" (TikTok: @{tiktok_username})" if tiktok_username else ""))
            take_screenshot(username)
        else:
            update_account(username, connected=0, status="Session expired", current_task="Please update session")
            print(f"✗ Session expired or invalid for {username}")

        # Live screenshot loop (0.5 seconds)
        def screenshot_loop():
            while username in browser_sessions:
                take_screenshot(username)
                time.sleep(0.5)

        threading.Thread(target=screenshot_loop, daemon=True).start()
        return logged_in

    except Exception as e:
        import traceback
        traceback.print_exc()
        update_account(username, status="Error", current_task=f"Error: {str(e)[:50]}")
        return False


# ---------------------------------------------------------------------------
# Real automation helpers
# ---------------------------------------------------------------------------

def _get_page(username):
    """Return a usable page for this account, recovering if it was closed."""
    session = browser_sessions.get(username)
    if not session:
        return None
    page = session.get("page")
    try:
        if page is None or page.is_closed():
            ctx_pages = session["context"].pages
            page = ctx_pages[-1] if ctx_pages else session["context"].new_page()
            session["page"] = page
    except Exception:
        return None
    return page


def search_on_tiktok(username, category):
    """Step 1: Navigate to TikTok search results for the category.
    Uses direct URL navigation (most reliable in headless).
    """
    page = _get_page(username)
    if page is None:
        return False

    update_account(username, current_task=f"Searching TikTok for '{category}'...")

    try:
        q = urllib.parse.quote(category)
        page.goto(f"https://www.tiktok.com/search/video?q={q}", timeout=30000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except TimeoutError:
            pass
        time.sleep(4)
        take_screenshot(username)
        print(f"[{username}] search page loaded for '{category}'")
        return True
    except Exception as e:
        print(f"[{username}] search failed: {e}")
        update_account(username, current_task=f"Search failed: {str(e)[:40]}")
        return False


def find_viral_video(username, category):
    """Step 2: Find a viral video for the category and return its info dict.

    First scrapes video links from the on-screen search results, then uses
    the tikwm API to fetch engagement stats and picks the most viewed one.
    If the page yields no links (headless blocks, captcha walls, etc.), it
    falls back to the tikwm search API which returns stats directly.
    Returns dict {url, video_id, title, play_count, digg_count, play} or None.
    """
    page = _get_page(username)
    update_account(username, current_task="Scanning results for viral videos...")

    candidate_urls = []

    # --- Try scraping video links from the browser search results ---
    if page is not None:
        try:
            for _ in range(3):
                page.mouse.wheel(0, 1200)
                time.sleep(1.5)
            take_screenshot(username)

            links = page.eval_on_selector_all(
                'a[href*="/video/"]',
                "els => els.map(e => e.href)"
            )
            seen = set()
            for link in links:
                m = re.search(r"tiktok\.com/@[^/]+/video/(\d+)", link)
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    candidate_urls.append(link.split("?")[0])
                if len(candidate_urls) >= 10:
                    break
        except Exception as e:
            print(f"[{username}] scraping search results failed: {e}")

    best = None

    # --- Get stats for scraped candidates via the tikwm detail API ---
    for url in candidate_urls[:6]:
        try:
            r = requests.get(TIKWM_API, params={"url": url}, timeout=20)
            data = r.json()
            if data.get("code") != 0:
                time.sleep(1.2)
                continue
            d = data["data"]
            info = {
                "url": url,
                "video_id": str(d.get("id", "")),
                "title": d.get("title", ""),
                "play_count": d.get("play_count", 0),
                "digg_count": d.get("digg_count", 0),
                "play": d.get("hdplay") or d.get("play", ""),
            }
            if best is None or info["play_count"] > best["play_count"]:
                best = info
            time.sleep(1.2)  # be polite to the free API
        except Exception as e:
            print(f"[{username}] tikwm detail lookup failed for {url}: {e}")

    # --- Fallback: tikwm search API (returns stats directly) ---
    if best is None or best.get("play_count", 0) < 10000:
        try:
            r = requests.post(
                TIKWM_SEARCH_API,
                data={"keywords": category, "count": 20, "cursor": 0, "HD": 1},
                timeout=25,
            )
            data = r.json()
            if data.get("code") == 0:
                videos = data.get("data", {}).get("videos", [])
                # Rank by engagement (views + likes weighted)
                videos.sort(
                    key=lambda v: v.get("play_count", 0) + v.get("digg_count", 0) * 20,
                    reverse=True,
                )
                for v in videos:
                    if v.get("duration", 0) > 180:  # skip very long videos
                        continue
                    author = (v.get("author") or {}).get("unique_id", "unknown")
                    info = {
                        "url": f"https://www.tiktok.com/@{author}/video/{v['video_id']}",
                        "video_id": str(v["video_id"]),
                        "title": v.get("title", ""),
                        "play_count": v.get("play_count", 0),
                        "digg_count": v.get("digg_count", 0),
                        "play": v.get("play", ""),
                    }
                    if best is None or info["play_count"] > best.get("play_count", 0):
                        best = info
                    break  # top-ranked acceptable video is enough
        except Exception as e:
            print(f"[{username}] tikwm search API failed: {e}")

    if best:
        views = best.get("play_count", 0)
        update_account(username, current_task=f"Found viral video ({views:,} views)")
        print(f"[{username}] selected video {best['url']} ({views} views)")
    return best


def download_video_no_watermark(username, video_info):
    """Step 3: Download the video WITHOUT watermark via the tikwm.com API.

    GET https://www.tikwm.com/api/?url={tiktok_video_url} returns JSON whose
    data.play (or data.hdplay) field is the no-watermark MP4 download link.
    Returns the local file path, or None on failure.
    """
    update_account(username, current_task="Downloading video (no watermark)...")

    play_url = video_info.get("play", "")
    try:
        if not play_url:
            r = requests.get(TIKWM_API, params={"url": video_info["url"], "hd": 1}, timeout=25)
            data = r.json()
            if data.get("code") != 0:
                print(f"[{username}] tikwm API error: {data.get('msg')}")
                return None
            play_url = data["data"].get("hdplay") or data["data"].get("play", "")

        if not play_url:
            return None
        if play_url.startswith("/"):
            play_url = "https://www.tikwm.com" + play_url

        video_id = video_info.get("video_id") or str(int(time.time()))
        safe_user = re.sub(r"[^A-Za-z0-9_-]", "_", username)
        file_path = os.path.join(DOWNLOADS_DIR, f"{safe_user}_{video_id}.mp4")

        with requests.get(play_url, stream=True, timeout=120, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }) as resp:
            resp.raise_for_status()
            with open(file_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        f.write(chunk)

        size = os.path.getsize(file_path)
        if size < 50 * 1024:  # sanity check: a real video should be > 50 KB
            print(f"[{username}] downloaded file too small ({size} bytes), discarding")
            os.remove(file_path)
            return None

        update_account(username, current_task=f"Video downloaded ({size // 1024} KB)")
        print(f"[{username}] downloaded {file_path} ({size} bytes)")
        return file_path
    except Exception as e:
        print(f"[{username}] download failed: {e}")
        return None


def generate_caption(video_info, category):
    """Step 5 helper: build a caption similar to the original with hashtags."""
    original = (video_info.get("title") or "").strip()

    # Strip hashtags from the original title to get the plain text part
    plain = re.sub(r"#\w+", "", original).strip()
    plain = re.sub(r"\s{2,}", " ", plain)
    if len(plain) > 100:
        plain = plain[:100].rsplit(" ", 1)[0] + "..."

    # Collect hashtags: reuse a few from the original + the category pool
    original_tags = re.findall(r"#\w+", original)
    fallback_tag = "#" + re.sub(r"[^a-z0-9]", "", category.lower())
    pool = CATEGORY_HASHTAGS.get(category.lower(), [fallback_tag, "#fyp", "#viral", "#trending"])
    tags = []
    for t in original_tags[:4] + pool:
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
        if len(tags) >= 7:
            break

    caption = (plain + " " if plain else "") + " ".join(tags)
    return caption.strip()[:150]  # keep well under TikTok's caption limit


def upload_video_to_tiktok(username, file_path, caption):
    """Steps 4-6: Open tiktok.com/upload, set the video on the file input,
    type the caption, and click the Post button. Returns True on success."""
    page = _get_page(username)
    if page is None:
        return False

    try:
        update_account(username, current_task="Opening TikTok upload page...")
        
        # Try multiple upload URLs (TikTok changes these)
        upload_urls = [
            "https://www.tiktok.com/creator#/upload/upload",
            "https://www.tiktok.com/upload",
            "https://www.tiktok.com/tiktokstudio/upload",
        ]
        
        file_input_found = False
        target = page
        
        for upload_url in upload_urls:
            try:
                page.goto(upload_url, timeout=45000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=20000)
                except TimeoutError:
                    pass
                time.sleep(5)
                take_screenshot(username)
                
                # Check if we got redirected to login
                if "/login" in page.url:
                    print(f"[{username}] upload URL {upload_url} redirected to login, trying next...")
                    update_account(username, current_task=f"Upload page redirected to login, trying alternate...")
                    continue
                
                # Check for iframe
                try:
                    iframe_el = page.query_selector('iframe[src*="upload"], iframe[data-tt="Upload_index_iframe"]')
                    if iframe_el:
                        frame = iframe_el.content_frame()
                        if frame:
                            target = frame
                except Exception:
                    pass
                
                # Look for file input
                try:
                    target.wait_for_selector('input[type="file"]', timeout=15000, state="attached")
                    file_input_found = True
                    print(f"[{username}] file input found on {upload_url}")
                    break
                except TimeoutError:
                    print(f"[{username}] no file input on {upload_url}, trying next...")
                    target = page  # reset target
                    continue
            except Exception as e:
                print(f"[{username}] failed to load {upload_url}: {e}")
                continue
        
        if not file_input_found:
            update_account(username, current_task="Upload page failed - no file input found")
            print(f"[{username}] no file input found on any upload URL")
            take_screenshot(username)
            return False

        # --- Step 4: set the video file on the file input ---
        update_account(username, current_task="Uploading video file...")

        file_input = target.locator('input[type="file"]').first
        file_input.set_input_files(file_path)
        print(f"[{username}] file set on input, waiting for processing...")
        update_account(username, current_task="Processing upload...")

        # Wait for TikTok to process the upload (caption editor appears)
        processed = False
        for _ in range(36):  # up to ~3 minutes
            time.sleep(5)
            take_screenshot(username)
            try:
                cap = target.locator(
                    'div[contenteditable="true"], '
                    'div.public-DraftEditor-content, '
                    '[data-e2e="caption-editor"], '
                    'div[data-contents="true"]'
                )
                if cap.count() > 0 and cap.first.is_visible():
                    processed = True
                    break
            except Exception:
                pass
        if not processed:
            print(f"[{username}] upload processing timed out")
            return False

        # --- Step 5: type the caption ---
        update_account(username, current_task="Writing caption...")
        try:
            caption_box = target.locator(
                'div[contenteditable="true"], '
                'div.public-DraftEditor-content, '
                '[data-e2e="caption-editor"]'
            ).first
            caption_box.click(timeout=10000)
            time.sleep(1)
            # Clear any auto-filled text (TikTok pre-fills the filename)
            caption_box.press("Control+a")
            time.sleep(0.3)
            caption_box.press("Delete")
            time.sleep(0.5)
            caption_box.type(caption, delay=random.randint(30, 70))
            time.sleep(2)
        except Exception as e:
            print(f"[{username}] caption typing failed (posting anyway): {e}")
        take_screenshot(username)

        # --- Step 6: click the Post button ---
        update_account(username, current_task="Posting video...")
        post_clicked = False
        post_selectors = [
            '[data-e2e="post_video_button"]',
            'button[data-e2e="post-button"]',
            'button:has-text("Post")',
            'button:has-text("Publish")',
        ]
        for _ in range(24):  # wait up to ~2 minutes for Post to become enabled
            for sel in post_selectors:
                try:
                    btn = target.locator(sel).first
                    if btn.count() > 0 and btn.is_visible() and btn.is_enabled():
                        btn.click(timeout=8000)
                        post_clicked = True
                        break
                except Exception:
                    continue
            if post_clicked:
                break
            time.sleep(5)
            take_screenshot(username)

        if not post_clicked:
            print(f"[{username}] Post button never became clickable")
            return False

        # Wait for the post to complete (success modal / redirect)
        time.sleep(8)
        take_screenshot(username)

        # Handle a possible confirmation modal ("Post now" etc.)
        try:
            confirm = target.locator(
                'button:has-text("Post now"), button:has-text("Post Now"), '
                'div[role="dialog"] button:has-text("Post")'
            ).first
            if confirm.count() > 0 and confirm.is_visible():
                confirm.click(timeout=5000)
                time.sleep(8)
        except Exception:
            pass

        take_screenshot(username)
        print(f"[{username}] ✓ video posted")

        # Leave the page in a good state for the live cam
        try:
            page.goto("https://www.tiktok.com", timeout=30000)
            time.sleep(2)
        except Exception:
            pass
        return True

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[{username}] upload flow failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Main automation worker:
# search -> find viral video -> download (no watermark) -> upload -> caption -> post
# ---------------------------------------------------------------------------

def automation_worker(username):
    posted_video_ids = set()

    while True:
        account = get_account(username)
        if not account or not account["enabled"]:
            break

        if not account["connected"]:
            update_account(username, current_task="Not connected")
            time.sleep(5)
            continue

        video_file = None
        try:
            # Open persistent session if needed
            if username not in browser_sessions:
                connect_account(username)
                time.sleep(3)
                account = get_account(username)
                if not account["connected"]:
                    update_account(username, enabled=0)
                    break

            category = account.get("category") or "dance"

            # --- Step 1: search TikTok in the browser ---
            update_account(username, current_task=f"Step 1: Searching '{category}'...")
            search_ok = search_on_tiktok(username, category)
            if not search_ok:
                print(f"[{username}] search step failed, using API fallback")

            # --- Step 2: find a viral video in the results ---
            update_account(username, current_task="Step 2: Finding viral video...")
            video_info = find_viral_video(username, category)
            if not video_info:
                update_account(username, current_task="No viral video found, retrying in 2 min")
                time.sleep(120)
                continue

            # Skip videos already reposted during this session
            if video_info.get("video_id") in posted_video_ids:
                update_account(username, current_task="Already posted that one, searching again...")
                time.sleep(30)
                continue

            # --- Step 3: download without watermark (tikwm.com API) ---
            update_account(username, current_task="Step 3: Downloading video (no watermark)...")
            video_file = download_video_no_watermark(username, video_info)
            if not video_file:
                update_account(username, current_task="Download failed, retrying in 2 min")
                time.sleep(120)
                continue

            # --- Step 5 prep: generate a caption with hashtags ---
            update_account(username, current_task="Step 4: Generating caption...")
            caption = generate_caption(video_info, category)
            print(f"[{username}] caption: {caption}")

            # --- Steps 4-6: upload, add caption, click Post ---
            update_account(username, current_task="Step 5: Uploading to TikTok...")
            success = upload_video_to_tiktok(username, video_file, caption)

            if success:
                posted_video_ids.add(video_info.get("video_id"))
                now = datetime.now()
                next_time = (now + timedelta(minutes=25)).strftime("%H:%M")
                update_account(
                    username,
                    last_post=now.strftime("%Y-%m-%d %H:%M"),
                    next_post=next_time,
                    current_task=f"Posted! Next post at {next_time}"
                )
                # Keep the 25-minute cycle, but check the enabled flag every 10s
                waited = 0
                while waited < 1500:
                    account = get_account(username)
                    if not account or not account["enabled"]:
                        break
                    time.sleep(10)
                    waited += 10
            else:
                update_account(username, current_task="Post failed, retrying in 3 min")
                time.sleep(180)

        except Exception as e:
            import traceback
            traceback.print_exc()
            update_account(username, current_task=f"Error: {str(e)[:50]}")
            time.sleep(30)
        finally:
            # Clean up the downloaded file to save disk space on Railway
            if video_file and os.path.exists(video_file):
                try:
                    os.remove(video_file)
                except Exception:
                    pass

    update_account(username, status="Stopped", current_task="Idle")
    workers.pop(username, None)


def start_automation(username):
    if username in workers:
        return
    thread = threading.Thread(target=automation_worker, args=(username,), daemon=True)
    workers[username] = thread
    thread.start()


def stop_automation(username):
    if username in workers:
        del workers[username]
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            del browser_sessions[username]
        except:
            pass


def delete_account_session(username):
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            del browser_sessions[username]
        except:
            pass
    if username in workers:
        del workers[username]

    import shutil
    session_path = f"sessions/{username}"
    if os.path.exists(session_path):
        shutil.rmtree(session_path, ignore_errors=True)

    # Also clear session_data from DB
    update_account(username, session_data=None, connected=0, status="Disconnected", current_task="Idle")
