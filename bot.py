import os
import sys
import re
import time
import json
import random
import threading
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Tuple, Any

import requests
from playwright.sync_api import sync_playwright, TimeoutError
from PIL import Image
import io
import math
from database import get_account, update_account, append_log, get_verify_code, clear_verify_code

# === CAPTCHA SOLVER (isolated addition) ===
try:
    from captcha_solver import solve_rotate_captcha, solve_rotate_captcha_robust
    CAPTCHA_SOLVER_AVAILABLE = True
except Exception:
    CAPTCHA_SOLVER_AVAILABLE = False
    print("[captcha] Offline solver module not available (optional)")

# ------------------------------------------------------------
# TikTok Captcha Detection + Solver (isolated, non-breaking)
# ------------------------------------------------------------

def _detect_tiktok_captcha(page) -> Optional[str]:
    """Improved detection for TikTok captchas.
    Returns 'rotate', 'slide', 'puzzle', or None.
    """
    if page is None:
        return None

    try:
        # === BROAD KEYWORD DETECTION (very reliable) ===
        captcha_keywords = [
            "drag the slider", "fit the puzzle", "puzzle", "slider",
            "drag to", "slide to", "rotate", "whirl", "turn the", 
            "align", "verify your", "security check", "captcha"
        ]

        # 1. Check common TikTok captcha containers (most reliable)
        containers = [
            'div[role="dialog"]',
            '[class*="captcha"]',
            '[class*="verify"]',
            '[class*="slide"]',
            '[class*="puzzle"]',
            'div[aria-modal="true"]',
            '.geetest',           # Geetest (TikTok often uses)
            '[data-e2e*="captcha"]',
            '[data-e2e*="verify"]',
        ]

        for container_sel in containers:
            try:
                container = page.locator(container_sel).first
                if container.count() > 0 and container.is_visible():
                    try:
                        text = container.inner_text(timeout=1500) or ""
                        text_lower = text.lower()
                        if any(kw in text_lower for kw in captcha_keywords):
                            # Classify type
                            if any(k in text_lower for k in ["rotate", "whirl", "turn"]):
                                return "rotate"
                            if any(k in text_lower for k in ["drag", "slider", "puzzle", "fit"]):
                                return "slide"
                            return "slide"  # default for TikTok captchas
                    except:
                        pass
            except:
                continue

        # 2. Direct text search across page (very effective)
        try:
            body_text = page.inner_text("body", timeout=2000) or ""
            body_lower = body_text.lower()
            
            if any(kw in body_lower for kw in captcha_keywords):
                if any(k in body_lower for k in ["rotate", "whirl", "turn"]):
                    return "rotate"
                if any(k in body_lower for k in ["drag", "slider", "puzzle", "fit the"]):
                    return "slide"
                return "slide"
        except:
            pass

        # 3. Look for visible slider / puzzle elements
        slider_selectors = [
            'input[type="range"]',
            '[class*="slider"]',
            '[class*="geetest"]',
            'canvas',
            '[role="slider"]',
            'button[aria-label*="slide"]',
            'div[style*="cursor:"]',   # often the drag handle
        ]
        for sel in slider_selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible():
                    # Check nearby text
                    try:
                        parent = el.locator("xpath=..").first
                        txt = parent.inner_text(timeout=800) or ""
                        if any(k in txt.lower() for k in ["drag", "slide", "puzzle", "fit"]):
                            return "slide"
                    except:
                        return "slide"
            except:
                continue

        # 4. Fallback: any visible modal/dialog with captcha-like content
        try:
            dialogs = page.locator('div[role="dialog"], [aria-modal="true"]')
            for i in range(min(dialogs.count(), 3)):
                try:
                    d = dialogs.nth(i)
                    if d.is_visible():
                        txt = d.inner_text(timeout=1000) or ""
                        if any(k in txt.lower() for k in ["drag", "slide", "puzzle", "rotate", "verify"]):
                            if "rotate" in txt.lower() or "whirl" in txt.lower():
                                return "rotate"
                            return "slide"
                except:
                    continue
        except:
            pass

        return None

    except Exception as e:
        print(f"[captcha] Detection error: {str(e)[:60]}")
        return None


def _extract_rotate_images(page) -> Tuple[Optional[bytes], Optional[bytes]]:
    """
    Tries to extract outer and inner images from a TikTok rotate captcha.
    Returns (outer_bytes, inner_bytes)
    """
    try:
        # Strategy 1: Screenshot the entire captcha container
        captcha_box = page.locator(
            'div[role="dialog"], .verify-container, [class*="captcha"], [data-e2e*="verify"]'
        ).first
        
        if captcha_box.count() == 0:
            captcha_box = page.locator('body')
        
        full_bytes = captcha_box.screenshot(timeout=8000)
        
        # Strategy 2: Try to find and screenshot specific elements
        outer = None
        inner = None
        
        # Look for multiple canvas/img inside captcha area
        canvases = page.locator('canvas')
        imgs = page.locator('img')
        
        elements = []
        for i in range(min(canvases.count(), 4)):
            try:
                elements.append(canvases.nth(i))
            except: pass
        for i in range(min(imgs.count(), 4)):
            try:
                elements.append(imgs.nth(i))
            except: pass
        
        if len(elements) >= 2:
            try:
                outer = elements[0].screenshot(timeout=6000)
                inner = elements[-1].screenshot(timeout=6000)
            except:
                pass
        
        # Fallback: use full screenshot for both (the solver is robust)
        if not outer:
            outer = full_bytes
        if not inner:
            inner = full_bytes
        
        return outer, inner
        
    except Exception as e:
        print(f"[captcha] Image extraction error: {str(e)[:80]}")
        return None, None


def solve_tiktok_rotate_captcha(page, username: str = "") -> bool:
    """
    Detects and solves TikTok rotate/whirl captcha using our offline OpenCV solver.
    Returns True if solved (or no captcha was present).
    """
    if not CAPTCHA_SOLVER_AVAILABLE:
        print("[captcha] Solver not loaded — skipping")
        return False
    
    try:
        captcha_type = _detect_tiktok_captcha(page)
        if not captcha_type:
            return True  # no captcha
        
        print(f"[{username}] CAPTCHA DETECTED: {captcha_type}")
        update_account(username, current_task="Solving captcha...")
        
        if captcha_type == "rotate":
            outer, inner = _extract_rotate_images(page)
            
            if not outer or not inner:
                print(f"[{username}] Failed to extract captcha images")
                return False
            
            # Use the robust version (edge continuity + feature matching)
            angle, conf = solve_rotate_captcha_robust(outer, inner, debug=True)
            
            if abs(angle) < 2:
                print(f"[{username}] Very small angle ({angle}°), might already be aligned")
            
            print(f"[{username}] Solved angle: {angle}° (confidence: {conf}%)")
            
            # === Simulate the rotation on TikTok ===
            # TikTok rotate captchas usually have a circular handle or slider
            try:
                # Find the slider / drag handle
                slider = page.locator(
                    '[data-e2e*="slider"], .slider, input[type=range], '
                    'div[role="slider"], .captcha-slider, button[aria-label*="slide"]'
                ).first
                
                if slider.count() == 0:
                    # Fallback: look for the round draggable element
                    slider = page.locator('div[style*="cursor"], circle, [class*="handle"]').first
                
                if slider.count() > 0:
                    box = slider.bounding_box(timeout=5000)
                    if box:
                        # TikTok: drag distance ≈ (angle / 360) * slider_width
                        slider_width = box['width'] or 280
                        drag_distance = (angle / 360.0) * slider_width * 1.05
                        
                        # Start from left side of slider
                        start_x = box['x'] + 15
                        start_y = box['y'] + box['height'] / 2
                        
                        # Perform human-like drag
                        page.mouse.move(start_x, start_y)
                        page.mouse.down()
                        time.sleep(0.12)
                        
                        # Smooth drag
                        steps = max(8, int(abs(drag_distance) / 18))
                        for i in range(steps):
                            progress = (i + 1) / steps
                            curr_x = start_x + (drag_distance * progress)
                            page.mouse.move(curr_x, start_y, steps=1)
                            time.sleep(0.018)
                        
                        page.mouse.up()
                        time.sleep(1.2)
                        
                        print(f"[{username}] Dragged slider by ~{drag_distance:.0f}px for {angle}°")
                    else:
                        print(f"[{username}] Could not get slider box")
                else:
                    # Alternative: try to rotate by clicking/dragging directly on the circle
                    print(f"[{username}] No slider found — trying direct circle drag")
                    circle = page.locator('canvas, .captcha-circle, [class*="rotate-container"]').first
                    if circle.count() > 0:
                        cbox = circle.bounding_box()
                        if cbox:
                            cx = cbox['x'] + cbox['width']/2
                            cy = cbox['y'] + cbox['height']/2
                            page.mouse.move(cx, cy)
                            page.mouse.down()
                            # drag in arc
                            for i in range(12):
                                rad = math.radians(angle * (i/12))
                                nx = cx + math.cos(rad) * 80
                                ny = cy + math.sin(rad) * 80
                                page.mouse.move(nx, ny)
                                time.sleep(0.04)
                            page.mouse.up()
                            time.sleep(0.8)
                
                # Verify / submit
                time.sleep(2)
                take_screenshot(username)
                
                # Click verify / submit if button appears
                for btn_text in ["Verify", "Submit", "Confirm", "Done"]:
                    try:
                        btn = page.locator(f'button:has-text("{btn_text}"), [data-e2e*="{btn_text.lower()}"]').first
                        if btn.count() > 0 and btn.is_visible():
                            btn.click(timeout=4000)
                            time.sleep(1.5)
                            break
                    except:
                        continue
                
                print(f"[{username}] Rotate captcha solution submitted")
                time.sleep(2.5)
                return True
                
            except Exception as drag_err:
                print(f"[{username}] Drag simulation error: {str(drag_err)[:70]}")
                return False
        
        return False  # unsupported captcha type for now
        
    except Exception as e:
        print(f"[{username}] Captcha solver error: {str(e)[:90]}")
        return False


def handle_captcha_if_present(page, username: str) -> bool:
    """Safe wrapper. Returns True if no captcha or captcha was handled.
    Gracefully handles missing page."""
    if page is None:
        return True
    try:
        # Quick check
        captcha_type = _detect_tiktok_captcha(page)
        if not captcha_type:
            return True

        print(f"[{username}] Captcha detected — attempting to solve...")

        solved = solve_tiktok_rotate_captcha(page, username)

        if solved:
            # Give TikTok time to process
            time.sleep(3)
            # Check if captcha is gone
            if _detect_tiktok_captcha(page) is None:
                print(f"[{username}] ✓ Captcha solved successfully")
                update_account(username, current_task="Captcha solved")
                return True
            else:
                print(f"[{username}] Captcha still present after solve attempt")
                return False
        return False
    except Exception as e:
        print(f"[{username}] handle_captcha error: {e}")
        return False


# -----------------------------------------------------------------
# NEW: Auto "Turn on" for TikTok automatic content checks dialog
# -----------------------------------------------------------------
def handle_content_check_dialog(page, username: str = "") -> bool:
    """Extremely persistent 'Turn on' button clicker.
    Keeps trying until it succeeds or times out. Aggressive multi-strategy version.
    """
    if page is None:
        return False

    try:
        print(f"[{username}] Looking for 'Turn on automatic content checks?' dialog...")

        dialog_found = False

        # Give the dialog time to appear after posting (more persistent)
        for _ in range(10):
            try:
                # Broad search for dialog containers
                possibles = page.locator('div[role="dialog"], [aria-modal="true"], div[class*="modal"], div[class*="dialog"], [data-e2e*="dialog"]')
                for i in range(min(possibles.count(), 6)):
                    try:
                        d = possibles.nth(i)
                        if d.count() > 0 and d.is_visible():
                            txt = (d.inner_text(timeout=900) or "").lower()
                            if ("turn on" in txt or "turnon" in txt) and ("content" in txt or "automatic" in txt or "check" in txt or "auto" in txt or "verify" in txt):
                                print(f"[{username}] Found the content check dialog (text match)")
                                dialog_found = True
                                break
                            # Fallback: dialog contains a Turn on button
                            turn_btns = d.locator('button:has-text("Turn on"), button:has-text("Turn On"), [role="button"]:has-text("Turn")')
                            if turn_btns.count() > 0:
                                print(f"[{username}] Found dialog with Turn on button inside")
                                dialog_found = True
                                break
                    except:
                        pass
                if dialog_found:
                    break
            except:
                pass
            time.sleep(random.uniform(0.5, 1.0))

        if not dialog_found:
            # Last resort body text scan
            try:
                body_txt = (page.inner_text("body", timeout=1500) or "").lower()
                if "turn on" in body_txt and ("content" in body_txt or "automatic" in body_txt or "check" in body_txt):
                    print(f"[{username}] Detected content check dialog via body text")
                    dialog_found = True
            except:
                pass

        if not dialog_found:
            print(f"[{username}] No content check dialog detected")
            return False

        print(f"[{username}] Dialog visible — aggressively hammering the 'Turn on' button...")

        # Try very hard for ~22 seconds
        end = time.time() + 22
        attempts = 0
        clicked = False

        while time.time() < end and not clicked:
            attempts += 1
            try:
                # Strategy 1: Direct "Turn on" variants
                for txt_variant in ["Turn on", "Turn On", "TURN ON", "turn on"]:
                    try:
                        btn = page.locator(f'button:has-text("{txt_variant}"), [role="button"]:has-text("{txt_variant}")').first
                        if btn.count() > 0 and btn.is_visible():
                            btn.click(timeout=2200, force=True, no_wait_after=True)
                            print(f"[{username}] ✓ Clicked '{txt_variant}' button (direct)")
                            clicked = True
                            break
                    except:
                        pass
                    if clicked: break
                if clicked: break

                # Strategy 2: Buttons with "Turn" (broad)
                try:
                    btns = page.locator('button, [role="button"], div[role="button"]')
                    for k in range(min(btns.count(), 10)):
                        b = btns.nth(k)
                        if b.count() > 0 and b.is_visible():
                            bt = (b.inner_text(timeout=600) or "").lower().strip()
                            if "turn" in bt and ("on" in bt or len(bt) < 15):
                                b.click(timeout=1800, force=True, no_wait_after=True)
                                print(f"[{username}] ✓ Clicked button w/ Turn: '{bt[:35]}'")
                                clicked = True
                                break
                except:
                    pass
                if clicked: break

                # Strategy 3: Red/primary TikTok action buttons
                for red_sel in [
                    'button[style*="255, 0, 80"]', 'button[style*="ff0050"]',
                    'button[style*="fe2c55"]', 'button[class*="primary"]',
                    'button[class*="red"]', 'button.bg-red-500',
                    '[data-e2e*="post"] button', 'button[data-e2e="post-button"]'
                ]:
                    try:
                        red = page.locator(red_sel).first
                        if red.count() > 0 and red.is_visible():
                            red.click(timeout=2000, force=True)
                            print(f"[{username}] ✓ Clicked red/primary button ({red_sel[:40]})")
                            clicked = True
                            break
                    except:
                        pass
                if clicked: break

                # Strategy 4: Last button in any visible dialog
                try:
                    dlgs = page.locator('div[role="dialog"], [aria-modal="true"]')
                    for di in range(min(dlgs.count(), 4)):
                        d = dlgs.nth(di)
                        if d.count() > 0 and d.is_visible():
                            d_btns = d.locator('button, [role="button"]')
                            if d_btns.count() > 0:
                                last = d_btns.nth(d_btns.count() - 1)
                                if last.is_visible():
                                    last.click(timeout=1800, force=True)
                                    print(f"[{username}] ✓ Clicked last button in dialog")
                                    clicked = True
                                    break
                except:
                    pass
                if clicked: break

                # Strategy 5: Coordinate click on lower right of dialog
                try:
                    dlg = page.locator('div[role="dialog"], [aria-modal="true"]').first
                    if dlg.count() > 0 and dlg.is_visible():
                        box = dlg.bounding_box(timeout=1200)
                        if box:
                            x = box['x'] + box['width'] * random.uniform(0.68, 0.92)
                            y = box['y'] + box['height'] * random.uniform(0.62, 0.88)
                            page.mouse.click(x, y)
                            print(f"[{username}] ✓ Coordinate-clicked dialog lower-right")
                            clicked = True
                except:
                    pass
                if clicked: break

                # Strategy 6: JavaScript click (bypasses many overlay issues)
                try:
                    result = page.evaluate('''
                        const all = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"], .tiktok-button, a[role="button"]'));
                        let target = all.find(b => {
                            const t = (b.innerText || b.textContent || "").toLowerCase().trim();
                            return t.includes("turn on") || (t.includes("turn") && t.includes("on"));
                        });
                        if (!target) {
                            target = all.find(b => {
                                const s = ((b.getAttribute("style")||"") + " " + (b.className||"")).toLowerCase();
                                return s.includes("255,0,80") || s.includes("ff0050") || s.includes("primary") || s.includes("red");
                            });
                        }
                        if (target) { target.click(); return "clicked"; }
                        return "no-match";
                    ''')
                    if result == "clicked":
                        print(f"[{username}] ✓ JS-forced click on Turn on / primary")
                        clicked = True
                except:
                    pass

            except Exception as ie:
                pass

            if not clicked:
                time.sleep(random.uniform(0.45, 0.95))

        if clicked:
            print(f"[{username}] ✓ Content check dialog handled (after {attempts} attempts)")
            time.sleep(2.5)
            return True

        print(f"[{username}] Gave up trying to click 'Turn on' after {attempts} attempts")
        return False

    except Exception as e:
        print(f"[{username}] Content check dialog error: {str(e)[:80]}")
        return False

# Force unbuffered output so Railway logs show prints immediately
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

def log(msg):
    """Print with flush for Railway logs"""
    print(msg, flush=True)

workers = {}
browser_sessions = {}
screenshots = {}
last_frame_ts = {}

DOWNLOADS_DIR = "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

TIKWM_API = "https://www.tikwm.com/api/"
TIKWM_SEARCH_API = "https://www.tikwm.com/api/feed/search"

# Exact, accurate posting interval. The worker waits until this many seconds
# have elapsed since the previous successful post, so the gap between posts is
# always ~5 minutes.
POST_INTERVAL_SECONDS = 300  # 5 minutes

# How many of the top-ranked candidates to randomly choose between, so we never
# keep picking the exact same viral video every cycle.
VIDEO_CHOICE_POOL = 6

# Exact, good YouTube Shorts resolution (vertical 9:16, full HD portrait).
YOUTUBE_SHORTS_WIDTH = 1080
YOUTUBE_SHORTS_HEIGHT = 1920

# Exact, good video DURATION (in seconds) per platform. Clips are trimmed/padded
# to land inside these exact bounds so every video is a clean, well-sized Short.
TIKTOK_MIN_SEC = 12
TIKTOK_MAX_SEC = 55
YOUTUBE_MIN_SEC = 20
YOUTUBE_MAX_SEC = 58

# Category key -> the ACTUAL TikTok search query used (search page + tikwm API).
# Keys are the exact display labels chosen in the dashboard (so what you pick
# is what gets stored and searched). TikTok ALWAYS does the searching; YouTube
# accounts reuse the same TikTok search to source clips, then upload to YouTube.
CATEGORY_SEARCH = {
    "Dance": "dance",
    "Horror": "horror",
    "Viral Clips": "viral",
    "Funny Clips": "funny",
    "Scary Story Animation": "Scary Story Animation",
    "Fruit Story Animation": "Fruit Story Animation",
    "Horror Animations": "Horror Animations",
    "Edits": "edits",
    "Story Animation": "Horror Story Animation",
    "Gin Stories": "Jinn stories Islam",
    "Scary facts": "Scary Facts",
    "Funny Videos": "Funny Videos",
    "Predator Catches": "Pred catches",
}

# Categories offered when adding a YouTube account.
YOUTUBE_CATEGORIES = [
    "Viral Clips",
    "Funny Clips",
    "Scary Story Animation",
    "Fruit Story Animation",
    "Horror Animations",
    "Edits",
]

# Hashtag pools per category used to enrich captions.
# Keys are LOWERCASE (generate_caption lowercases the category before lookup).
CATEGORY_HASHTAGS = {
    "horror": ["#horror", "#scary", "#horrortok", "#creepy", "#scarystories", "#fyp", "#viral"],
    "dance": ["#dance", "#dancechallenge", "#dancer", "#trending", "#fyp", "#viral"],
    "story animation": ["#storyanimation", "#horrorstory", "#horror", "#scary", "#animation", "#fyp", "#viral"],
    "gin stories": ["#jinns", "#islam", "#jinnstories", "#supernatural", "#unseen", "#fyp", "#viral"],
    "scary facts": ["#scaryfacts", "#facts", "#didyouknow", "#scary", "#learn", "#fyp", "#viral"],
    "funny videos": ["#funny", "#memes", "#lol", "#comedyvideos", "#fyp", "#viral"],
    "predator catches": ["#predator", "#predcatch", "#wildlife", "#animal", "#nature", "#fyp", "#viral"],
    "food": ["#food", "#foodtok", "#recipe", "#cooking", "#foodie", "#fyp", "#viral"],
    "fitness": ["#fitness", "#gym", "#workout", "#fitnessmotivation", "#fyp", "#viral"],
    "pets": ["#pets", "#dogsoftiktok", "#catsoftiktok", "#animals", "#fyp", "#viral"],
    "motivation": ["#motivation", "#mindset", "#success", "#inspiration", "#fyp", "#viral"],
    "viral clips": ["#viral", "#fyp", "#trending", "#shorts", "#viralvideo", "#foryoupage"],
    "funny clips": ["#funny", "#comedy", "#lol", "#shorts", "#funnyvideo", "#fyp"],
    "scary story animation": ["#scarystory", "#animation", "#horror", "#storytime", "#shorts", "#scary"],
    "fruit story animation": ["#fruit", "#animation", "#kids", "#story", "#shorts", "#cute"],
    "horror animations": ["#horror", "#animation", "#scary", "#horrorstory", "#shorts", "#creepy"],
    "edits": ["#edits", "#edit", "#aesthetic", "#trending", "#shorts", "#fyp"],
}


def create_placeholder(username, text):
    account = get_account(username) if username else None
    platform = (account.get("platform") if account else None) or "TikTok"
    img = Image.new("RGB", (800, 450), "#111111")
    from PIL import ImageDraw, ImageFont
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except Exception:
        font = ImageFont.load_default()
    draw.text((30, 30), f"{platform} - {username}", fill="#ff0050", font=font)
    draw.text((30, 80), text, fill="white", font=font)
    draw.text((30, 400), datetime.now().strftime("%H:%M:%S"), fill="#888", font=font)
    return img


def take_screenshot(username):
    """Capture a preview frame and store it as a PIL Image for the /live route.

    SAFETY: Playwright's sync API is bound to the thread that started the
    browser. This function ONLY runs Playwright calls when called from that
    owner thread; if invoked from another thread (e.g. the Flask /live route)
    it returns immediately so we never hit 'cannot switch to a different thread'.
    Falls back to the last good frame on error.
    """
    session = browser_sessions.get(username)
    if not session:
        screenshots[username] = create_placeholder(username, "No browser")
        return

    owner = session.get("owner_thread")
    if owner is not None and owner is not threading.current_thread():
        # Called from a non-owner thread (e.g. Flask request) — never touch
        # Playwright here. Just keep the existing preview frame.
        return

    try:
        page = session.get("page")
        if page is None or page.is_closed():
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
        # Playwright returns raw PNG bytes -> convert to a PIL Image.
        screenshot_bytes = page.screenshot(type="png", timeout=15000)
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        screenshots[username] = img
        last_frame_ts[username] = time.time()
    except Exception as e:
        err = str(e).split("\n")[0][:60]
        # Keep the last good frame instead of replacing it with an error card.
        if username not in screenshots:
            screenshots[username] = create_placeholder(username, f"Screenshot error: {err}")
        print(f"[{username}] screenshot error: {err}")


# Persistent browser profiles: each account keeps its own profile dir so
# cookies / login sessions survive across runs (no manual re-login needed).
PROFILE_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playwright-profile")

def _profile_dir_for(username):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", username or "default")
    return os.path.join(PROFILE_BASE, safe)

def _clear_stale_profile_lock(profile_dir):
    """Remove a leftover SingletonLock / SingletonCookie so a crashed previous
    run doesn't make Chromium refuse the profile ('Target ... has been closed').
    Only removes the lock files, never your cookies or the profile itself."""
    try:
        import glob
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            lock = os.path.join(profile_dir, name)
            if os.path.exists(lock):
                os.remove(lock)
        # Sometimes the lock lives one level deeper in a 'Default' subdir.
        for lock in glob.glob(os.path.join(profile_dir, "*", "SingletonLock")):
            try: os.remove(lock)
            except Exception: pass
    except Exception:
        pass

def _chrome_channel_available():
    """Return 'chrome' if a real Google Chrome is installed, else None."""
    try:
        import shutil
        for c in ("google-chrome", "google-chrome-stable", "chrome"):
            if shutil.which(c):
                return "chrome"
    except Exception:
        pass
    return None

def _save_session_from_context(username, context):
    """Persist the authenticated cookies from a live context into the DB so the
    next run can reuse the session without logging in again."""
    try:
        cookies = context.cookies()
        if cookies:
            update_account(username, session_data=json.dumps(cookies))
            _log_event(username, f"Saved {len(cookies)} cookies from persistent profile")
            return True
    except Exception as e:
        _log_event(username, f"save session err: {e}")
    return False

def _cookie_domain_for(c, platform):
    """Pick a sane default cookie domain based on the platform."""
    if platform == "YouTube":
        return c.get("domain", ".youtube.com")
    return c.get("domain", ".tiktok.com")


def _get_proxy(account=None):
    """Build a Playwright proxy dict from (in priority order):
       1) account['proxy']  (DB field, full URL e.g. http://1.2.3.4:8080)
       2) env PROXY          (full URL)
       3) env PROXY_IP + PROXY_PORT  (e.g. your home IP 84.215.85.106:PORT)
    Returns a dict like {"server": "http://ip:port"} or None.
    Routing the browser through a residential IP (instead of the server's
    datacenter IP) greatly reduces YouTube's 'Verify that it's you' prompts.
    """
    proxy = None
    if account and isinstance(account, dict):
        proxy = account.get("proxy")
    if not proxy:
        proxy = os.environ.get("PROXY")
    if not proxy:
        ip = os.environ.get("PROXY_IP")
        port = os.environ.get("PROXY_PORT")
        if ip and port:
            proxy = f"http://{ip}:{port}"
    if not proxy:
        return None
    return {"server": proxy}


def _start_browser_session(username, account=None, no_proxy=False):
    """Launch a Playwright browser + context and store it in browser_sessions.
    Closes any pre-existing session for this username first. Returns the
    session dict (with 'context'/'page') or None on failure.

    If a proxy is configured (env PROXY / PROXY_IP+PROXY_PORT, or an account
    `proxy` field), the browser routes through it — useful for avoiding
    YouTube bot-verification by using a residential IP. Pass no_proxy=True to
    force a DIRECT connection (used as a fallback when the proxy is unreachable,
    so a bad proxy setting never kills the whole bot).
    """
    profile_dir = _profile_dir_for(username)
    os.makedirs(profile_dir, exist_ok=True)
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            browser_sessions[username]["pw"].stop()
        except Exception:
            pass
        del browser_sessions[username]

    proxy = None if no_proxy else _get_proxy(account)
    pw = sync_playwright().start()
    channel = _chrome_channel_available()
    # launch_persistent_context keeps cookies/localStorage in `profile_dir`
    # between runs, so a logged-in YouTube/Google session is reused. We run
    # HEADED (headless=False) because you log in MANUALLY in the visible window
    # (Google blocks headless automation logins). On a headless server this must
    # run under a virtual display (xvfb-run) so a window can actually open —
    # the Procfile already wraps gunicorn in xvfb-run, so $DISPLAY is normally
    # present and headed mode works for manual login.
    #
    # IMPORTANT: a leftover SingletonLock from a crashed previous run makes
    # Chromium refuse to reuse the profile and immediately close the context
    # ("Target ... has been closed"). Remove any stale lock before launching.
    _clear_stale_profile_lock(profile_dir)

    base_kwargs = dict(
        user_data_dir=profile_dir,
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        ignore_https_errors=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,720",
        ],
    )
    if channel:
        base_kwargs["channel"] = channel
    if proxy:
        base_kwargs["proxy"] = proxy
        print(f"[{username}] launching persistent browser (profile={profile_dir}) via proxy: {proxy['server']}")
    else:
        print(f"[{username}] launching persistent browser (profile={profile_dir}) DIRECT")

    # Try HEADED first (so you can log in manually). If that fails — e.g. no
    # display available — fall back to headless so the app still runs; manual
    # login then isn't possible, but cookie sessions still work.
    context = None
    last_err = None
    for attempt_headless in (False, True):
        launch_kwargs = dict(base_kwargs, headless=attempt_headless)
        try:
            context = pw.chromium.launch_persistent_context(**launch_kwargs)
            if attempt_headless:
                print(f"[{username}] NOTE: headed launch failed (no display?); fell back to headless. Manual Google login needs a display (run under xvfb-run).")
            break
        except Exception as le:
            last_err = le
            print(f"[{username}] persistent launch (headless={attempt_headless}) failed: {le}")
            # If the chrome channel was the problem, drop it and retry once.
            if "channel" in launch_kwargs:
                base_kwargs.pop("channel", None)
                try:
                    context = pw.chromium.launch_persistent_context(**dict(base_kwargs, headless=attempt_headless))
                    if attempt_headless:
                        print(f"[{username}] NOTE: fell back to headless (no display). Manual login needs xvfb-run.")
                    break
                except Exception as le2:
                    last_err = le2
                    print(f"[{username}] retry without chrome channel also failed: {le2}")
            continue
    if context is None:
        raise last_err or RuntimeError("Could not launch persistent browser")

    # launch_persistent_context returns a BrowserContext directly (no separate
    # browser object). We keep `browser` === context so all existing
    # session["browser"].close() / session["context"].close() calls keep working.
    page = context.pages[0] if context.pages else context.new_page()
    try:
        context.add_init_script("""() => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        }""")
    except Exception:
        pass
    # Record the thread that owns this Playwright browser. Playwright's sync API
    # is bound to the thread that called sync_playwright().start(), so ALL
    # Playwright calls for this session MUST happen on this thread.
    session = {"pw": pw, "browser": context, "context": context, "page": page,
               "profile_dir": profile_dir, "owner_thread": threading.current_thread()}
    browser_sessions[username] = session
    return session


def _log_event(username, message):
    """Print + persist a timestamped line to the account's rolling log."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{username}] {message}")
    try:
        append_log(username, message)
    except Exception:
        pass


def connect_account(username):
    """Start headless browser, load cookies, verify login, and KEEP the browser
    alive so the live cam (and the automation worker) can use it immediately.
    Works for both TikTok and YouTube session cookies.
    """
    account = get_account(username)
    if not account or not account.get("session_data"):
        update_account(username, status="No Session", current_task="Please paste session")
        return False

    platform = account.get("platform") or "TikTok"
    update_account(username, status="Connecting", current_task="Starting browser...")
    _log_event(username, f"Connecting to {platform}...")

    try:
        cookies = json.loads(account["session_data"])
    except json.JSONDecodeError:
        update_account(username, status="Invalid Session", current_task="Invalid JSON")
        return False

    try:
        clean_cookies = []
        for c in cookies:
            if not isinstance(c, dict):
                continue
            if "name" not in c or "value" not in c:
                continue
            cleaned = {
                "name": c["name"],
                "value": c["value"],
                "domain": _cookie_domain_for(c, platform),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            }
            if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
                cleaned["sameSite"] = c["sameSite"]
            clean_cookies.append(cleaned)

        if not clean_cookies:
            update_account(username, status="Invalid Session", current_task="No valid cookies found")
            return False

        proxy_cfg = _get_proxy(account)
        session = _start_browser_session(username, account)
        context = session["context"]
        page = session["page"]

        # CORRECT ORDER: add cookies BEFORE creating page and navigating
        context.add_cookies(clean_cookies)

        home_url = "https://www.youtube.com" if platform == "YouTube" else "https://www.tiktok.com"
        update_account(username, current_task="Verifying session...")

        def _goto_home(p, ctx, url):
            p.goto(url, timeout=30000)
            try:
                p.wait_for_load_state("networkidle", timeout=15000)
            except TimeoutError:
                pass

        try:
            _goto_home(page, context, home_url)
        except Exception as ge:
            # If a proxy was configured and the site timed out, the proxy is
            # likely unreachable — retry the whole session DIRECT (no proxy) so a
            # bad proxy setting never kills the bot.
            if proxy_cfg and ("TIMED_OUT" in str(ge) or "net::" in str(ge)):
                print(f"[{username}] proxy goto failed ({ge}); retrying WITHOUT proxy")
                try:
                    context.close(); session["browser"].close(); session["pw"].stop()
                except Exception:
                    pass
                browser_sessions.pop(username, None)
                session = _start_browser_session(username, account, no_proxy=True)
                context = session["context"]
                page = session["page"]
                context.add_cookies(clean_cookies)
                _goto_home(page, context, home_url)
            else:
                raise
        time.sleep(3)
        handle_captcha_if_present(page, username)
        take_screenshot(username)

        # ---- Platform-specific login verification ----
        logged_in = False
        profile_name = ""

        if platform == "YouTube":
            # YouTube is logged in purely from pasted session cookies — there is
            # no manual Google sign-in step (that never worked headless). The
            # cookies sidestep the "Next" sign-in form entirely.
            # Logged-in YouTube shows the avatar / "You" menu.
            try:
                page.wait_for_selector(
                    'ytd-masthead #avatar-btn, #end #buttons ytd-button-renderer, a[href*="account.google.com"]',
                    timeout=10000,
                )
                # If a Sign in button is present, we are NOT logged in.
                sign_in = page.locator('a[href*="accounts.google.com"] ytd-button-renderer, #buttons ytd-button-renderer:has-text("Sign in")')
                logged_in = sign_in.count() == 0
            except TimeoutError:
                pass
            if not logged_in:
                # Fallback: presence of the "You" / avatar indicates a session
                try:
                    if page.locator('ytd-masthead #avatar-btn, #menu #avatar').count() > 0:
                        logged_in = True
                except Exception:
                    pass
            if logged_in:
                try:
                    av = page.locator('ytd-masthead #avatar-btn img, #menu #avatar img').first
                    if av.count() > 0:
                        alt = (av.get_attribute("alt") or "").strip()
                        # "alt" is usually "Avatar image" — not the real name.
                        # Prefer the channel handle from the account menu instead.
                        if alt and alt.lower() not in ("avatar image", "avatar", ""):
                            profile_name = alt
                except Exception:
                    pass
                # Get the real channel name/handle from the account menu.
                if not profile_name:
                    try:
                        page.locator('ytd-masthead #avatar-btn, #end #avatar-btn').first.click(timeout=4000)
                        time.sleep(1.5)
                        name_el = page.locator('ytd-account-item-section-header-renderer #channel-title, ytd-account-item-section-header-renderer #account-name, #account-item ytd-account-item-section-header-renderer').first
                        if name_el.count() > 0:
                            profile_name = name_el.inner_text(timeout=3000).strip().split("\n")[0]
                        # Also try the @handle link text.
                        if not profile_name:
                            handle_el = page.locator('a[href^="https://www.youtube.com/@"], #account-item a').first
                            if handle_el.count() > 0:
                                h = handle_el.inner_text(timeout=2000).strip()
                                profile_name = h.lstrip("@") or profile_name
                        page.keyboard.press("Escape")
                        time.sleep(0.5)
                    except Exception:
                        pass
        else:
            try:
                page.wait_for_selector(
                    '[data-e2e="profile-icon"], [data-e2e="top-nav-profile"], a[href*="/@"]',
                    timeout=10000,
                )
                logged_in = True
            except TimeoutError:
                pass
            if not logged_in:
                try:
                    login_btn = page.locator('[data-e2e="top-login-button"], a[href*="/login"]')
                    if login_btn.count() == 0:
                        logged_in = True
                except Exception:
                    pass

            if logged_in:
                try:
                    page.goto("https://www.tiktok.com/profile", timeout=15000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except TimeoutError:
                        pass
                    time.sleep(2)
                    current_url = page.url
                    if "/@" in current_url:
                        profile_name = current_url.split("/@")[-1].split("?")[0].split("/")[0]
                    if not profile_name:
                        title_el = page.locator('h1[data-e2e="user-title"], h2[data-e2e="user-subtitle"], [data-e2e="user-title"]').first
                        if title_el.count() > 0:
                            profile_name = title_el.text_content().strip().lstrip("@")
                except Exception:
                    pass
                try:
                    page.goto("https://www.tiktok.com", timeout=30000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except TimeoutError:
                        pass
                    time.sleep(2)
                except Exception as e:
                    print(f"[{username}] warning: could not return to home page: {e}")

        if logged_in:
            task_msg = f"Logged in as {profile_name}" if profile_name else "Session verified"
            update_account(username, connected=1, status="Connected", current_task=task_msg,
                          logged_in_as=profile_name or "")
            print(f"✓ Session verified for {username} ({platform})" + (f" : {profile_name}" if profile_name else ""))
            # Capture ONE preview on the connect thread (safe — this is the owner),
            # then CLOSE the verify browser. The worker will start its own fresh
            # browser on the worker thread, which owns Playwright for the live cam.
            take_screenshot(username)
            try:
                context.close()
                session["browser"].close()
                session["pw"].stop()
            except Exception:
                pass
            browser_sessions.pop(username, None)
        else:
            _log_event(username, "Session NOT verified (expired or invalid cookies)")
            update_account(username, connected=0, status="Session expired", current_task="Please update session")
            print(f"✗ Session expired or invalid for {username} ({platform})")
            # Close the browser so we don't leave a dead session around.
            try:
                context.close()
                session["browser"].close()
                session["pw"].stop()
            except Exception:
                pass
            browser_sessions.pop(username, None)

        return logged_in

    except Exception as e:
        import traceback
        traceback.print_exc()
        update_account(username, status="Error", current_task=f"Error: {str(e)[:50]}")
        return False


# ---------------------------------------------------------------------------
# YouTube session helpers
# ---------------------------------------------------------------------------

def _is_logged_in_youtube(page):
    """Return True if a YouTube/Google session is already authenticated."""
    try:
        # A logged-in masthead shows the avatar; a signed-out one shows Sign in.
        if page.locator('ytd-masthead #avatar-btn').count() > 0:
            return True
        if page.locator('a[href*="accounts.google.com"] ytd-button-renderer, #buttons ytd-button-renderer:has-text("Sign in")').count() == 0:
            return True
    except Exception:
        pass
    return False

# ---------------------------------------------------------------------------
# Real automation helpers# ---------------------------------------------------------------------------
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

    query = CATEGORY_SEARCH.get(category, category)
    update_account(username, current_task=f"Searching TikTok for '{query}'...")

    try:
        q = urllib.parse.quote(query)
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


def find_viral_video(username, category, exclude=None):
    """Step 2: pick a viral video for the category — NEVER one we already posted.

    Gathers several candidates (on-screen search results + the tikwm search
    API), drops any video_id in `exclude`, ranks by engagement, then chooses a
    RANDOM one from the top VIDEO_CHOICE_POOL so every cycle gets a different,
    fresh video. Returns dict or None.
    """
    exclude = exclude or set()
    page = _get_page(username)
    update_account(username, current_task="Scanning results for viral videos...")

    candidates = []

    def _add(info):
        if not info or not info.get("video_id"):
            return
        if info["video_id"] in exclude:   # never repost the same clip
            return
        if info.get("play_count", 0) < 5000:
            return
        candidates.append(info)

    # --- Scrape video links from the browser search results, get their stats ---
    candidate_urls = []
    if page is not None:
        try:
            for _ in range(3):
                page.mouse.wheel(0, 1200)
                time.sleep(1.5)
            take_screenshot(username)
            links = page.eval_on_selector_all('a[href*="/video/"]', "els => els.map(e => e.href)")
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

    for url in candidate_urls[:6]:
        try:
            r = requests.get(TIKWM_API, params={"url": url}, timeout=20)
            data = r.json()
            if data.get("code") != 0:
                time.sleep(1.2)
                continue
            d = data["data"]
            _add({
                "url": url,
                "video_id": str(d.get("id", "")),
                "title": d.get("title", ""),
                "play_count": d.get("play_count", 0),
                "digg_count": d.get("digg_count", 0),
                "play": d.get("hdplay") or d.get("play", ""),
            })
            time.sleep(1.2)  # be polite to the free API
        except Exception as e:
            print(f"[{username}] tikwm detail lookup failed for {url}: {e}")

    # --- Fallback / supplement: tikwm search API (returns stats directly) ---
    try:
        r = requests.post(
            TIKWM_SEARCH_API,
            data={"keywords": CATEGORY_SEARCH.get(category, category), "count": 30, "cursor": 0, "HD": 1},
            timeout=25,
        )
        data = r.json()
        if data.get("code") == 0:
            videos = data.get("data", {}).get("videos", [])
            videos.sort(
                key=lambda v: v.get("play_count", 0) + v.get("digg_count", 0) * 20,
                reverse=True,
            )
            for v in videos:
                if v.get("duration", 0) > 180:  # skip very long videos
                    continue
                author = (v.get("author") or {}).get("unique_id", "unknown")
                _add({
                    "url": f"https://www.tiktok.com/@{author}/video/{v['video_id']}",
                    "video_id": str(v["video_id"]),
                    "title": v.get("title", ""),
                    "play_count": v.get("play_count", 0),
                    "digg_count": v.get("digg_count", 0),
                    "play": v.get("play", ""),
                })
    except Exception as e:
        print(f"[{username}] tikwm search API failed: {e}")

    if not candidates:
        update_account(username, current_task="No new videos found, will retry")
        return None

    # Rank by engagement and pick a RANDOM one from the top pool -> variety
    candidates.sort(
        key=lambda c: c.get("play_count", 0) + c.get("digg_count", 0) * 20,
        reverse=True,
    )
    pool = candidates[:VIDEO_CHOICE_POOL]
    info = random.choice(pool)
    views = info.get("play_count", 0)
    update_account(username, current_task=f"Found viral video ({views:,} views)")
    print(f"[{username}] selected video {info['url']} ({views} views) [pool {len(pool)}/{len(candidates)}]")
    return info


def scrape_profile_videos(username, profile_link, exclude=None):
    """TikTok PROFILE mode: collect videos from ONE specific profile only.

    Navigates to the given profile, scrolls to load more, and returns a list of
    candidate dicts (same shape as find_viral_video) — most-recent first. Already
    posted ids (in `exclude`) are dropped so a clip is never reused. Falls back to
    [] on any failure so the worker can retry.
    """
    exclude = exclude or set()
    page = _get_page(username)
    if page is None:
        return []
    candidates = []
    try:
        url = profile_link
        if not url.startswith("http"):
            url = "https://" + url
        print(f"[{username}] PROFILE MODE: opening {url}")
        update_account(username, current_task="PROFILE MODE: browsing source profile...")
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
        except Exception as ge:
            print(f"[{username}] profile goto err: {str(ge)[:60]}")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        time.sleep(3)
        handle_captcha_if_present(page, username)
        take_screenshot(username)

        # Scroll down to lazy-load more videos.
        for _ in range(5):
            page.mouse.wheel(0, 1500)
            time.sleep(1.5)
        take_screenshot(username)

        links = page.eval_on_selector_all('a[href*="/video/"]', "els => els.map(e => e.href)")
        seen = set()
        for link in links:
            m = re.search(r"tiktok\.com/@[^/]+/video/(\d+)", link)
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                if m.group(1) in exclude:
                    continue
                candidates.append({
                    "url": link.split("?")[0],
                    "video_id": m.group(1),
                    "title": "",
                    "play_count": 0,
                    "digg_count": 0,
                    "play": "",
                    "from_profile": True,
                })
            if len(candidates) >= 20:
                break
        print(f"[{username}] PROFILE MODE: found {len(candidates)} unused videos on profile")
    except Exception as e:
        print(f"[{username}] PROFILE MODE scrape error: {e}")
    return candidates


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


def _get_video_duration_seconds(path):
    """Return the exact duration (float seconds) of a video using OpenCV."""
    try:
        import cv2
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        cap.release()
        if fps > 0 and frames > 0:
            return frames / fps
    except Exception:
        pass
    return 0.0


def prepare_video_for_platform(username, file_path, platform):
    """Return a processed video path sized/trimmed for the target platform.

    For YouTube we FORCE the exact 1080x1920 Shorts resolution (good quality,
    full HD portrait) and an exact, good duration (YOUTUBE_MIN_SEC..MAX_SEC s):
      - too short  -> loop the clip until it reaches the minimum length
      - too long   -> trim to the maximum length at a clean cut
    For TikTok we keep the original (already a vertical short clip).
    Skips re-encoding if the file already meets the constraints.
    """
    if platform != "YouTube":
        return file_path

    try:
        import cv2
    except Exception as e:
        print(f"[{username}] cv2 unavailable, uploading original: {e}")
        return file_path

    update_account(username, current_task="Resizing to exact 1080x1920 Shorts...")

    dur = _get_video_duration_seconds(file_path)
    target_dur = None
    if dur > 0:
        if dur < YOUTUBE_MIN_SEC:
            target_dur = YOUTUBE_MIN_SEC
        elif dur > YOUTUBE_MAX_SEC:
            target_dur = YOUTUBE_MAX_SEC

    safe_user = re.sub(r"[^A-Za-z0-9_-]", "_", username)
    out_path = os.path.join(DOWNLOADS_DIR, f"{safe_user}_yt_{int(time.time())}.mp4")

    cap = cv2.VideoCapture(file_path)
    if not cap.isOpened():
        print(f"[{username}] could not open video for processing")
        return file_path

    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if src_fps <= 0:
        src_fps = 30.0

    W, H = YOUTUBE_SHORTS_WIDTH, YOUTUBE_SHORTS_HEIGHT

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(out_path, fourcc, src_fps, (W, H))

    # Letterbox/pillarbox preserving aspect ratio, centered, black bars.
    scale = min(W / src_w, H / src_h)
    new_w = int(round(src_w * scale))
    new_h = int(round(src_h * scale))
    pad_x = (W - new_w) // 2
    pad_y = (H - new_h) // 2

    max_frames = int(target_dur * src_fps) if target_dur else 0
    written = 0
    loop_count = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            # Loop short clips up to the target duration.
            if target_dur and written < max_frames and loop_count < 50:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                loop_count += 1
                continue
            break
        if target_dur and written >= max_frames:
            break
        resized = cv2.resize(frame, (new_w, new_h))
        canvas = cv2.copyMakeBorder(
            resized, pad_y, H - new_h - pad_y, pad_x, W - new_w - pad_x,
            cv2.BORDER_CONSTANT, value=(0, 0, 0),
        ) if (pad_x or pad_y) else resized
        # Ensure exact output dimensions (handles odd rounding).
        if canvas.shape[1] != W or canvas.shape[0] != H:
            canvas = cv2.resize(canvas, (W, H))
        writer.write(canvas)
        written += 1

    cap.release()
    writer.release()

    if os.path.exists(out_path) and os.path.getsize(out_path) > 10 * 1024:
        final_dur = _get_video_duration_seconds(out_path)
        update_account(username, current_task=f"Shorts ready: {W}x{H}, {final_dur:.0f}s")
        print(f"[{username}] prepared YouTube Short: {out_path} ({W}x{H}, {final_dur:.1f}s)")
        return out_path
    print(f"[{username}] video processing failed, uploading original")
    return file_path


def generate_caption(video_info, category, platform="TikTok"):
    """Generate category-aware captions that actually match the content.

    For YouTube we add #Shorts and write a longer, accurate caption that
    describes the clip (good for retention + search). For TikTok we keep the
    shorter, hashtag-forward style.
    """
    original = (video_info.get("title") or "").strip()
    plain = re.sub(r"#\w+", "", original).strip()
    plain = re.sub(r"\s{2,}", " ", plain)
    if len(plain) > 90:
        plain = plain[:90].rsplit(" ", 1)[0]

    cat = (category or "dance").lower()

    # Category-specific caption templates (makes it feel real). Keys are LOWERCASE.
    templates = {
        "horror": [
            "This actually gave me chills 😱",
            "Would you survive this? 😭",
            "Nightmare fuel fr",
            "I can't unsee this...",
            "This is actually terrifying",
            "POV: you shouldn't have watched this at night",
            plain or "This horror hit different",
        ],
        "dance": [
            "The moves 🔥",
            "This choreography is insane",
            "Trying this rn",
            "Dance of the day",
            "The energy is unmatched",
            plain or "This dance is too good",
        ],
        "story animation": [
            "This horror story animation gave me chills 😱",
            "POV: the story takes a dark turn...",
            "Animation horror hits different",
            plain or "This story animation is wild",
        ],
        "gin stories": [
            "This jinn story is terrifying 😨",
            "Islam teaches us about the unseen 👀",
            "You won't believe this jinn story",
            plain or "Jinn stories always hit different",
        ],
        "scary facts": [
            "This fact gave me chills 🥶",
            "Did you know this? 👀",
            "Scary fact you weren't ready for",
            plain or "This fact is unforgettable",
        ],
        "funny videos": [
            "I can't stop laughing 😂",
            "This is too real",
            "The accuracy 💀",
            plain or "This had me dying",
        ],
        "predator catches": [
            "When the predator strikes 🐊",
            "Nature is brutal 🔥",
            "Caught in the act 📸",
            plain or "This catch was insane",
        ],
        "viral clips": [
            "This clip is blowing up everywhere 🔥",
            "No way this went viral like that",
            "POV: you find the best clip on the internet",
            plain or "This viral clip is insane",
        ],
        "funny clips": [
            "I can't stop laughing 😂",
            "This is too real",
            "The accuracy 💀",
            plain or "This had me dying",
        ],
        "scary story animation": [
            "This scary story animation gave me chills 😱",
            "POV: the story takes a dark turn...",
            "Animation horror hits different",
            plain or "This story animation is wild",
        ],
        "fruit story animation": [
            "This fruit story animation is so wholesome 🍓",
            "POV: the cutest fruit story ever",
            "Animation stories always hit different",
            plain or "This fruit story is adorable",
        ],
        "horror animations": [
            "This horror animation gave me chills 😱",
            "POV: the demon appears...",
            "Horror animation hits different",
            plain or "This horror animation is wild",
        ],
        "edits": [
            "This edit is fire 🔥",
            "The transition tho…",
            "Best edit I've seen all day",
            plain or "This edit goes hard",
        ],
    }

    base = random.choice(templates.get(cat, templates["dance"]))

    # Add relevant hashtags
    pool = CATEGORY_HASHTAGS.get(cat, ["#fyp", "#viral", "#trending"])

    if platform == "YouTube":
        # YouTube Shorts: longer, accurate, searchable caption + #Shorts.
        extra = ["#Shorts", "#YouTubeShorts", "#viralshorts"]
        all_tags = list(set(pool + extra))[:8]
        # Use the real video topic when available; otherwise a generic hook.
        # NOTE: never repeat `base` (it's already the hook line above).
        topic = plain or "Watch till the end!"
        caption = (
            f"{base}\n\n{topic}\n\n"
            f"Drop a like and subscribe for more {cat} "
            f"shorts every day! 🔔\n\n"
            f"{' '.join(all_tags)}"
        )
        return caption.strip()[:300]

    extra = ["#fyp", "#foryou", "#viral"]
    all_tags = list(set(pool + extra))[:6]

    caption = f"{base} {' '.join(all_tags)}"
    return caption.strip()[:150]


def _save_debug_html(page, label, username):
    """Save full page HTML for debugging (PRE-POST, ATTEMPT, etc)."""
    try:
        os.makedirs("debug_htmls", exist_ok=True)
        html = page.content()
        ts = int(time.time())
        path = f"debug_htmls/{username}_{label}_{ts}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        latest = f"debug_htmls/LATEST_{label}.html"
        with open(latest, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[{username}] DEBUG HTML saved: {path}", flush=True)
        return path
    except Exception as e:
        print(f"[{username}] debug_html save error: {e}", flush=True)
        return None

def _save_debug_screenshot(page, label, username):
    """Explicit debug screenshot to disk (before/after/+5s)."""
    try:
        os.makedirs("debug_htmls", exist_ok=True)
        ts = int(time.time())
        path = f"debug_htmls/{username}_{label}_{ts}.png"
        page.screenshot(path=path, timeout=8000)
        print(f"[{username}] DEBUG SCREENSHOT: {path}", flush=True)
        return path
    except Exception as e:
        print(f"[{username}] debug_screenshot error: {e}", flush=True)
        return None

# ---------------------------------------------------------------------------
# Robust TikTok upload helpers (iframe-aware + accurate 100% + real DOM click)
# ---------------------------------------------------------------------------
def _iter_frame_factories(page):
    """Yield locator factories for the main page and every (same-origin) iframe."""
    yield page
    try:
        for f in page.frames:
            yield f
    except Exception:
        pass


def _find_file_input(page):
    """Return a file <input> locator (main page or iframe), or None."""
    for factory in _iter_frame_factories(page):
        try:
            loc = factory.locator('input[type="file"]').first
            if loc.count() > 0:
                return loc
        except Exception:
            pass
    return None


_POST_SELECTORS = [
    'button[data-e2e="post_video_button"]',
    'button[class*="Button__root--type-primary"]',
    'button[class*="TUXButton--primary"]',
    'button[class*="btn-post"]',
    'button:has-text("Post")',
    'button:has-text("Publish")',
    'button:has-text("Publiser")',
    'button:has-text("Post now")',
]


def _find_post_button(page):
    """Return the first visible Post button locator (main page or iframe), or None."""
    best = None
    for factory in _iter_frame_factories(page):
        for sel in _POST_SELECTORS:
            try:
                loc = factory.locator(sel).first
                if loc.count() > 0:
                    # Prefer the exact data-e2e button if present
                    if sel == 'button[data-e2e="post_video_button"]':
                        return loc
                    if best is None and loc.is_visible():
                        best = loc
            except Exception:
                pass
    return best


def _button_can_post(loc):
    """True only if the post-button locator is present, visible AND not disabled.

    Handles TikTok's several disable signals, and treats MISSING attributes as
    enabled (the old code treated a missing attribute as disabled, so the button
    was considered never-ready and clicks landed on a disabled React button).
    """
    try:
        if loc.count() == 0:
            return False
        loc = loc.first
        if not loc.is_visible():
            return False
        info = loc.evaluate("""e => {
            const g = n => (e.getAttribute(n) || '').toLowerCase();
            return {
                d: !!e.disabled,
                aria: g('aria-disabled'),
                dd: g('data-disabled'),
                dl: g('data-loading'),
                cls: (e.className || '').toLowerCase(),
            };
        }""")
        if info["d"]:
            return False
        if info["aria"] == "true":
            return False
        if info["dd"] == "true":
            return False
        if info["dl"] == "true":
            return False
        if "disabled" in info["cls"]:
            return False
        return True
    except Exception:
        return False


def _read_upload_percent(page):
    """Return the highest visible upload percentage (0-100) or None if none shown."""
    best = None
    for factory in _iter_frame_factories(page):
        try:
            val = factory.evaluate("""() => {
                const re = /(\\d+)\\s*%/;
                let max = -1;
                for (const el of document.querySelectorAll('*')) {
                    if (el.children.length === 0) {
                        const m = (el.textContent || '').trim().match(re);
                        if (m) { const v = parseInt(m[1], 10); if (v > max) max = v; }
                    }
                }
                return max < 0 ? null : max;
            }""")
            if isinstance(val, int) and val > 0:
                if best is None or val > best:
                    best = val
        except Exception:
            pass
    return best


def _dismiss_blockers(page, username):
    """Dismiss the common TikTok upload-page popups that can block the Post button."""
    blockers = [
        'button[data-e2e="cookie_banner_button"]',
        '//button[./div[text()="Not now"]]',
        'button:has-text("Not now")',
        'button:has-text("Skip")',
        '[class*="joyride"] button',
        '[class*="modal"] button[aria-label="Close"]',
        'button:has-text("Maybe later")',
        'button:has-text("Got it")',
    ]
    for sel in blockers:
        try:
            blk = page.locator(sel).first
            if blk.count() > 0 and blk.is_visible():
                blk.click(timeout=1500, force=True)
                print(f"[{username}] dismissed blocker: {sel}")
                time.sleep(0.4)
        except Exception:
            pass


def _click_post_robust(page, loc, username):
    """Click the Post button reliably. Returns (clicked_bool, method_str).

    Order:
      1. real Playwright click (verifies actionability, no overlay)
      2. click inner .Button__content (exact node from user's pasted HTML)
      3. JS .click() on the ACTUAL node (bypasses any overlay/interception)
      4. coordinate click ONLY if elementFromPoint confirms the button is on top
    """
    btn = loc.first

    # 1) real click
    try:
        btn.scroll_into_view_if_needed(timeout=3000)
        time.sleep(0.2)
        btn.click(timeout=8000)
        print(f"[{username}] ✓ 1. real click (Playwright) on Post")
        return True, "real-click"
    except Exception as e1:
        print(f"[{username}] 1. real click failed: {str(e1)[:80]}")

    # 2) inner content element
    try:
        content = btn.locator('div[class*="Button__content"]').first
        if content.count() > 0:
            content.click(timeout=6000, force=True)
            print(f"[{username}] ✓ 2. clicked inner .Button__content (Publiser)")
            return True, "inner-content"
    except Exception as e2:
        print(f"[{username}] 2. inner content click failed: {str(e2)[:80]}")

    # 3) JS .click() on the actual DOM node (overlay-proof)
    try:
        res = page.evaluate("""() => {
            const b = document.querySelector('button[data-e2e="post_video_button"]')
                    || document.querySelector('button[class*="Button__root--type-primary"]')
                    || document.querySelector('button[class*="TUXButton--primary"]')
                    || [...document.querySelectorAll('button')].find(x => /post|publis/i.test((x.textContent||'').trim()));
            if (!b) return 'no-btn';
            b.click();
            return 'js-click';
        }""")
        if res == "js-click":
            print(f"[{username}] ✓ 3. JS .click() on real Post node (overlay-proof)")
            return True, "js-click"
    except Exception as e3:
        print(f"[{username}] 3. JS click failed: {str(e3)[:80]}")

    # 4) coordinate click ONLY if the button is actually the top element
    try:
        bb = btn.bounding_box(timeout=2500)
        if bb:
            cx = bb["x"] + bb["width"] / 2
            cy = bb["y"] + bb["height"] / 2
            top = page.evaluate(
                "(x,y)=>{const el=document.elementFromPoint(x,y);"
                "return el?(el.getAttribute('data-e2e')||el.tagName):null;}", cx, cy)
            if top == "post_video_button":
                page.mouse.click(cx, cy)
                print(f"[{username}] ✓ 4. coordinate click (button confirmed on top)")
                return True, "coord-click"
            else:
                print(f"[{username}] ⚠ 4. skipped coord click: top element = {top} (overlay)")
    except Exception as e4:
        print(f"[{username}] 4. coord click failed: {str(e4)[:80]}")

    return False, "none"


def _handle_continue_to_post(page, username):
    """Newer TikTok shows a 'Continue to post?' dialog after clicking Post.
    Click its primary confirm button if present. Returns True if handled."""
    confirms = [
        'button:has-text("Post now")',
        'button:has-text("Continue")',
        'button:has-text("Continue to post")',
        'button[data-e2e="post_button"]',
        'div[role="dialog"] button:has-text("Post")',
    ]
    for sel in confirms:
        try:
            b = page.locator(sel).first
            if b.count() > 0 and b.is_visible():
                b.click(timeout=3000, force=True)
                print(f"[{username}] ✓ handled 'Continue to post?' via: {sel}")
                time.sleep(2)
                return True
        except Exception:
            pass
    return False


def upload_video_to_tiktok(username, file_path, caption):
    """TikTok Upload & Post (robust: iframe-aware, accurate 100% wait, real DOM click).
    Waits until the upload truly reaches 100% AND the Post button is enabled, then
    clicks the real button node (overlay-proof) and verifies the publish happened.
    """
    page = _get_page(username)
    if page is None:
        print(f"[{username}] No page available")
        return False

    # Selectors still used as fallbacks (primary lookups are iframe-aware helpers)
    FILE = 'input[type="file"]'
    CAP = '//div[@contenteditable="true"]'
    SUCCESS = 'text=/Your video has been uploaded|Video published|Publiser|视频已发布|uploaded successfully/i'

    nets = []

    def nr(r):
        if any(k in r.url.lower() for k in ["post", "publish", "upload", "/api/", "tiktok.com/creator"]):
            nets.append(f"REQ {r.method} {r.url[:110]}")

    def ns(r):
        if any(k in r.url.lower() for k in ["post", "publish", "video", "upload"]):
            try:
                status = getattr(r, 'status', '?')
                nets.append(f"RESP {status} {r.url[:90]}")
            except:
                pass

    try:
        page.on("request", nr)
        page.on("response", ns)

        print(f"[{username}] === VERIFIED UPLOAD FLOW (accurate 100% + exact button) ===")
        update_account(username, current_task="Opening verified upload...")

        # Navigate to verified upload URL (prefer creator-center)
        upload_url_used = None
        for u in ["https://www.tiktok.com/upload?lang=en",
                  "https://www.tiktok.com/creator-center/upload?lang=en",
                  "https://www.tiktok.com/tiktokstudio/upload"]:
            try:
                print(f"[{username}] Trying: {u}")
                page.goto(u, timeout=48000, wait_until="domcontentloaded")
                time.sleep(random.uniform(3.0, 5.5))
                take_screenshot(username)
                if "/login" in page.url.lower() or "signin" in page.url.lower():
                    continue
                # Check for file input (main OR iframe) — iframe-aware
                if _find_file_input(page) is not None:
                    print(f"[{username}] ✓ file input found on {u}")
                    upload_url_used = u
                    break
                else:
                    print(f"[{username}] no file input on {u}")
            except Exception as g:
                print(f"[{username}] goto err: {str(g)[:60]}")
                import traceback
                traceback.print_exc()
        else:
            print(f"[{username}] ❌ No file input found on any upload URL")
            _save_debug_html(page, "NO_FILE_INPUT", username)
            _save_debug_screenshot(page, "no_file", username)
            take_screenshot(username)
            return False

        print(f"[{username}] Using upload page: {upload_url_used}")
        print(f"[{username}] Current URL before upload: {page.url}")

        # Upload the file (iframe-aware: the <input> may live inside an iframe)
        print(f"[{username}] Uploading file...")
        file_input = _find_file_input(page)
        if file_input is None:
            file_input = page.locator(FILE).first
        try:
            file_input.set_input_files(file_path, timeout=20000)
            print(f"[{username}] ✓ set_input_files succeeded (iframe-aware)")
        except Exception as se:
            print(f"[{username}] set_input EXCEPTION: {se}")
            import traceback
            traceback.print_exc()
            _save_debug_html(page, "SET_INPUT_FAIL", username)
            return False

        # The Post button appears only after the file picker has been attached;
        # wait for it so the rest of the flow is deterministic.
        try:
            page.wait_for_selector('button[data-e2e="post_video_button"]', timeout=20000)
        except Exception:
            _find_post_button(page)

        # === ACCURATE WAIT FOR UPLOAD 100% AND POST BUTTON ENABLED ===
        # The Post button is the source of truth: it only becomes enabled once
        # TikTok has finished uploading AND server-side processing. We also read
        # the visible percentage so we never click while still below 100%.
        print(f"[{username}] === WAITING FOR UPLOAD 100% + Post button ENABLED ===")
        upload_100 = False
        for sec in range(360):  # up to ~12 min
            if sec % 10 == 0:
                pct = _read_upload_percent(page)
                can = _button_can_post(_find_post_button(page))
                print(f"[{username}] upload wait {sec}s — progress={pct}% can_post={can}")
            time.sleep(2)

            # Dismiss any popups that could block the Post button
            _dismiss_blockers(page, username)

            try:
                pct = _read_upload_percent(page)
                post_loc = _find_post_button(page)
                can = _button_can_post(post_loc) if post_loc is not None else False

                if can and (pct is None or pct >= 100):
                    print(f"[{username}] ✓✓ UPLOAD 100% + POST BUTTON ENABLED (progress={pct}%)")
                    upload_100 = True
                    break
                if can and pct is None:
                    print(f"[{username}] ✓ Post button enabled (no progress label present)")
                    upload_100 = True
                    break
            except Exception as we:
                print(f"[{username}] wait loop err: {we}")

            if sec % 20 == 0:
                take_screenshot(username)
                _save_debug_screenshot(page, f"upload_wait_{sec}", username)

        if not upload_100:
            print(f"[{username}] ⚠ 100%/enabled wait timeout — will still attempt post if button present")
            _save_debug_html(page, "UPLOAD_100_TIMEOUT", username)

        take_screenshot(username)
        _save_debug_screenshot(page, "pre_caption", username)

        # Set caption (iframe-aware). Do this after the upload has started.
        print(f"[{username}] Setting caption...")
        try:
            c = None
            for factory in _iter_frame_factories(page):
                try:
                    loc = factory.locator('div[contenteditable="true"]').first
                    if loc.count() > 0 and loc.is_visible():
                        c = loc
                        break
                except Exception:
                    pass
            if c is None:
                c = page.locator(CAP).first
            if c.count() > 0:
                c.click(timeout=6000)
                time.sleep(0.3)
                page.keyboard.press("Control+A")
                time.sleep(0.12)
                page.keyboard.press("Delete")
                time.sleep(0.15)
                c.type(caption, delay=32)
                print(f"[{username}] ✓ caption set")
            else:
                print(f"[{username}] ⚠ no caption field found")
        except Exception as ce:
            print(f"[{username}] caption EXCEPTION (full): {ce}")
            import traceback
            traceback.print_exc()

        # Final confirmation: the Post button MUST be genuinely enabled right
        # before clicking. Never click a still-disabled React button.
        post_loc = _find_post_button(page)
        if post_loc is None or not _button_can_post(post_loc):
            print(f"[{username}] ⚠ Post button not enabled yet — final short wait")
            for sec in range(90):
                time.sleep(2)
                _dismiss_blockers(page, username)
                post_loc = _find_post_button(page)
                if post_loc is not None and _button_can_post(post_loc):
                    print(f"[{username}] ✓ Post button became enabled (final wait {sec}s)")
                    break
                if sec % 15 == 0:
                    take_screenshot(username)

        take_screenshot(username)
        _save_debug_screenshot(page, "pre_post", username)
        _save_debug_html(page, "PRE_POST", username)

        print(f"[{username}] === PRE-POST DEBUG ===")
        print(f"[{username}] URL: {page.url}")
        print(f"[{username}] upload_url_used: {upload_url_used}")
        try:
            print(f"[{username}] frames count: {len(page.frames)}")
            for fi, fr in enumerate(page.frames[:5]):
                try:
                    fu = fr.url if hasattr(fr, 'url') else 'no-url'
                    print(f"  frame{fi}: {fu[:80]}")
                except Exception:
                    pass
        except Exception as fe:
            print(f"frames err: {fe}")

        # Diagnostic dump of the located Post button
        post_loc = _find_post_button(page)
        if post_loc is not None and post_loc.count() > 0:
            try:
                outer = post_loc.first.evaluate("(e)=>e.outerHTML") or ""
                print(f"[{username}] Post button outerHTML:\n{outer[:950]}")
            except Exception as oe:
                print(f"[{username}] outerHTML err: {oe}")
            try:
                bb = post_loc.first.bounding_box(timeout=2500)
                info = post_loc.first.evaluate("""e => ({
                    vis: e.offsetParent !== null,
                    dis: !!e.disabled,
                    aria: e.getAttribute('aria-disabled'),
                    dd: e.getAttribute('data-disabled'),
                    dl: e.getAttribute('data-loading'),
                    txt: (e.textContent||'').trim().slice(0,40)
                })""")
                print(f"[{username}] Post button state: {info}")
                if bb:
                    cx = bb["x"] + bb["width"] / 2
                    cy = bb["y"] + bb["height"] / 2
                    top = page.evaluate(
                        "(x,y)=>{const el=document.elementFromPoint(x,y);"
                        "return el?(el.getAttribute('data-e2e')||el.tagName):null;}", cx, cy)
                    print(f"[{username}] elementFromPoint(center): {top}")
                    if top != "post_video_button":
                        print(f"[{username}] ⚠⚠ OVERLAP — top element is {top}, not the Post button")
            except Exception as oe2:
                print(f"[{username}] post-button debug err: {oe2}")
        else:
            print(f"[{username}] ❌ post button NOT FOUND in DOM")
            _save_debug_html(page, "NO_POST_BUTTON", username)

        print(f"[{username}] === CLICKING POST (real DOM click, overlay-proof) ===")
        posted = False
        for attempt in range(4):
            post_loc = _find_post_button(page)
            if post_loc is None:
                print(f"[{username}] ❌ no Post button (attempt {attempt+1})")
                time.sleep(3)
                continue

            if not _button_can_post(post_loc):
                print(f"[{username}] Post button still disabled (attempt {attempt+1}) — waiting")
                time.sleep(4)
                continue

            clicked, method = _click_post_robust(page, post_loc, username)
            print(f"[{username}] click attempt {attempt+1}: clicked={clicked} method={method}")

            # Newer TikTok shows a 'Continue to post?' confirmation after the click
            _handle_continue_to_post(page, username)

            _save_debug_screenshot(page, f"post_attempt_{attempt+1}", username)
            take_screenshot(username)
            _save_debug_html(page, f"POST_ATTEMPT_{attempt+1}", username)

            # === VERIFY THE POST ACTUALLY REGISTERED ===
            print(f"[{username}] verifying post (attempt {attempt+1})...")
            for _ in range(15):  # up to ~45s
                time.sleep(3)
                _handle_continue_to_post(page, username)
                url = page.url.lower()
                if "/video/" in url or "/content" in url:
                    print(f"[{username}] ✓ URL changed after post: {page.url}")
                    posted = True
                    break
                if _find_post_button(page) is None:
                    print(f"[{username}] ✓ Post button disappeared (published)")
                    posted = True
                    break
                if page.locator(SUCCESS).count() > 0:
                    print(f"[{username}] ✓ success/published toast visible")
                    posted = True
                    break
                try:
                    body = page.inner_text("body", timeout=2000) or ""
                    if any(k in body.lower() for k in
                           ["your video has been uploaded", "video published", "is being uploaded",
                            "uploaded successfully", "posted", "publiser"]):
                        print(f"[{username}] ✓ body text indicates published")
                        posted = True
                        break
                except Exception:
                    pass
                # 'Something went wrong' -> retry the whole click
                try:
                    if "something went wrong" in (page.inner_text("body", timeout=1500) or "").lower():
                        print(f"[{username}] ⚠ 'Something went wrong' — will retry click")
                        break
                except Exception:
                    pass
            if posted:
                break
            print(f"[{username}] attempt {attempt+1} did not confirm a post — retrying")

        try:
            page.remove_listener("request", nr)
            page.remove_listener("response", ns)
        except Exception as re:
            print(f"[{username}] remove_listener err: {re}")

        if posted:
            try:
                handle_content_check_dialog(page, username)
            except Exception as hde:
                print(f"[{username}] content dialog err: {hde}")
            _save_debug_html(page, "FINAL_SUCCESS", username)
            _log_event(username, "TikTok: post published")
            return True
        else:
            print(f"[{username}] ❌ Post never confirmed (see debug HTMLs / screenshots above)")
            _save_debug_html(page, "FINAL_FAIL", username)
            _log_event(username, "TikTok: post NOT confirmed")
            take_screenshot(username)
            return False

    except Exception as fatal:
        print(f"[{username}] FATAL UPLOAD: {fatal}")
        import traceback
        traceback.print_exc()
        _save_debug_html(page, "FATAL", username) if page else None
        _save_debug_screenshot(page, "fatal", username) if page else None
        take_screenshot(username)
        try:
            page.remove_listener("request", nr)
            page.remove_listener("response", ns)
        except: pass
        return False


def _dismiss_youtube_popups(page, username=""):
    """Auto-dismiss the cookie-consent banner and the various YouTube/Studio
    onboarding popups ('Review your channel', 'Got it', 'Skip', 'Not now',
    'Turn on', surveys, etc.) so they never block the upload flow.
    """
    # 1) Cookie consent — YouTube's consent dialog uses these buttons.
    for sel in [
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'button:has-text("Reject all")',
        'tp-yt-paper-button:has-text("Accept all")',
        'ytd-button-renderer:has-text("Accept all")',
    ]:
        try:
            b = page.locator(sel).first
            if b.count() > 0 and b.is_visible():
                b.click(timeout=2000, force=True)
                print(f"[{username}] dismissed YouTube cookie popup: {sel}")
                time.sleep(1)
        except Exception:
            pass

    # 2) Generic dismiss buttons (dialogs, onboarding, "review your channel", etc.)
    #    NOTE: we deliberately do NOT click "Dismiss"/"Close" on the
    #    ytcp-auth-confirmation-dialog ("Verify that it's you") — collapsing it
    #    without resolving verification just makes it re-block every click. That
    #    dialog is handled separately by _handle_youtube_auth_dialog().
    generic = [
        'button:has-text("Skip")',
        'button:has-text("Got it")',
        'button:has-text("Not now")',
        'button:has-text("No thanks")',
        'button:has-text("Maybe later")',
        'ytcp-button:has-text("Skip")',
        'tp-yt-paper-button:has-text("Skip")',
        'ytd-button-renderer:has-text("Got it")',
    ]
    for sel in generic:
        try:
            b = page.locator(sel).first
            if b.count() > 0 and b.is_visible():
                b.click(timeout=1500, force=True)
                print(f"[{username}] dismissed YouTube popup: {sel}")
                time.sleep(0.4)
        except Exception:
            pass

    # 3) Remove any leftover modal/overlay elements via JS (reviews surveys etc.)
    try:
        page.evaluate("""() => {
            document.querySelectorAll(
                'ytd-popup-container, tp-yt-paper-dialog, [role="dialog"], ' +
                '.ytd-consent-bump, ytd-enforcement-message-renderer, ' +
                'yt-mealbar-promo-renderer, ytcp-survey, [class*="survey"]'
            ).forEach(el => {
                if (el.tagName && el.tagName.toLowerCase().includes('ytcp-auth-confirmation-dialog')) return;
                const t = (el.textContent || '').toLowerCase();
                if (t.includes('review') || t.includes('survey') || t.includes('cookie') ||
                    t.includes('consent') || t.includes('got it') || t.includes('skip')) {
                    el.remove();
                }
            });
        }""")
    except Exception:
        pass


def _log_click_targets(page, username, context_label):
    """Dump EVERY clickable control that could be the Next/Publish button, with
    its selector, visible text, bounding box (x, y, width, height) and the exact
    center point we would click. This makes it 100% clear what the bot sees and
    where it will click. Called before every Next/Publish attempt.
    """
    try:
        data = page.evaluate("""() => {
            const out = [];
            const sels = [
                'ytcp-button#next-button', '#next-button',
                'tp-yt-paper-button#next-button', 'ytcp-button#publish-button',
                '#publish-button', 'ytcp-button', 'tp-yt-paper-button',
                'button', '[role="button"]'
            ];
            const seen = new Set();
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    const t = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                    const low = t.toLowerCase();
                    if (!(low === 'next' || low === 'publish' || low === 'continue' ||
                          low === 'verify' || low === 'confirm' || low === 'done' ||
                          el.id === 'next-button' || el.id === 'publish-button')) continue;
                    const r = el.getBoundingClientRect();
                    const key = el.tagName + '|' + el.id + '|' + t + '|' + Math.round(r.x) + ',' + Math.round(r.y);
                    if (seen.has(key)) continue;
                    seen.add(key);
                    const cs = getComputedStyle(el);
                    out.push({
                        tag: el.tagName.toLowerCase(),
                        id: el.id || '',
                        text: t.slice(0, 40),
                        x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height),
                        cx: Math.round(r.x + r.width / 2),
                        cy: Math.round(r.y + r.height / 2),
                        visible: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none',
                        disabled: el.disabled === true,
                        opacity: cs.opacity,
                    });
                }
            }
            return out;
        }""")
        if not data:
            print(f"[{username}] [{context_label}] no Next/Publish candidates found on page")
            return
        print(f"[{username}] [{context_label}] === CLICK TARGETS ({len(data)}) ===")
        for d in data:
            flag = ""
            if d["disabled"]:
                flag = " [DISABLED]"
            elif not d["visible"]:
                flag = " [NOT VISIBLE]"
            print(
                f"[{username}]   <{d['tag']}#{d['id']}> text='{d['text']}' "
                f"box=({d['x']},{d['y']} {d['w']}x{d['h']}) center=({d['cx']},{d['cy']}) "
                f"opacity={d['opacity']}{flag}"
            )
    except Exception as e:
        print(f"[{username}] [{context_label}] target logging err: {e}")


def _click_dialog_button_js(page, container, label):
    """Click a button inside `container` (a CSS selector string) by its visible
    text, via direct DOM .click() (overlay-proof — Playwright's normal click is
    blocked by the dialog's own backdrop/overlay). Returns True if clicked.
    """
    return page.evaluate("""(args) => {
        const [container, label] = args;
        const root = document.querySelector(container);
        if (!root) return false;
        const want = label.toLowerCase();
        // Recurse into shadow roots; prefer the inner <button> that owns the
        // real click handler (the custom <ytcp-button> host ignores .click()).
        const findInner = (r) => {
            const els = [...r.querySelectorAll('ytcp-button, tp-yt-paper-button, button, [role="button"]')];
            let hit = els.find(el => {
                const t = (el.textContent || '').trim().toLowerCase();
                return (t === want || t.startsWith(want + ' ') || t.endsWith(' ' + want)) && !el.disabled;
            });
            if (hit) {
                const inner = hit.shadowRoot && hit.shadowRoot.querySelector('button, tp-yt-paper-button, [role="button"]');
                return inner || hit;
            }
            for (const el of els) { if (el.shadowRoot) { const r2 = findInner(el.shadowRoot); if (r2) return r2; } }
            return null;
        };
        const b = findInner(root);
        if (b && !b.disabled) {
            const r = b.getBoundingClientRect();
            const cx = r.x + r.width/2, cy = r.y + r.height/2;
            b.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, screenX:cx, screenY:cy, isTrusted:true, detail:1}));
            try { b.click(); } catch(e){}
            return true;
        }
        return false;
    }""", [container, label])


def _clear_text_selection(page):
    """Clear any accidental text selection (so the live preview doesn't show the
    whole page highlighted/blue after a click)."""
    try:
        page.evaluate("() => { window.getSelection() && window.getSelection().removeAllRanges(); }")
    except Exception:
        pass


def _debug_dump_yt_buttons(page, username, label):
    """EXTREME DEBUG: dump every visible button-like element on the page (incl.
    shadow DOM) so we can see exactly what the 'Verify that's you' / Next dialog
    looks like — text, disabled state, tag, id, and bounding box."""
    try:
        dump = page.evaluate("""() => {
            const out = [];
            const walk = (root, depth) => {
                const els = [...root.querySelectorAll('*')];
                for (const el of els) {
                    const tag = el.tagName ? el.tagName.toLowerCase() : '?';
                    const txt = (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 40);
                    const isBtn = /button|ytcp-button|tp-yt-paper-button|paper-button/.test(tag)
                        || el.getAttribute && el.getAttribute('role') === 'button'
                        || el.hasAttribute && (el.hasAttribute('role'));
                    const clickableTxt = /next|continue|verify|confirm|done|submit|publish|post|skip|dismiss|cancel/i.test(txt);
                    if ((isBtn || clickableTxt) && txt) {
                        let disabled = false;
                        try { disabled = el.disabled; } catch(e){}
                        let rect = null;
                        try { const r = el.getBoundingClientRect(); rect = {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}; } catch(e){}
                        out.push({tag, id: el.id||'', cls: (el.className||'').toString().slice(0,40), txt, disabled, rect, depth});
                    }
                    if (el.shadowRoot) walk(el.shadowRoot, depth+1);
                }
            };
            try { walk(document, 0); } catch(e) { out.push({err: String(e)}); }
            return out.slice(0, 60);
        }""")
        print(f"[{username}] === DEBUG BUTTONS [{label}] (url={page.url}) ===", flush=True)
        for d in dump:
            print(f"[{username}]   - tag={d.get('tag')} id='{d.get('id')}' cls='{d.get('cls')}' "
                  f"txt='{d.get('txt')}' disabled={d.get('disabled')} rect={d.get('rect')}", flush=True)
        # Also save full HTML for offline inspection.
        try:
            os.makedirs("debug_htmls", exist_ok=True)
            ts = int(time.time())
            p = f"debug_htmls/{username}_{label}_{ts}.html"
            with open(p, "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"[{username}] DEBUG HTML -> {p}", flush=True)
        except Exception as he:
            print(f"[{username}] debug html err: {he}", flush=True)
    except Exception as e:
        print(f"[{username}] _debug_dump_yt_buttons err: {e}", flush=True)


def _verify_dialog_present_js():
    """JS that returns truthy info ONLY if YouTube's real 'Verify that it's you'
    dialog is actually open.

    Critical: we must NOT match a generic 'Continue'/'Next' button that happens
    to exist elsewhere on the Studio page (the left-nav, banners, upload wizard).
    The old loose fallback matched any 'Continue' text and made the bot think the
    auth dialog was up forever — it then spun clicking nothing while the real
    upload wizard sat ready. So detection now requires EITHER the dedicated
    ytcp-auth-confirmation-dialog element to be visible, OR visible page text that
    actually mentions verification ('verify that it's you', 'unusual activity',
    'not a robot', etc.).
    """
    return """() => {
        // 1) The dedicated auth dialog element, if present and visible.
        const dlg = document.querySelector('ytcp-auth-confirmation-dialog, ytcp-auth-confirmation-dialog');
        if (dlg && dlg.offsetParent !== null) {
            const btns = [...dlg.querySelectorAll('button, ytcp-button, tp-yt-paper-button, [role="button"]')];
            const next = btns.find(b => /next|continue|verify|confirm|ok|got it|done|submit/i.test((b.textContent||'')) && !b.disabled);
            const disabledNext = btns.find(b => /next|continue|verify|confirm|ok|got it|done|submit/i.test((b.textContent||'')) && b.disabled);
            return {present:true, hasEnabledNext: !!next, hasDisabledNext: !!disabledNext,
                    text: (dlg.textContent||'').replace(/\\s+/g,' ').slice(0,80)};
        }
        // 2) No dedicated element — only treat as a verify dialog if visible page
        //    text actually mentions verification. A bare 'Continue'/'Next' button
        //    on the rest of the page is NOT sufficient.
        const bodyText = (document.body && document.body.innerText || '').toLowerCase();
        const verifyPhrases = ['verify that', 'verify it', 'confirm it', 'confirm your',
                                "it's you", 'not a robot', 'unusual activity',
                                'suspicious', 'verify your identity', 'prove you'];
        if (verifyPhrases.some(p => bodyText.includes(p))) {
            // Make sure the visible text is inside an actual dialog/overlay, not
            // a random help article in the page.
            const dialogs = [...document.querySelectorAll('[role="dialog"], ytcp-auth-confirmation-dialog, tp-yt-paper-dialog, ytcp-dialog')];
            const inDialog = dialogs.some(d => d.offsetParent !== null && verifyPhrases.some(p => (d.innerText||'').toLowerCase().includes(p)));
            if (inDialog) {
                return {present:true, hasEnabledNext:false, hasDisabledNext:false,
                        text: bodyText.match(new RegExp(verifyPhrases.join('|'),'i'))[0]};
            }
        }
        return {present:false};
    }"""


def _handle_youtube_auth_dialog(page, username=""):
    """Handle YouTube Studio's 'Verify that it's you' (ytcp-auth-confirmation-dialog).

    This dialog intercepts ALL clicks on the upload form, so it MUST be resolved
    first. Its primary action button is #next-button (text 'Next'); we click it
    via a DIRECT DOM click (JS) because Playwright's actionability check is blocked
    by the dialog's own overlay backdrop.

    IMPORTANT: we NEVER click a DISABLED button. When the dialog has advanced to a
    step that waits for a verification code/phone (the Next button is disabled), we
    STOP and let the user finish it in the live cam — we do NOT hammer it (which
    previously dragged a text selection across the page).

    Returns:
      False  -> no dialog present
      "advanced" -> we clicked the enabled Next and the dialog moved on
      "needs_code" -> dialog still open but Next is disabled (waiting for user input)
    """
    try:
        # Detect the dialog BROADLY (the old strict '#confirm-button text===next'
        # check missed it and the upload hung forever).
        info = page.evaluate(_verify_dialog_present_js())
        dlg_present = bool(info and info.get("present"))
        if not dlg_present:
            return False
        print(f"[{username}] ⚠ YouTube 'Verify that it's you' dialog detected — {info.get('text')}", flush=True)
        _debug_dump_yt_buttons(page, username, "VERIFY_DIALOG")

        clicked = False
        disabled = False

        # First try a shadow-DOM-aware search for the actual visible Next control.
        # YouTube sometimes nests the button two or three shadow roots deep; a
        # normal '#confirm-button button' locator can then see the host but not the
        # real clickable control. Do not label this as a code step until Next has
        # actually been pressed and the dialog changes.
        try:
            first = page.evaluate("""() => {
                const visible = (el) => {
                    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
                };
                const disabled = (el) => el.disabled === true ||
                    String(el.getAttribute && el.getAttribute('aria-disabled') || '').toLowerCase() === 'true' ||
                    el.hasAttribute && el.hasAttribute('disabled');
                const walk = (root) => {
                    for (const el of root.querySelectorAll('*')) {
                        const text = (el.textContent || '').trim().replace(/\s+/g, ' ').toLowerCase();
                        if ((text === 'next' || text === 'continue') && visible(el)) {
                            const inner = el.shadowRoot && [...el.shadowRoot.querySelectorAll('button,[role="button"],tp-yt-paper-button')]
                                .find(x => visible(x));
                            const target = inner || el;
                            if (!disabled(target) && !disabled(el)) {
                                target.click();
                                return {clicked:true, text};
                            }
                            // This element may only be a disabled-looking wrapper;
                            // keep walking its descendants/shadow root for the real
                            // interactive control instead of aborting the search.
                            continue;
                        }
                        if (el.shadowRoot) {
                            const hit = walk(el.shadowRoot);
                            if (hit) return hit;
                        }
                    }
                    return null;
                };
                const dlg = document.querySelector('ytcp-auth-confirmation-dialog');
                return dlg ? (walk(dlg.shadowRoot || dlg) || {clicked:false}) : {clicked:false};
            }""")
            if first and first.get('disabled'):
                # The custom-element wrapper can report disabled while its inner
                # shadow button is enabled. Keep going and inspect the real target.
                print(f"[{username}] verify wrapper reported disabled; checking inner control", flush=True)
            if first and first.get('clicked'):
                clicked = True
                print(f"[{username}] ✓ clicked verify-dialog Next (shadow DOM)", flush=True)
        except Exception as e:
            print(f"[{username}] shadow-DOM verify click failed: {str(e)[:100]}", flush=True)

        # Resolve the dialog's primary Next/Continue button. The real clickable
        # target is the INNER <button> inside the enabled #next-button shadow DOM. We click
        # that inner <button> directly with Playwright (force=True bypasses the
        # dialog backdrop hit-test). This is what actually fires Polymer's handler —
        # clicking the wrapper (or page.mouse at its center) misses the inner
        # control and lands on the backdrop, which drags a text selection across
        # the whole page instead of advancing.
        inner_btn = page.locator(
            'ytcp-auth-confirmation-dialog #next-button button, '
            '#next-button button, ytcp-auth-confirmation-dialog #confirm-button button'
        ).first
        if not clicked and inner_btn.count() > 0:
            if inner_btn.is_disabled():
                disabled = True
            else:
                try:
                    inner_btn.click(timeout=5000, force=True, no_wait_after=True)
                    clicked = True
                except Exception as e:
                    print(f"[{username}] verify inner-button click failed: {str(e)[:60]}")
        if not clicked:
            # Fallback: trusted JS click on #next-button's inner <button>.
            try:
                res = page.evaluate("""() => {
                    const b = document.querySelector('ytcp-auth-confirmation-dialog #next-button, #next-button, ytcp-auth-confirmation-dialog #confirm-button, #confirm-button');
                    if (!b) return {ok:false, reason:'no-confirm'};
                    const roots = [];
                    const collect = root => {
                        if (!root || roots.includes(root)) return;
                        roots.push(root);
                        for (const el of root.querySelectorAll('*')) if (el.shadowRoot) collect(el.shadowRoot);
                    };
                    collect(b);
                    let target = null;
                    const visible = el => { const r=el.getBoundingClientRect(), c=getComputedStyle(el); return r.width>0 && r.height>0 && c.display!=='none' && c.visibility!=='hidden'; };
                    for (const root of roots) {
                        const candidates = [...root.querySelectorAll('button,[role="button"],tp-yt-paper-button,ytcp-button')];
                        target = candidates.find(x => visible(x) && /^(next|continue|verify|submit|confirm)$/i.test((x.textContent||'').trim()) && !x.disabled && String(x.getAttribute('aria-disabled')||'').toLowerCase()!=='true')
                              || candidates.find(x => visible(x) && !x.disabled && String(x.getAttribute('aria-disabled')||'').toLowerCase()!=='true');
                        if (target) break;
                    }
                    target = target || b;
                    if (target.disabled || String(target.getAttribute && target.getAttribute('aria-disabled')||'').toLowerCase()==='true') return {ok:false, disabled:true};
                    const r = target.getBoundingClientRect();
                    const cx = r.x + r.width/2, cy = r.y + r.height/2;
                    target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, screenX:cx, screenY:cy, detail:1}));
                    try { target.click(); } catch(e){}
                    return {ok:true, tag:target.tagName, id:b.id||'', cx:Math.round(cx), cy:Math.round(cy)};
                }""")
                if res and res.get("disabled"):
                    disabled = True
                elif res and res.get("ok"):
                    clicked = True
                    print(f"[{username}] verify Next clicked (JS) <#{res.get('id')}> at ({res.get('cx')},{res.get('cy')})")
            except Exception as e:
                print(f"[{username}] verify JS fallback err: {e}")
        _clear_text_selection(page)

        if disabled:
            print(f"[{username}] ⚠ verify Next is DISABLED — waiting for code/method (user action needed)", flush=True)
            _debug_dump_yt_buttons(page, username, "VERIFY_DISABLED")
            update_account(username, current_task="Verify that it's you — enter the code in live cam")
            return "needs_code"
        if not clicked:
            print(f"[{username}] ⚠ verify Next not clickable", flush=True)
            _debug_dump_yt_buttons(page, username, "VERIFY_NO_CLICK")
            update_account(username, current_task="Verify that it's you — click Next in live cam")
            return "needs_code"

        print(f"[{username}] ✓ clicked verify-dialog Next")
        _log_event(username, "Verify dialog: clicked Next")
        update_account(username, current_task="Verify that it's you — clicked Next, waiting...")
        time.sleep(3)
        take_screenshot(username)

        # Do not treat a transient overlay repaint as success. YouTube can briefly
        # hide the custom element while rebuilding the same verification dialog.
        # Require it to remain absent across several checks before resuming upload.
        gone_since = None
        for _ in range(8):
            present_now = page.evaluate(_verify_dialog_present_js())
            if present_now and present_now.get("present"):
                gone_since = None
                break
            if gone_since is None:
                gone_since = time.time()
            if time.time() - gone_since >= 2:
                break
            time.sleep(0.5)

        # Poll up to ~5 min. If a 6-digit code is supplied via the dashboard,
        # type it into the dialog and submit; otherwise wait for the user.
        deadline = time.time() + 300
        absent_checks = 0
        while time.time() < deadline:
            take_screenshot(username)
            # The dialog may change its button from Next to Verify/Submit while
            # waiting for the user's phone, passkey, or 6-digit code.  Looking only
            # at the button label made the bot declare success too early and click
            # the upload wizard behind the still-open overlay.
            still = page.evaluate("""() => {
                const d = document.querySelector('ytcp-auth-confirmation-dialog');
                if (!d) return false;
                const r = d.getBoundingClientRect();
                const cs = getComputedStyle(d);
                return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
            }""")
            if not still:
                absent_checks += 1
                if absent_checks < 3:
                    time.sleep(0.7)
                    continue
                print(f"[{username}] ✓ verify dialog dismissed")
                _log_event(username, "Verify dialog: dismissed")
                update_account(username, current_task="Verification complete — resuming upload")
                return "advanced"
            absent_checks = 0
            code = get_verify_code(username)
            if code:
                try:
                    filled = page.evaluate("""(c) => {
                        const d = document.querySelector('ytcp-auth-confirmation-dialog') || document;
                        const inp = d.querySelector('input[type="text"], input[type="tel"], input[type="number"], input:not([type]), textarea');
                        if (!inp) return false;
                        const set = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        if (set) { set.call(inp, c); inp.dispatchEvent(new Event('input', {bubbles:true})); }
                        else { inp.value = c; inp.dispatchEvent(new Event('input', {bubbles:true})); }
                        return true;
                    }""", code)
                    if filled:
                        _log_event(username, f"Verify dialog: typed code {code}")
                        clear_verify_code(username)
                        try:
                            page.evaluate("""() => {
                                const b = document.querySelector('ytcp-auth-confirmation-dialog #next-button, #next-button, ytcp-auth-confirmation-dialog #confirm-button, #confirm-button');
                                if (!b || b.disabled) return;
                                const inner = b.shadowRoot && b.shadowRoot.querySelector('button, tp-yt-paper-button, [role="button"]');
                                const target = inner || b;
                                const r = target.getBoundingClientRect();
                                const cx = r.x + r.width/2, cy = r.y + r.height/2;
                                target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, isTrusted:true}));
                                try { target.click(); } catch(e){}
                            }""")
                        except Exception:
                            pass
                except Exception as ce:
                    _log_event(username, f"Verify code entry err: {ce}")
            update_account(username, current_task="Verify that it's you — waiting for your code/method in live cam")
            time.sleep(5)
        _log_event(username, "Verify dialog: timed out waiting for code — still blocked")
        return "needs_code"
    except Exception as e:
        print(f"[{username}] verify dialog handling err: {e}")
        return False


def _click_youtube_next(page):
    """Click YouTube Studio's Next button — but NEVER a disabled one, and only
    ONCE. If the 'Verify that it's you' dialog is open, we hand off to
    _handle_youtube_auth_dialog (which skips disabled buttons). Returns True if a
    real (enabled) Next was clicked.
    """
    # 0) If the REAL verify dialog is up, let the dedicated handler deal with it.
    #    IMPORTANT: only match the dedicated ytcp-auth-confirmation-dialog
    #    container — NOT a bare '#confirm-button', because other dialogs on the
    #    Studio page can also have a #confirm-button and we must not hijack the
    #    upload wizard's own Next.
    try:
        if page.evaluate("""() => {
            const dlg = document.querySelector('ytcp-auth-confirmation-dialog');
            if (!dlg) return false;
            const r = dlg.getBoundingClientRect();
            const cs = getComputedStyle(dlg);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        }"""):
            res = _handle_youtube_auth_dialog(page, "")
            # "advanced" => dialog gone, let the upload Next run on the next call.
            # "needs_code"/False => dialog still up; do not click the upload Next.
            return res == "advanced"
    except Exception:
        pass

    # Robust JS click: scope to the upload dialog, recurse shadow DOM, and
    # detect "disabled" correctly on custom ytcp-button hosts (which don't
    # always expose a .disabled property on the outer element — check the
    # inner paper-button / aria-disabled too). This is the reliable path.
    try:
        res = page.evaluate("""() => {
            const isDisabled = (el) => {
                if (!el) return true;
                if (el.disabled) return true;
                if ((el.getAttribute && (el.getAttribute('aria-disabled')||'').toLowerCase()) === 'true') return true;
                if ((el.getAttribute && (el.getAttribute('disabled')||'')) !== null) return true;
                const cs = el.classList;
                if (cs && /disabled/.test(cs.toString())) return true;
                // inner control
                const inner = el.shadowRoot && el.shadowRoot.querySelector('button, paper-button, [role="button"]');
                return inner ? isDisabled(inner) : false;
            };
            const clickEl = (el) => {
                const r = el.getBoundingClientRect();
                const cx = r.x + r.width/2, cy = r.y + r.height/2;
                el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, screenX:cx, screenY:cy, isTrusted:true, detail:1}));
                try { el.click(); } catch(e){}
                return {tag: el.tagName.toLowerCase(), id: el.id||'', cx: Math.round(cx), cy: Math.round(cy)};
            };
            const findInner = (r) => {
                const els = [...r.querySelectorAll('ytcp-button, tp-yt-paper-button, button, [role="button"]')];
                let hit = els.find(el => (el.textContent || '').trim().toLowerCase() === 'next'
                    && !isDisabled(el) && el.offsetParent !== null);
                if (hit) {
                    const inner = hit.shadowRoot && hit.shadowRoot.querySelector('button, tp-yt-paper-button, [role="button"]');
                    return inner || hit;
                }
                for (const el of els) { if (el.shadowRoot) { const r2 = findInner(el.shadowRoot); if (r2) return r2; } }
                return null;
            };
            // Prefer the upload dialog's own Next button.
            const root = document.querySelector('ytcp-upload-dialog, ytcp-video-metadata-editor, ytcp-upload-renderer') || document;
            const next = findInner(root);
            if (next) {
                const info = clickEl(next);
                return {clicked: true, tag: info.tag, id: info.id, cx: info.cx, cy: info.cy};
            }
            return {clicked: false};
        }""")
        _clear_text_selection(page)
        if res and res.get("clicked"):
            print(f"[NEXT] ✓ clicked via scoped JS <{res['tag']}#{res['id']}> at ({res['cx']},{res['cy']})")
            return True
        print(f"[NEXT] no enabled 'Next' element found in upload dialog")
    except Exception as e:
        print(f"[NEXT] JS click err: {e}")

    # Last resort: Playwright locators with force=True.
    selectors = [
        'ytcp-upload-dialog ytcp-button#next-button',
        'ytcp-button#next-button',
        '#next-button',
        'tp-yt-paper-button#next-button',
        'ytcp-button:has-text("Next")',
        'button:has-text("Next")',
    ]
    for sel in selectors:
        try:
            b = page.locator(sel).first
            if b.count() > 0 and b.is_visible():
                if b.is_disabled():
                    print(f"[NEXT] skipping DISABLED Next '{sel}'")
                    continue
                box = b.bounding_box()
                cx = cy = None
                if box:
                    cx = round(box["x"] + box["width"] / 2)
                    cy = round(box["y"] + box["height"] / 2)
                print(f"[NEXT] clicking selector='{sel}' box={box} center=({cx},{cy})")
                b.click(timeout=4000, force=True, no_wait_after=True)
                _clear_text_selection(page)
                print(f"[NEXT] ✓ clicked via Playwright selector '{sel}' at ({cx},{cy})")
                return True
        except Exception as e:
            print(f"[NEXT] selector '{sel}' failed: {e}")
            continue
    print(f"[NEXT] ⚠ no Next button clickable anywhere")
    try:
        _save_debug_html(page, "YT_NEXT_NOT_FOUND", username or "")
    except Exception:
        pass
    return False


def upload_video_to_youtube(username, file_path, caption, title):
    """Upload a prepared (1080x1920) Short to YouTube Studio with an accurate,
    full caption. Selects 'No, it's not made for kids' + public visibility so it
    publishes as a Short. Returns True on success.
    """
    page = _get_page(username)
    if page is None:
        print(f"[{username}] No page available for YouTube upload")
        return False

    SUCCESS = 'text=/uploaded|published|video is live|your video has been|processing|done/i'

    try:
        _dismiss_youtube_popups(page, username)
        print(f"[{username}] === YOUTUBE UPLOAD FLOW ===", flush=True)
        _debug_dump_yt_buttons(page, username, "UPLOAD_START")
        update_account(username, current_task="Opening YouTube Studio upload...")

        upload_url_used = None
        for u in ["https://studio.youtube.com/channel/upload",
                  "https://studio.youtube.com",
                  "https://www.youtube.com/upload"]:
            try:
                print(f"[{username}] Trying: {u}")
                page.goto(u, timeout=45000, wait_until="domcontentloaded")
                time.sleep(random.uniform(3.0, 5.5))
                take_screenshot(username)
                if _find_file_input(page) is not None:
                    print(f"[{username}] ✓ file input found on {u}")
                    upload_url_used = u
                    break
                # Studio dashboard may show the upload button instead
                try:
                    page.locator('ytcp-uploads-dialog, ytcp-button#upload-button, button:has-text("CREATE")').first.click(timeout=6000, force=True)
                    time.sleep(3)
                    if _find_file_input(page) is not None:
                        upload_url_used = u
                        break
                except Exception:
                    pass
            except Exception as g:
                print(f"[{username}] goto err: {str(g)[:60]}")

        if not upload_url_used:
            print(f"[{username}] ❌ No file input found on any YouTube upload URL")
            _save_debug_html(page, "YT_NO_FILE_INPUT", username)
            take_screenshot(username)
            return False

        print(f"[{username}] Using YouTube upload page: {upload_url_used}")
        _dismiss_youtube_popups(page, username)
        file_input = _find_file_input(page)
        if file_input is None:
            file_input = page.locator('input[type="file"]').first
        try:
            file_input.set_input_files(file_path, timeout=30000)
            print(f"[{username}] ✓ set_input_files succeeded (YouTube)")
        except Exception as se:
            print(f"[{username}] YouTube set_input EXCEPTION: {se}")
            _save_debug_html(page, "YT_SET_INPUT_FAIL", username)
            return False

        # Wait for processing + the details form to appear
        print(f"[{username}] Waiting for YouTube processing + details form...")
        details_ready = False
        verify_blocked = False
        for sec in range(240):  # up to 8 min
            time.sleep(3)
            # The "Verify that it's you" dialog can appear during processing and
            # BLOCKS the details form from ever rendering. Surface it loudly
            # instead of spinning silently for 8 minutes.
            _handle_youtube_auth_dialog(page, username)
            # Did the REAL verify dialog actually block us? Only match the
            # dedicated ytcp-auth-confirmation-dialog container — a bare
            # '#confirm-button' elsewhere on the page (e.g. the upload wizard)
            # must NOT be treated as the auth dialog.
            try:
                still_verify = page.evaluate("""() => {
                    const dlg = document.querySelector('ytcp-auth-confirmation-dialog');
                    if (!dlg) return false;
                    const r = dlg.getBoundingClientRect();
                    const cs = getComputedStyle(dlg);
                    return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
                }""")
                if still_verify:
                    verify_blocked = True
                    print(f"[{username}] ⚠ VERIFY DIALOG STILL BLOCKING — details form cannot appear. Resolve it in the live cam.")
            except Exception:
                pass
            # Report upload progress so we know if the file is actually uploading.
            pct = _read_upload_percent(page)
            if pct is not None and sec % 5 == 0:
                print(f"[{username}] YouTube upload progress: {pct}%")
            # Title input indicates the details screen is ready. YouTube's Studio
            # upload wizard uses several possible structures for the title field
            # across redesigns, so check all of them + the upload dialog container
            # (which only exists once the wizard actually opened).
            title_loc = page.locator(
                '#title-textarea, #textbox[label*="Title"], ytcp-mention-textbox[label*="Title"], '
                'input[placeholder*="Title"], tp-yt-paper-input[label*="Title"], '
                'div#title-container textarea, #title-container input, '
                'ytcp-upload-dialog #title, ytcp-video-metadata-editor'
            ).first
            if title_loc.count() > 0 and title_loc.is_visible():
                details_ready = True
                print(f"[{username}] ✓ YouTube details form detected (after ~{sec*3}s)")
                break
            if sec % 15 == 0:
                take_screenshot(username)

        if verify_blocked and not details_ready:
            print(f"[{username}] ❌ YouTube upload blocked by 'Verify that it's you' dialog. "
                  f"Manually complete verification in the live cam, then re-run. Skipping upload.")
            _save_debug_html(page, "YT_VERIFY_BLOCKED", username)
            take_screenshot(username)
            return False

        if not details_ready:
            print(f"[{username}] ⚠ YouTube details form not detected, attempting anyway")

        take_screenshot(username)

        # Title — keep it short & accurate, with #Shorts for discovery.
        try:
            if title:
                t = (title[:58] + " #Shorts") if len(title) <= 58 else (title[:58] + "…")
            else:
                t = "Daily Short #Shorts"
            tl = page.locator('#title-textarea, #textbox[label*="Title"], ytcp-mention-textbox[label*="Title"], input[placeholder*="Title"], tp-yt-paper-input[label*="Title"], div#title-container textarea, #title-container input, ytcp-upload-dialog #title').first
            if tl.count() > 0:
                tl.click(timeout=4000, force=True)
                time.sleep(0.3)
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                tl.type(t, delay=25)
                print(f"[{username}] ✓ YouTube title set: {t}")
            else:
                # JS fallback: set the title via Polymer/React value setter on the
                # first visible text input/fields inside the upload editor.
                set_ok = page.evaluate("""(val) => {
                    const root = document.querySelector('ytcp-upload-dialog, ytcp-video-metadata-editor') || document;
                    const inp = [...root.querySelectorAll('textarea, input[type="text"], [contenteditable]')]
                        .find(e => (e.placeholder||'').toLowerCase().includes('title') || (e.getAttribute('label')||'').toLowerCase().includes('title') || (e.id||'').toLowerCase().includes('title'));
                    if (!inp) return false;
                    inp.focus();
                    if (inp.tagName === 'TEXTAREA' || inp.tagName === 'INPUT') {
                        const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
                                   || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                        if (setter) { setter.call(inp, val); inp.dispatchEvent(new Event('input', {bubbles:true})); }
                        else { inp.value = val; inp.dispatchEvent(new Event('input', {bubbles:true})); }
                    } else {
                        inp.innerText = val; inp.dispatchEvent(new Event('input', {bubbles:true}));
                    }
                    return true;
                }""", t)
                print(f"[{username}] {'✓' if set_ok else '⚠'} YouTube title set via JS fallback: {t}") if set_ok else print(f"[{username}] YouTube title NOT found")
        except Exception as ce:
            print(f"[{username}] YouTube title EXCEPTION: {ce}")

        # Description — paste the full accurate caption.
        try:
            dl = page.locator('#description-textarea, #textbox[label*="Description"], ytcp-mention-textbox[label*="Description"], textarea[placeholder*="Description"], div#description-container textarea, #description-container textarea').first
            if dl.count() > 0:
                dl.click(timeout=4000, force=True)
                time.sleep(0.3)
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                dl.type(caption, delay=12)
                print(f"[{username}] ✓ YouTube description set")
            else:
                set_ok = page.evaluate("""(val) => {
                    const root = document.querySelector('ytcp-upload-dialog, ytcp-video-metadata-editor') || document;
                    const inp = [...root.querySelectorAll('textarea, input[type="text"]')]
                        .find(e => (e.placeholder||'').toLowerCase().includes('description') || (e.getAttribute('label')||'').toLowerCase().includes('description') || (e.id||'').toLowerCase().includes('description'));
                    if (!inp) return false;
                    inp.focus();
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set
                               || Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    if (setter) { setter.call(inp, val); inp.dispatchEvent(new Event('input', {bubbles:true})); }
                    else { inp.value = val; inp.dispatchEvent(new Event('input', {bubbles:true})); }
                    return true;
                }""", caption)
                print(f"[{username}] {'✓' if set_ok else '⚠'} YouTube description set via JS fallback") if set_ok else print(f"[{username}] YouTube description NOT found")
        except Exception as ce:
            print(f"[{username}] YouTube description EXCEPTION: {ce}")

        # Resolve the "Verify that it's you" dialog if it appeared (it blocks clicks).
        _handle_youtube_auth_dialog(page, username)
        _dismiss_youtube_popups(page, username)
        take_screenshot(username)

        # Click "Show more" to reveal Made for kids, then set Not made for kids.
        _handle_youtube_auth_dialog(page, username)
        try:
            more = page.locator('button:has-text("Show more")').first
            if more.count() > 0 and more.is_visible():
                more.click(timeout=4000)
                time.sleep(1)
        except Exception:
            pass
        try:
            # "Made for kids" — YouTube's current Studio uses a
            # ytkc-made-for-kids-select with tp-yt-paper-radio-button elements
            # (name="NOT_MADE_FOR_KIDS" / "MADE_FOR_KIDS"). Click via JS so the
            # shadow-DOM inner control actually fires (a Playwright click on the
            # wrapper often misses). Match broadly on the "No" option.
            page.evaluate("""() => {
                const opts = [...document.querySelectorAll(
                    'tp-yt-paper-radio-button, ytkc-made-for-kids-select tp-yt-paper-radio-button, [name="NOT_MADE_FOR_KIDS"]'
                )];
                const no = opts.find(el => {
                    const t = (el.textContent||'').trim().toLowerCase();
                    const name = (el.getAttribute('name')||'') ;
                    return /no/.test(t) || name === 'NOT_MADE_FOR_KIDS';
                });
                if (no && !no.disabled) {
                    const inner = no.shadowRoot && no.shadowRoot.querySelector('button, paper-button, [role="button"]');
                    const target = inner || no;
                    target.click();
                }
            }""")
            time.sleep(0.6)
        except Exception:
            pass

        # Next -> Next -> Next (Details -> Video elements -> Checks -> Public).
        # We VERIFY each click actually advanced the form. Studio keeps every
        # wizard step in the DOM at once (visibility-toggled) and the upload
        # progress text changes constantly, so a raw text signature is useless.
        # Instead we track the ACTIVE step: the visible title of the current
        # step panel (ytcp-video-metadata-editor / ytcp-upload-renderer) plus
        # whether the Publish button exists yet. That changes only when the
        # wizard actually moves forward.
        def _page_signature():
            try:
                return page.evaluate("""() => {
                    const root = document.querySelector('ytcp-upload-dialog, ytcp-video-metadata-editor, ytcp-upload-renderer') || document;
                    // The step indicator: which step chip is active (Details/Checks/Visibility).
                    const steps = [...root.querySelectorAll('[class*="step"], [role="tab"], tp-yt-paper-tab, [class*="stepChip"]')]
                        .map(e => (e.className||'') + ':' + (!!(e.offsetParent) && /active|selected/.test(e.className||''))).join('|');
                    // Active panel heading text (the visible step's label).
                    const panels = [...root.querySelectorAll('ytcp-video-metadata-editor, [class*="metadata"], [class*="upload"]')]
                        .filter(e => e.offsetParent !== null)
                        .map(e => (e.textContent||'').replace(/\\s+/g,' ').slice(0,40)).join('|');
                    const hasPublish = !!document.querySelector('ytcp-button#publish-button, #publish-button, ytcp-button:has-text("Publish")');
                    return steps + '###' + panels + '###PUB:' + hasPublish;
                }""")
            except Exception:
                return str(time.time())

        for step in range(6):
            _handle_youtube_auth_dialog(page, username)

            # Before each Next: reveal "Made for kids" and set "No" (required to proceed).
            try:
                more = page.locator('button:has-text("Show more")').first
                if more.count() > 0 and more.is_visible():
                    more.click(timeout=3000, force=True)
                    time.sleep(0.8)
            except Exception:
                pass
            try:
                page.evaluate("""() => {
                    const opts = [...document.querySelectorAll(
                        'tp-yt-paper-radio-button, ytkc-made-for-kids-select tp-yt-paper-radio-button, [name="NOT_MADE_FOR_KIDS"]'
                    )];
                    const no = opts.find(el => {
                        const t = (el.textContent||'').trim().toLowerCase();
                        const name = (el.getAttribute('name')||'') ;
                        return /no/.test(t) || name === 'NOT_MADE_FOR_KIDS';
                    });
                    if (no && !no.disabled) {
                        const inner = no.shadowRoot && no.shadowRoot.querySelector('button, paper-button, [role="button"]');
                        const target = inner || no;
                        target.click();
                    }
                }""")
                time.sleep(0.4)
            except Exception:
                pass

            before = _page_signature()
            _log_click_targets(page, username, f"NEXT_STEP_{step+1}")
            _debug_dump_yt_buttons(page, username, f"NEXT_STEP_{step+1}_PRE")
            clicked = _click_youtube_next(page)
            if not clicked:
                # Try the dialog's confirm Next as a fallback.
                try:
                    if _click_dialog_button_js(page, 'ytcp-upload-renderer, ytcp-video-metadata-editor, [role="dialog"]', 'Next'):
                        clicked = True
                        print(f"[{username}] ✓ clicked dialog Next (fallback)")
                except Exception:
                    pass
            if not clicked:
                print(f"[{username}] ⚠ no Next button found at step {step+1}")
                time.sleep(3)
                continue

            print(f"[{username}] → clicked Next (step {step+1}), waiting for form to advance...")
            time.sleep(random.uniform(2.0, 3.5))
            after = _page_signature()
            if after == before:
                print(f"[{username}] ⚠ Next did NOT advance the form (step {step+1}) — retrying with dialog Next")
                try:
                    _click_dialog_button_js(page, 'ytcp-upload-renderer, ytcp-video-metadata-editor, [role="dialog"]', 'Next')
                except Exception:
                    pass
                time.sleep(2)
                after = _page_signature()
                if after == before:
                    print(f"[{username}] ✗ Next STILL did not advance (step {step+1}). "
                          f"The form is stuck — stopping the Next sequence to avoid a bad publish.")
                    _save_debug_html(page, "YT_NEXT_STUCK", username)
                    take_screenshot(username)
                    break
            else:
                print(f"[{username}] ✓ form advanced after Next (step {step+1})")
            take_screenshot(username)
        print(f"[{username}] ✓ ALL Next steps done — moving to visibility/Publish", flush=True)

        # Set visibility to Public
        _handle_youtube_auth_dialog(page, username)
        try:
            public = page.locator('tp-yt-paper-radio-button:has-text("Public"), paper-radio-button:has-text("Public")').first
            if public.count() > 0:
                public.click(timeout=4000)
                time.sleep(0.5)
        except Exception:
            pass

        take_screenshot(username)
        _save_debug_screenshot(page, "yt_pre_publish", username)
        _save_debug_html(page, "YT_PRE_PUBLISH", username)

        # Click Publish
        published = False
        for attempt in range(4):
            _handle_youtube_auth_dialog(page, username)
            _log_click_targets(page, username, f"PUBLISH_ATTEMPT_{attempt+1}")
            try:
                pb = page.locator('ytcp-button#publish-button, #publish-button, button:has-text("Publish"), ytcp-button:has-text("Publish")').first
                if pb.count() == 0 or not pb.is_visible():
                    # JS fallback for an icon-only Publish button — report coords.
                    clicked = page.evaluate("""() => {
                        const els = [...document.querySelectorAll('ytcp-button, tp-yt-paper-button, button')];
                        const p = els.find(el => (el.textContent || '').trim().toLowerCase() === 'publish'
                            && !el.disabled && el.offsetParent !== null);
                        if (p) {
                            const r = p.getBoundingClientRect();
                            const cx = r.x + r.width/2, cy = r.y + r.height/2;
                            p.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window, clientX:cx, clientY:cy, screenX:cx, screenY:cy, isTrusted:true, detail:1}));
                            try { p.click(); } catch(e){}
                            return {ok:true, tag:p.tagName.toLowerCase(), id:p.id||'',
                                    text:(p.textContent||'').trim().slice(0,40),
                                    cx:Math.round(cx), cy:Math.round(cy)};
                        }
                        return {ok:false};
                    }""")
                    if not clicked or not clicked.get("ok"):
                        print(f"[{username}] YouTube publish button not found (attempt {attempt+1})")
                        time.sleep(4)
                        continue
                    print(f"[PUBLISH] ✓ clicked via JS <{clicked['tag']}#{clicked['id']}> text='{clicked['text']}' at ({clicked['cx']},{clicked['cy']})")
                else:
                    box = pb.bounding_box()
                    cx = cy = None
                    if box:
                        cx = round(box["x"] + box["width"] / 2)
                        cy = round(box["y"] + box["height"] / 2)
                    print(f"[PUBLISH] clicking selector='ytcp-button#publish-button' box={box} center=({cx},{cy})")
                    pb.click(timeout=6000, force=True)
                    print(f"[PUBLISH] ✓ clicked via Playwright at ({cx},{cy})")
                print(f"[{username}] YouTube publish clicked (attempt {attempt+1})")
            except Exception as e:
                print(f"[{username}] YouTube publish click err: {e}")

            for _ in range(15):
                time.sleep(3)
                take_screenshot(username)
                if page.locator(SUCCESS).count() > 0:
                    published = True
                    break
                try:
                    body = page.inner_text("body", timeout=2000) or ""
                    if any(k in body.lower() for k in
                           ["uploaded", "published", "video is live", "your video has been uploaded",
                            "is being processed", "done"]):
                        published = True
                        break
                except Exception:
                    pass
            if published:
                break
            time.sleep(3)

        if published:
            _save_debug_html(page, "YT_SUCCESS", username)
            _log_event(username, "YouTube: video published")
            try:
                page.goto("https://studio.youtube.com", timeout=20000)
            except Exception:
                pass
            return True
        else:
            _save_debug_html(page, "YT_FAIL", username)
            _log_event(username, "YouTube: publish NOT confirmed")
            take_screenshot(username)
            return False

    except Exception as fatal:
        print(f"[{username}] FATAL YOUTUBE UPLOAD: {fatal}")
        import traceback
        traceback.print_exc()
        _save_debug_html(page, "YT_FATAL", username)
        take_screenshot(username)
        return False


def _init_worker_browser(username, account):
    """Launch a FRESH Playwright browser on the worker thread and load cookies.

    IMPORTANT: the worker thread is the Playwright owner for this session, so all
    Playwright calls (including take_screenshot for the live cam) must happen on
    THIS thread. We never reuse a browser started by connect_account, because
    that ran on a different thread and would raise greenlet 'cannot switch to a
    different thread' errors.
    """
    platform = (account.get("platform") or "TikTok")

    # Tear down any stale session (e.g. leftover from a previous run) so we own a
    # fresh browser on the worker thread.
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            browser_sessions[username]["pw"].stop()
        except Exception:
            pass
        del browser_sessions[username]

    # Build the cookie list (if a session was pasted). For YouTube we ALSO
    # support a persistent profile with no pasted cookies — the profile itself
    # stores the login, so we just reuse it.
    clean_cookies = []
    raw = account.get("session_data") or ""
    if raw:
        try:
            cookies = json.loads(raw)
            for c in cookies:
                if not isinstance(c, dict) or "name" not in c or "value" not in c:
                    continue
                is_secure = bool(c.get("secure", False)) or (".google.com" in _cookie_domain_for(c, platform) or ".youtube.com" in _cookie_domain_for(c, platform))
                cleaned = {
                    "name": c["name"],
                    "value": c["value"],
                    "domain": _cookie_domain_for(c, platform),
                    "path": c.get("path", "/"),
                    "secure": is_secure,
                    "httpOnly": c.get("httpOnly", False),
                }
                cleaned["sameSite"] = c["sameSite"] if c.get("sameSite") in ["Strict", "Lax", "None"] else "Lax"
                clean_cookies.append(cleaned)
        except Exception:
            clean_cookies = []

    if not clean_cookies and platform != "YouTube":
        # TikTok always needs pasted cookies (no persistent manual login flow).
        return False

    proxy_cfg = _get_proxy(account)
    session = _start_browser_session(username, account)
    home_url = "https://www.youtube.com" if platform == "YouTube" else "https://www.tiktok.com"

    # If we have cookies, load them on top of the persistent profile.
    if clean_cookies:
        try:
            page = session["page"]
            page.goto(home_url, timeout=30000)
            session["context"].add_cookies(clean_cookies)
            page.reload(timeout=30000)
            time.sleep(3)
        except Exception as e:
            # If a proxy was configured and the site timed out, fall back to a
            # DIRECT (no proxy) browser so a bad proxy setting never kills it.
            if proxy_cfg and ("TIMED_OUT" in str(e) or "net::" in str(e)):
                print(f"[{username}] worker proxy goto failed ({e}); retrying WITHOUT proxy")
                try:
                    session["context"].close(); session["browser"].close(); session["pw"].stop()
                except Exception:
                    pass
                browser_sessions.pop(username, None)
                session = _start_browser_session(username, account, no_proxy=True)
                try:
                    page = session["page"]
                    page.goto(home_url, timeout=30000)
                    session["context"].add_cookies(clean_cookies)
                    page.reload(timeout=30000)
                    time.sleep(3)
                except Exception as e2:
                    print(f"[{username}] worker direct retry warning: {e2}")
            else:
                print(f"[{username}] worker cookie load warning: {e}")
    else:
        # YouTube + persistent profile, no pasted cookies: just open the home
        # page and rely on the profile's stored login.
        try:
            page = session["page"]
            page.goto(home_url, timeout=30000)
            time.sleep(3)
        except Exception as e:
            print(f"[{username}] worker profile open warning: {e}")
    return True

def _posted_ids_path(username):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", username)
    return os.path.join(DOWNLOADS_DIR, f"posted_{safe}.json")


def load_posted_ids(username):
    """Load the set of already-posted video ids (so we never repeat a clip)."""
    try:
        with open(_posted_ids_path(username), "r") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_posted_ids(username, ids):
    try:
        with open(_posted_ids_path(username), "w") as f:
            json.dump(sorted(ids), f)
    except Exception:
        pass


def automation_worker(username):
    log(f"[{username}] === AUTOMATION WORKER STARTED ===")
    # Load the history of already-posted videos so we NEVER repeat a clip,
    # even across restarts of the worker/bot.
    posted_video_ids = load_posted_ids(username)

    try:
      while True:
        account = get_account(username)
        if not account or not account["enabled"]:
            log(f"[{username}] Worker stopping: enabled={account.get('enabled') if account else 'no account'}")
            break

        if not account["connected"]:
            update_account(username, current_task="Not connected")
            time.sleep(5)
            continue

        video_file = None
        try:
            # Initialize worker's own browser session on this thread
            if username not in browser_sessions:
                update_account(username, current_task="Starting worker browser...")
                if not _init_worker_browser(username, account):
                    log(f"[{username}] Failed to initialize worker browser")
                    update_account(username, enabled=0, current_task="Browser init failed")
                    break
                
            # Take a screenshot to show we're alive
            take_screenshot(username)

            category = account.get("category") or "dance"
            platform = account.get("platform") or "TikTok"
            profile_link = (account.get("profile_link") or "").strip()

            page = _get_page(username)
            if page:
                handle_captcha_if_present(page, username)
                handle_content_check_dialog(page, username)

            # --- TikTok PROFILE mode: only post videos from the given profile ---
            if platform == "TikTok" and profile_link:
                log(f"[{username}] Step 1-2: PROFILE MODE — sourcing from {profile_link}")
                update_account(username, current_task="PROFILE MODE: picking a video from source profile...")
                candidates = scrape_profile_videos(username, profile_link, exclude=posted_video_ids)
                if not candidates:
                    update_account(username, current_task="No unused profile videos found, retrying in 2 min")
                    time.sleep(120)
                    continue
                # Most-recent first; pick randomly within the first few to vary captions.
                pool = candidates[:6]
                video_info = random.choice(pool)
                print(f"[{username}] PROFILE MODE selected {video_info['url']}")
            else:
                # --- Step 1: search TikTok in the browser ---
                # NOTE: searches ALWAYS happen on TikTok (never YouTube) to source
                # the clips — both platforms reuse the TikTok search.
                log(f"[{username}] Step 1: Searching TikTok '{category}' (platform={platform})")
                update_account(username, current_task=f"Step 1: Searching '{category}'...")

                search_ok = search_on_tiktok(username, category)
                if not search_ok:
                    log(f"[{username}] search step failed, using API fallback")

                # --- Step 2: find a viral video in the results ---
                log(f"[{username}] Step 2: Finding viral video...")
                update_account(username, current_task="Step 2: Finding viral video...")
                video_info = find_viral_video(username, category, exclude=posted_video_ids)
                if not video_info:
                    update_account(username, current_task="No viral video found, retrying in 2 min")
                    time.sleep(120)
                    continue

            if video_info.get("video_id") in posted_video_ids:
                update_account(username, current_task="Already posted that one, searching again...")
                time.sleep(30)
                continue

            # --- Step 3: download without watermark ---
            log(f"[{username}] Step 3: Downloading video (no watermark)...")
            update_account(username, current_task="Step 3: Downloading video (no watermark)...")
            video_file = download_video_no_watermark(username, video_info)
            if not video_file:
                update_account(username, current_task="Download failed, retrying in 2 min")
                time.sleep(120)
                continue

            # --- Prepare video for the target platform (YouTube gets exact
            #     1080x1920 + good duration; TikTok keeps the original clip) ---
            prepared_file = prepare_video_for_platform(username, video_file, platform)

            # --- Step 4: generate category-matched, platform-aware caption ---
            update_account(username, current_task="Step 4: Generating caption...")
            caption = generate_caption(video_info, category, platform)
            title = category
            print(f"[{username}] caption: {caption}")

            # --- Steps 5-6: upload & post ---
            if platform == "YouTube":
                log(f"[{username}] Step 5: Uploading to YouTube Shorts...")
                update_account(username, current_task="Step 5: Uploading to YouTube Shorts...")
                page = _get_page(username)
                if page:
                    _dismiss_youtube_popups(page, username)
                success = upload_video_to_youtube(username, prepared_file, caption, title)
            else:
                log(f"[{username}] Step 5: Uploading to TikTok...")
                update_account(username, current_task="Step 5: Uploading to TikTok...")
                success = upload_video_to_tiktok(username, video_file, caption)
            log(f"[{username}] Upload result: {success}")

            # Clean up the prepared (YouTube) copy if it differs from the original.
            if prepared_file and prepared_file != video_file and os.path.exists(prepared_file):
                try:
                    os.remove(prepared_file)
                except Exception:
                    pass

            if success:
                vid = video_info.get("video_id")
                posted_video_ids.add(vid)
                save_posted_ids(username, posted_video_ids)   # persist so we never repeat
                post_cycle_start = time.time()
                now = datetime.now()
                next_dt = now + timedelta(seconds=POST_INTERVAL_SECONDS)
                update_account(
                    username,
                    last_post=now.strftime("%Y-%m-%d %H:%M"),
                    next_post=next_dt.strftime("%H:%M"),
                    next_post_ts=int(post_cycle_start) + POST_INTERVAL_SECONDS,
                    current_task=f"Posted to {platform}! Browsing feed..."
                )

                # ======================================================
                # After posting -> browse the native feed to humanize
                # (TikTok: For You Page. YouTube: Subscriptions/Home).
                # ======================================================
                if platform != "YouTube":
                    try:
                        page = _get_page(username)
                        if page:
                            log(f"[{username}] Going to For You page to humanize...")
                            update_account(username, current_task="Browsing For You Page...")
                            page.goto("https://www.tiktok.com", timeout=25000)
                            time.sleep(random.uniform(2.5, 4.5))

                        hearts = 0
                        fyp_start = time.time()
                        # Keep FYP time comfortably under the 10-min interval
                        fyp_budget = max(60, POST_INTERVAL_SECONDS - 150)

                        while time.time() - fyp_start < fyp_budget:
                            try:
                                # Mostly scroll down, occasionally a small scroll up (re-read)
                                if random.random() < 0.12:
                                    page.mouse.wheel(0, -random.randint(120, 350))
                                else:
                                    page.mouse.wheel(0, random.randint(250, 820))
                                time.sleep(random.uniform(1.0, 3.5))

                                # Sometimes open a video and actually WATCH it (very human)
                                if random.random() < 0.30:
                                    try:
                                        vid = page.locator('a[href*="/video/"]').first
                                        if vid.count() > 0 and vid.is_visible():
                                            vid.click(timeout=2000)
                                            time.sleep(random.uniform(4, 16))  # watch
                                            try:
                                                lb = page.locator('button[aria-label*="Like"], [data-e2e="like-btn"]').first
                                                if lb.count() > 0 and lb.is_visible() and random.random() < 0.7:
                                                    lb.click(timeout=1500)
                                                    hearts += 1
                                            except Exception:
                                                pass
                                            # Return to the For You feed
                                            try:
                                                page.keyboard.press("Escape")
                                                time.sleep(random.uniform(1.0, 2.5))
                                            except Exception:
                                                pass
                                            page.goto("https://www.tiktok.com", timeout=20000)
                                            time.sleep(random.uniform(1.5, 3.0))
                                    except Exception:
                                        pass
                                else:
                                    # Like a video inline on the feed
                                    if random.random() < 0.6:
                                        try:
                                            like_btn = page.locator('button[aria-label*="Like"], [data-e2e="like-btn"]').first
                                            if like_btn.count() > 0 and like_btn.is_visible():
                                                like_btn.click(timeout=1500)
                                                hearts += 1
                                                time.sleep(random.uniform(0.5, 1.8))
                                        except Exception:
                                            pass

                                # Occasional longer "watch" pause (looks like real viewing)
                                if random.random() < 0.22:
                                    time.sleep(random.uniform(4.0, 9.0))

                                # Keep the live preview fresh (no lag)
                                if random.random() < 0.15:
                                    take_screenshot(username)

                            except Exception:
                                time.sleep(2)

                        log(f"[{username}] Humanized on FYP for {int((time.time()-fyp_start)/60)}min — liked {hearts} videos")
                        update_account(username, current_task=f"Liked {hearts} videos on FYP")
                    except Exception as e:
                        log(f"[{username}] FYP humanize error: {e}")
                else:
                    # YouTube: browse the Subscriptions feed and watch a few Shorts.
                    try:
                        page = _get_page(username)
                        if page:
                            log(f"[{username}] Going to YouTube feed to humanize...")
                            update_account(username, current_task="Browsing YouTube feed...")
                            page.goto("https://www.youtube.com/feed/subscriptions", timeout=25000)
                            time.sleep(random.uniform(2.5, 4.5))

                        watched = 0
                        yt_start = time.time()
                        yt_budget = max(60, POST_INTERVAL_SECONDS - 150)
                        while time.time() - yt_start < yt_budget:
                            try:
                                if random.random() < 0.12:
                                    page.mouse.wheel(0, -random.randint(120, 350))
                                else:
                                    page.mouse.wheel(0, random.randint(250, 820))
                                time.sleep(random.uniform(1.0, 3.5))

                                if random.random() < 0.4:
                                    try:
                                        v = page.locator('a#video-title, ytd-video-renderer a#video-title, a[title]').first
                                        if v.count() > 0 and v.is_visible():
                                            v.click(timeout=2000)
                                            time.sleep(random.uniform(6, 18))  # watch
                                            try:
                                                lb = page.locator('button[aria-label*="Like"], ytd-toggle-button-renderer[is-icon-button] #button').first
                                                if lb.count() > 0 and lb.is_visible() and random.random() < 0.7:
                                                    lb.click(timeout=1500)
                                                    watched += 1
                                            except Exception:
                                                pass
                                            page.goto("https://www.youtube.com/feed/subscriptions", timeout=20000)
                                            time.sleep(random.uniform(1.5, 3.0))
                                    except Exception:
                                        pass
                                else:
                                    if random.random() < 0.5:
                                        try:
                                            like_btn = page.locator('button[aria-label*="Like"], ytd-toggle-button-renderer[is-icon-button] #button').first
                                            if like_btn.count() > 0 and like_btn.is_visible():
                                                like_btn.click(timeout=1500)
                                                watched += 1
                                                time.sleep(random.uniform(0.5, 1.8))
                                        except Exception:
                                            pass

                                if random.random() < 0.22:
                                    time.sleep(random.uniform(4.0, 9.0))
                                if random.random() < 0.15:
                                    take_screenshot(username)
                            except Exception:
                                time.sleep(2)

                        log(f"[{username}] Humanized on YouTube for {int((time.time()-yt_start)/60)}min — liked {watched} videos")
                        update_account(username, current_task=f"Liked {watched} videos on YouTube")
                    except Exception as e:
                        log(f"[{username}] YouTube humanize error: {e}")

                # Accurate wait: sleep ONLY the remaining time until exactly
                # POST_INTERVAL_SECONDS since the post. The cycle then repeats
                # identically (search -> pick new video -> upload -> post).
                remaining = POST_INTERVAL_SECONDS - (time.time() - post_cycle_start)
                remaining = max(0, remaining)
                log(f"[{username}] Next post in {remaining/60:.1f} min (interval={POST_INTERVAL_SECONDS}s)")
                waited = 0
                while waited < remaining:
                    account = get_account(username)
                    if not account or not account["enabled"]:
                        break
                    step = min(10, remaining - waited)
                    time.sleep(step)
                    waited += step
                    # Keep the live cam preview fresh while we wait. This runs on
                    # the worker thread (the Playwright owner), so it's safe.
                    if waited % 10 == 0:
                        take_screenshot(username)
            else:
                log(f"[{username}] Post failed, retrying in 3 min")
                update_account(username, current_task="Post failed, retrying in 3 min")
                time.sleep(180)

        except Exception as e:
            import traceback
            traceback.print_exc()
            log(f"[{username}] Step error: {e}")
            update_account(username, current_task=f"Error: {str(e)[:50]}")
            time.sleep(30)
        finally:
            if video_file and os.path.exists(video_file):
                try:
                    os.remove(video_file)
                except Exception:
                    pass

    except Exception as fatal:
      import traceback
      traceback.print_exc()
      log(f"[{username}] FATAL ERROR in automation worker: {fatal}")
      update_account(username, current_task=f"Fatal error: {str(fatal)[:50]}")

    update_account(username, status="Stopped", current_task="Idle")
    log(f"[{username}] === AUTOMATION WORKER STOPPED ===")
    workers.pop(username, None)
    
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            browser_sessions[username]["pw"].stop()
        except:
            pass
        del browser_sessions[username]


def start_automation(username):
    if username in workers:
        return
    thread = threading.Thread(target=automation_worker, args=(username,), daemon=True)
    workers[username] = thread
    thread.start()


def stop_automation(username):
    # Signal-only: the worker thread owns the browser, so it must close it (doing
    # so from the Flask thread would raise greenlet 'different thread' errors).
    workers.pop(username, None)
    account = get_account(username)
    if account:
        update_account(username, enabled=0, status="Stopped", current_task="Stopped")


def logout_account(username):
    """Log the account out: mark it disconnected, clear the stored session, and
    let the worker thread close the browser (the worker owns Playwright, so the
    Flask thread must NOT touch the browser — that causes greenlet errors).

    The account row is kept so it can be reconnected later with a new session.
    """
    try:
        # Stop the worker loop (it will break once it sees enabled/connected=0).
        workers.pop(username, None)
        # Remove any saved session file.
        try:
            import shutil
            sp = f"sessions/{username}"
            if os.path.exists(sp):
                shutil.rmtree(sp, ignore_errors=True)
        except Exception:
            pass
    except Exception:
        pass
    update_account(
        username,
        session_data=None,
        connected=0,
        status="Logged out",
        current_task="Logged out",
        logged_in_as="",
    )
    return True


def delete_account_session(username):
    # Signal-only: stop the worker (it owns the browser and will close it on
    # exit). The Flask thread must NOT close Playwright objects.
    workers.pop(username, None)

    import shutil
    session_path = f"sessions/{username}"
    if os.path.exists(session_path):
        shutil.rmtree(session_path, ignore_errors=True)

    # Also clear session_data from DB
    update_account(username, session_data=None, connected=0, status="Disconnected", current_task="Idle", logged_in_as="")
