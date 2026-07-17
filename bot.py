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
    except Exception as e:
        err = str(e).split("\n")[0][:60]
        # Keep the last good frame instead of replacing it with an error card.
        if username not in screenshots:
            screenshots[username] = create_placeholder(username, f"Screenshot error: {err}")
        print(f"[{username}] screenshot error: {err}")


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
    launch_kwargs = dict(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    if proxy:
        launch_kwargs["proxy"] = proxy
        print(f"[{username}] launching browser via proxy: {proxy['server']}")
    else:
        print(f"[{username}] launching browser DIRECT (no proxy)")
    browser = pw.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        locale="en-US",
    )
    page = context.new_page()
    try:
        page.set_default_timeout(25000)
        page.set_default_navigation_timeout(35000)
    except Exception:
        pass
    # Record the thread that owns this Playwright browser. Playwright's sync API
    # is bound to the thread that called sync_playwright().start(), so ALL
    # Playwright calls for this session MUST happen on this thread — never from
    # the Flask request thread or any other thread (would raise greenlet errors).
    session = {"pw": pw, "browser": browser, "context": context, "page": page,
               "owner_thread": threading.current_thread()}
    browser_sessions[username] = session
    return session


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
    log_event(username, f"Connecting to {platform}...")

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
                p.wait_for_load_state("domcontentloaded", timeout=15000)
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
            # Instant, JS-based logged-in detection (no blocking waits). We treat
            # the account as logged in if a SIGN-IN button is absent AND an
            # account avatar / account menu is present. We also log the raw
            # signals so a failure is debuggable in the Logs panel.
            time.sleep(2)  # let the page settle after navigation
            diag = page.evaluate("""() => {
                const out = {};
                const masthead = document.querySelector('ytd-masthead');
                out.hasMasthead = !!masthead;
                // "Sign in" button anywhere in the masthead / end section.
                const signIn = document.querySelector('ytd-masthead a[href*="accounts.google.com"], ytd-button-renderer:has-text("Sign in"), tp-yt-paper-button:has-text("Sign in")');
                out.signInText = signIn ? (signIn.textContent||'').trim().slice(0,40) : '';
                out.hasSignIn = !!signIn;
                // Account avatar image AND the account button (either proves login).
                const av = document.querySelector('ytd-masthead #avatar-btn img, #avatar-btn img, #menu #avatar img, ytd-masthead #account-button img, #account-button img');
                out.hasAvatar = !!av;
                const acctBtn = document.querySelector('ytd-masthead #avatar-btn, #avatar-btn, ytd-masthead #account-button, #account-button');
                out.hasAcctBtn = !!acctBtn;
                // Account/channel handle link.
                const handleA = document.querySelector('a[href^="https://www.youtube.com/@"], a[href^="http://www.youtube.com/@"], a[href*="/@"], a[href^="https://studio.youtube.com/channel/"]');
                out.handle = handleA ? (handleA.getAttribute('href')||'').split('@')[1]||'' : '';
                out.url = location.href;
                return out;
            }""")
            has_avatar = bool(diag.get("hasAvatar")) or bool(diag.get("hasAcctBtn"))
            has_sign_in = bool(diag.get("hasSignIn")) and "sign in" in (diag.get("signInText") or "").lower()
            # Primary signal: a logged-IN YouTube session NEVER shows a "Sign in"
            # button. If there's no Sign in button we are logged in (avatar/handle
            # only reinforce this). This avoids false negatives when the avatar
            # selector doesn't perfectly match the current YouTube DOM.
            logged_in = (not has_sign_in) or bool(diag.get("handle"))
            if has_avatar:
                logged_in = True
            # Fallback: handle link present with a real @handle also means logged in.
            if (diag.get("handle") or "").strip():
                logged_in = True
                if not profile_name:
                    profile_name = diag["handle"].strip().lstrip("@")
            log_event(username, f"YT connect diag: avatar={has_avatar} signIn={has_sign_in} handle='{diag.get('handle','')}' url={diag.get('url','')[:60]}")
            try:
                av = page.locator('ytd-masthead #avatar-btn img, #avatar-btn img, #menu #avatar img').first
                if av.count() > 0:
                    alt = (av.get_attribute("alt") or "").strip()
                    if alt and alt.lower() not in ("avatar image", "avatar", ""):
                        profile_name = alt
            except Exception:
                pass
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
                        page.wait_for_load_state("domcontentloaded", timeout=10000)
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
        os.makedirs("/home/user/debug_htmls", exist_ok=True)
        html = page.content()
        ts = int(time.time())
        path = f"/home/user/debug_htmls/{username}_{label}_{ts}.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        latest = f"/home/user/debug_htmls/LATEST_{label}.html"
        with open(latest, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[{username}] DEBUG HTML saved: {path}")
        return path
    except Exception as e:
        print(f"[{username}] debug_html save error: {e}")
        return None

def _save_debug_screenshot(page, label, username):
    """Explicit debug screenshot to disk (before/after/+5s)."""
    try:
        os.makedirs("/home/user/debug_htmls", exist_ok=True)
        ts = int(time.time())
        path = f"/home/user/debug_htmls/{username}_{label}_{ts}.png"
        page.screenshot(path=path, timeout=8000)
        print(f"[{username}] DEBUG SCREENSHOT: {path}")
        return path
    except Exception as e:
        print(f"[{username}] debug_screenshot error: {e}")
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
            log_event(username, "TikTok: post confirmed/published")
            return True
        else:
            print(f"[{username}] ❌ Post never confirmed (see debug HTMLs / screenshots above)")
            _save_debug_html(page, "FINAL_FAIL", username)
            log_event(username, "TikTok: post NOT confirmed (FINAL_FAIL)")
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
    return page.evaluate("""(args, clickSrc) => {
        const [container, label] = args;
        const root = document.querySelector(container);
        if (!root) return false;
        const want = label.toLowerCase();
        const btns = [...root.querySelectorAll('ytcp-button, tp-yt-paper-button, button, [role="button"]')];
        const b = btns.find(el => {
            const t = (el.textContent || '').trim().toLowerCase();
            return t === want || t.startsWith(want + ' ') || t.endsWith(' ' + want);
        });
        const disabled = (function(el){
            if (!el) return true;
            if (el.disabled === true) return true;
            if (el.hasAttribute && (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true')) return true;
            const sr = el.shadowRoot;
            if (sr) { const inner = sr.querySelector('button, [role="button"], tp-yt-paper-button');
                if (inner && (inner.disabled === true || (inner.hasAttribute && inner.hasAttribute('disabled'))
                    || inner.getAttribute('aria-disabled') === 'true')) return true; }
            return false;
        })(b);
        if (b && !disabled) {
            const fn = new Function('el', clickSrc);
            fn(b);
            return true;
        }
        return false;
    }""", [[container, label], _js_click_element_js()])


def _clear_text_selection(page):
    """Clear any accidental text selection (so the live preview doesn't show the
    whole page highlighted/blue after a click)."""
    try:
        page.evaluate("() => { window.getSelection() && window.getSelection().removeAllRanges(); }")
    except Exception:
        pass


_log_lock = threading.Lock()

def log_event(username, message):
    """Print + persist a timestamped line to the account's rolling log.

    This is what the per-account "Logs" button in the dashboard reads, so it
    captures everything needed to debug issues (verify dialog, Next clicks, etc).
    """
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    print(f"[{username}] {message}")
    try:
        with _log_lock:
            append_log(username, message)
    except Exception:
        pass


def _js_click_element_js():
    """Return the JS source for a robust DOM click that actually triggers
    Polymer/Angular handlers.

    YouTube's buttons are custom elements (ytcp-button / tp-yt-paper-button) that
    wrap the real <button> inside a shadow root. A bare `.click()` on the host
    custom element frequently does NOT fire the dialog's action listener, so the
    dialog just sits there. We fix this by:
      1) Walking into the shadow root to find the inner real button.
      2) Dispatching a full native event sequence (pointerdown -> mousedown ->
         pointerup -> mouseup -> click) with proper coordinates on the element,
         which Polymer listeners react to.
    """
    return """(el) => {
        if (!el) return false;
        // Resolve the actually-clickable element (real <button> inside shadow DOM).
        let target = el;
        const sr = el.shadowRoot;
        if (sr) {
            const inner = sr.querySelector('button, [role="button"], tp-yt-paper-button, ytcp-button')
                       || sr.querySelector('*');
            if (inner) target = inner;
        }
        // Also handle nested custom elements (tp-yt-paper-button might itself wrap).
        let probe = target;
        for (let i = 0; i < 5 && probe && probe.shadowRoot; i++) {
            const deeper = probe.shadowRoot.querySelector('button, [role="button"]');
            if (deeper) { target = deeper; probe = deeper; } else break;
        }
        const r = target.getBoundingClientRect();
        const cx = Math.round(r.x + r.width / 2);
        const cy = Math.round(r.y + r.height / 2);
        const opts = (type) => ({ view: window, bubbles: true, cancelable: true,
                                  composed: true, clientX: cx, clientY: cy,
                                  button: 0, buttons: 1, pointerId: 1,
                                  pointerType: 'mouse' });
        try {
            if (typeof target.dispatchEvent === 'function') {
                target.dispatchEvent(new PointerEvent('pointerdown', opts('pointerdown')));
                target.dispatchEvent(new MouseEvent('mousedown', opts('mousedown')));
                target.dispatchEvent(new PointerEvent('pointerup', opts('pointerup')));
                target.dispatchEvent(new MouseEvent('mouseup', opts('mouseup')));
                target.dispatchEvent(new MouseEvent('click', opts('click')));
            }
        } catch (e) {}
        // Fallback: native click on whichever element is clickable.
        try { (target.click ? target : el).click(); } catch (e) {}
        return true;
    }"""


def _js_is_disabled(el_expr):
    """JS expression (returns bool) evaluating whether `el` (a DOM node) is
    disabled, checking native prop, the [disabled] attribute on the host AND on
    any shadow-root inner button — Polymer reflects disabled in several places."""
    return """(() => {
        const el = %s;
        if (!el) return true;
        if (el.disabled === true) return true;
        if (el.hasAttribute && (el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true')) return true;
        const sr = el.shadowRoot;
        if (sr) {
            const inner = sr.querySelector('button, [role="button"], tp-yt-paper-button');
            if (inner) {
                if (inner.disabled === true) return true;
                if (inner.hasAttribute && (inner.hasAttribute('disabled') || inner.getAttribute('aria-disabled') === 'true')) return true;
            }
        }
        return false;
    })()""" % el_expr


def _handle_youtube_auth_dialog(page, username=""):
    """Handle YouTube Studio's 'Verify that it's you' dialog.

    This dialog intercepts ALL clicks on the upload form, so it MUST be resolved
    first. Its primary action button is labelled 'Next' (often #confirm-button).

    Strategy (most reliable first):
      1) Find the real, visible, ENABLED Next/Confirm button via Playwright
         locators and click it with Playwright's TRUSTED click (scrolls into
         view, hits the true center, dispatches trusted events Polymer accepts).
      2) If locators miss, fall back to a real page.mouse.click() at the button's
         true bounding-box center (computed from the live element handle).
      3) Only if both fail do we report "needs_code".

    We NEVER click a DISABLED button. When the dialog has advanced to a step that
    waits for a verification code/phone (the Next button is disabled), we STOP and
    let the user finish it (no hammering that drags a text selection).
    """
    try:
        # Candidate containers (most specific first).
        container_sels = [
            'ytcp-auth-confirmation-dialog',
            'ytcp-dialog',
            'ytcp-upload-confirmation-dialog',
            '[role="dialog"]',
        ]
        container = None
        for cs in container_sels:
            try:
                loc = page.locator(cs).first
                if loc.count() > 0 and loc.is_visible():
                    container = loc
                    break
            except Exception:
                continue
        if container is None:
            # Maybe a dialog exists but isn't matched above; try a broad check.
            has_dlg = page.evaluate("""() => !!document.querySelector('[role="dialog"], ytcp-auth-confirmation-dialog, ytcp-dialog')""")
            if not has_dlg:
                return False

        print(f"[{username}] ⚠ YouTube verify/confirm dialog detected")
        log_event(username, "YouTube 'Verify that's you' dialog detected")

        # 1) Playwright locator click on a visible, enabled Next/Confirm button.
        button_sels = [
            'ytcp-auth-confirmation-dialog #confirm-button',
            'ytcp-auth-confirmation-dialog #next-button',
            '#confirm-button', '#next-button',
            'ytcp-button:has-text("Next")',
            'tp-yt-paper-button:has-text("Next")',
            'button:has-text("Next")',
            'ytcp-button:has-text("Confirm")',
            'button:has-text("Confirm")',
            '[role="dialog"] ytcp-button:visible, [role="dialog"] button:visible',
        ]
        clicked_handle = None
        for sel in button_sels:
            try:
                b = page.locator(sel).first
                if b.count() == 0 or not b.is_visible():
                    continue
                if b.is_disabled():
                    print(f"[{username}] verify button '{sel}' DISABLED — skipping")
                    continue
                box = b.bounding_box()
                if not box or box["width"] <= 0 or box["height"] <= 0:
                    continue
                cx = round(box["x"] + box["width"] / 2)
                cy = round(box["y"] + box["height"] / 2)
                print(f"[{username}] verify clicking Next via Playwright '{sel}' center=({cx},{cy})")
                log_event(username, f"Verify dialog: clicking Next via '{sel}' at ({cx},{cy})")
                b.click(timeout=5000, force=True, no_wait_after=True)
                clicked_handle = b
                break
            except Exception as e:
                print(f"[{username}] verify Playwright click '{sel}' failed: {str(e)[:80]}")
                continue

        # 2) Fallback: JS-driven real mouse click at the true button center.
        if clicked_handle is None:
            pick = page.evaluate("""() => {
                const containers = ['ytcp-auth-confirmation-dialog','ytcp-dialog','ytcp-upload-confirmation-dialog','[role=\"dialog\"]'];
                let root = null;
                for (const c of containers) { const el = document.querySelector(c);
                    if (el && (el.offsetParent !== null || getComputedStyle(el).display !== 'none')) { root = el; break; } }
                if (!root) return {found:false};
                const disabled = (el) => {
                    if (!el) return true;
                    if (el.disabled === true) return true;
                    if (el.hasAttribute && (el.hasAttribute('disabled') || el.getAttribute('aria-disabled')==='true')) return true;
                    const sr = el.shadowRoot;
                    if (sr) { const i = sr.querySelector('button, [role=\"button\"], tp-yt-paper-button');
                        if (i && (i.disabled===true || (i.hasAttribute && i.hasAttribute('disabled')) || i.getAttribute('aria-disabled')==='true')) return true; }
                    return false;
                };
                const cands = [...root.querySelectorAll('ytcp-button, tp-yt-paper-button, button, [role=\"button\"]')];
                let b = root.querySelector('#confirm-button') || root.querySelector('#next-button');
                if (b && disabled(b)) b = null;
                if (!b) b = cands.find(el => { const t=(el.textContent||'').trim().toLowerCase();
                    return (t==='next'||t==='confirm'||t==='continue') && !disabled(el)
                        && el.getBoundingClientRect().width>0; });
                if (!b) return {found:false};
                const r = b.getBoundingClientRect();
                return {found:true, id:b.id||'', text:(b.textContent||'').trim().slice(0,30),
                        cx:Math.round(r.x+r.width/2), cy:Math.round(r.y+r.height/2),
                        vw:window.innerWidth, vh:window.innerHeight};
            }""")
            if pick and pick.get("found"):
                cx, cy = pick["cx"], pick["cy"]
                print(f"[{username}] verify Next <#{pick['id']}> text='{pick['text']}' center=({cx},{cy}) vp=({pick['vw']}x{pick['vh']})")
                log_event(username, f"Verify dialog: Next <#{pick['id']}> text='{pick['text']}' center=({cx},{cy})")
                if 0 < cx < pick["vw"] and 0 < cy < pick["vh"]:
                    _clear_text_selection(page)
                    try:
                        page.mouse.click(cx, cy, delay=80, button="left")
                        clicked_handle = True
                    except Exception as me:
                        print(f"[{username}] verify mouse click err: {me}")

        _clear_text_selection(page)
        if clicked_handle is None:
            # Dump the dialog DOM so we can see the real structure.
            try:
                html = page.evaluate("""() => {
                    const c = ['ytcp-auth-confirmation-dialog','ytcp-dialog','[role=\"dialog\"]'];
                    for (const x of c) { const d=document.querySelector(x); if (d) return d.outerHTML.slice(0,5000); }
                    return 'NO DIALOG IN DOM';
                }""")
                print(f"[{username}] ⚠ verify Next NOT clickable. Dialog DOM:\n{html}")
                log_event(username, f"Verify Next NOT clickable. Dialog DOM: {html[:1500]}")
            except Exception as e:
                print(f"[{username}] ⚠ verify Next NOT clickable (dump err {e})")
                log_event(username, f"Verify Next NOT clickable (dump err {e})")
            # Is the dialog actually waiting for a code? (Next present but disabled)
            waiting = page.evaluate("""() => {
                const c = ['ytcp-auth-confirmation-dialog','ytcp-dialog','[role=\"dialog\"]'];
                for (const x of c) { const root=document.querySelector(x); if(!root) continue;
                    const b = root.querySelector('#confirm-button')||root.querySelector('#next-button')
                        || [...root.querySelectorAll('ytcp-button,button,[role=\"button\"]')].find(el=>(el.textContent||'').trim().toLowerCase()==='next');
                    if (b) return {present:true, disabled: !!(b.disabled || (b.hasAttribute && b.hasAttribute('disabled')))};
                }
                return {present:false};
            }""")
            if waiting.get("present") and waiting.get("disabled"):
                update_account(username, current_task="Verify that it's you — enter the code in live cam")
                return "needs_code"
            update_account(username, current_task="Verify that it's you — could not click Next (see log)")
            return "needs_code"

        log_event(username, "Verify dialog: clicked Next successfully")
        update_account(username, current_task="Verify that it's you \u2014 clicked Next, waiting...")
        # After clicking Next the verify wizard may: (a) close, (b) move to another
        # step (e.g. "choose a method"), or (c) ask for a 6-digit code. We poll for
        # up to ~5 min, handling code entry automatically when the user supplies a
        # code via the dashboard. Automation is NEVER stopped by this dialog \u2014 we
        # only return once the dialog is actually gone (or after the long timeout).
        def _dialog_present():
            try:
                return page.evaluate("""() => {
                    const c = ['ytcp-auth-confirmation-dialog','ytcp-dialog','ytcp-upload-confirmation-dialog','[role=\"dialog\"]'];
                    for (const x of c) { const d=document.querySelector(x);
                        if (d && d.offsetParent !== null) return true; }
                    return false;
                }""")
            except Exception:
                return False

        def _enter_code_and_submit():
            # Look for a visible numeric/text input inside the dialog and type the
            # user-provided 6-digit code, then click Next/Submit/Verify.
            code = get_verify_code(username)
            if not code:
                return False
            filled = page.evaluate("""(code) => {
                const c = ['ytcp-auth-confirmation-dialog','ytcp-dialog','[role=\"dialog\"]'];
                let root = null;
                for (const x of c) { const d=document.querySelector(x); if (d && d.offsetParent!==null) { root=d; break; } }
                if (!root) return false;
                // Prefer an input inside the dialog (code entry fields).
                const inp = root.querySelector('input[type=\"text\"], input[type=\"tel\"], input[type=\"number\"], input:not([type]), textarea');
                if (!inp) return false;
                const settable = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                if (settable) { settable.call(inp, code); inp.dispatchEvent(new Event('input', {bubbles:true})); }
                else { inp.value = code; inp.dispatchEvent(new Event('input', {bubbles:true})); }
                return true;
            }""", code)
            if not filled:
                return False
            log_event(username, f"Verify dialog: typed code '{code}' into input")
            # Clear the code so it is only used once.
            clear_verify_code(username)
            time.sleep(0.5)
            # Click the enabled Next/Submit/Verify button.
            try:
                b = page.locator('ytcp-auth-confirmation-dialog #confirm-button, #confirm-button, #next-button, ytcp-button:has-text("Next"), ytcp-button:has-text("Submit"), ytcp-button:has-text("Verify"), button:has-text("Next"), button:has-text("Submit"), button:has-text("Verify")').first
                if b.count() > 0 and b.is_visible() and not b.is_disabled():
                    b.click(timeout=5000, force=True)
                    return True
            except Exception:
                pass
            return True

        deadline = time.time() + 300  # up to 5 minutes
        while time.time() < deadline:
            take_screenshot(username)  # keep the live view in sync
            if not _dialog_present():
                print(f"[{username}] \u2713 verify dialog dismissed")
                log_event(username, "Verify dialog: dismissed")
                return "advanced"
            # Code step? If a code is available in the DB, enter it and submit.
            if get_verify_code(username):
                update_account(username, current_task="Verify that it's you \u2014 entering code from dashboard...")
                _enter_code_and_submit()
                time.sleep(3)
                continue
            # Still on a step (choose method etc.) \u2014 wait for the user, but keep
            # the dialog alive (do NOT abort automation).
            update_account(username, current_task="Verify that it's you \u2014 waiting for your code/method in live cam")
            time.sleep(5)
        # Timed out waiting (no code supplied) \u2014 return advanced so the flow
        # retries; automation keeps running regardless.
        log_event(username, "Verify dialog: timed out waiting for code \u2014 continuing")
        return "advanced"
    except Exception as e:
        print(f"[{username}] verify dialog handling err: {e}")
        return False
        print(f"[{username}] verify dialog handling err: {e}")
        return False


def _click_youtube_next(page):
    """Click YouTube Studio's Next button — but NEVER a disabled one, and only
    ONCE. If the 'Verify that it's you' dialog is open, we hand off to
    _handle_youtube_auth_dialog (which skips disabled buttons). Returns True if a
    real (enabled) Next was clicked.
    """
    # 0) If the verify dialog is up, let the dedicated handler deal with it
    #    (it correctly skips disabled buttons). Return whether it advanced.
    try:
        if page.locator('ytcp-auth-confirmation-dialog').count() > 0:
            res = _handle_youtube_auth_dialog(page, "")
            return res == "advanced"
    except Exception:
        pass

    # 1) Try Playwright locators with force=True. Skip disabled buttons.
    selectors = [
        'ytcp-button#next-button',
        '#next-button',
        'tp-yt-paper-button#next-button',
        'ytcp-button:has-text("Next")',
        'button:has-text("Next")',
        'tp-yt-paper-button:has-text("Next")',
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
    # 2) JS fallback: click a visible, ENABLED element whose text is exactly "Next".
    try:
        res = page.evaluate("""(clickSrc) => {
            const els = [...document.querySelectorAll('ytcp-button, tp-yt-paper-button, button, [role="button"]')];
            const next = els.find(el => {
                const disabled = (function(d){
                    if (!d) return true;
                    if (d.disabled === true) return true;
                    if (d.hasAttribute && (d.hasAttribute('disabled') || d.getAttribute('aria-disabled') === 'true')) return true;
                    const sr = d.shadowRoot;
                    if (sr) { const inner = sr.querySelector('button, [role="button"], tp-yt-paper-button');
                        if (inner && (inner.disabled === true || (inner.hasAttribute && inner.hasAttribute('disabled'))
                            || inner.getAttribute('aria-disabled') === 'true')) return true; }
                    return false;
                })(el);
                return (el.textContent || '').trim().toLowerCase() === 'next'
                    && !disabled && el.offsetParent !== null;
            });
            if (next) {
                const r = next.getBoundingClientRect();
                const fn = new Function('el', clickSrc);
                fn(next);
                return {clicked: true, tag: next.tagName.toLowerCase(), id: next.id || '',
                         text: (next.textContent||'').trim().slice(0,40),
                         cx: Math.round(r.x + r.width/2), cy: Math.round(r.y + r.height/2)};
            }
            return {clicked: false};
        }""", _js_click_element_js())
        _clear_text_selection(page)
        if res and res.get("clicked"):
            print(f"[NEXT] ✓ clicked via JS <{res['tag']}#{res['id']}> text='{res['text']}' at ({res['cx']},{res['cy']})")
            return True
        print(f"[NEXT] no enabled 'Next' element found")
        return False
    except Exception as e:
        print(f"[NEXT] JS fallback err: {e}")
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
        print(f"[{username}] === YOUTUBE UPLOAD FLOW ===")
        update_account(username, current_task="Opening YouTube Studio upload...")

        upload_url_used = None
        for u in ["https://studio.youtube.com/channel/upload",
                  "https://www.youtube.com/upload",
                  "https://studio.youtube.com"]:
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
        for sec in range(240):  # up to 8 min
            time.sleep(3)
            # The "Verify that it's you" dialog can appear during processing.
            _handle_youtube_auth_dialog(page, username)
            # Title input indicates the details screen is ready
            title_loc = page.locator('#title-textarea, #textbox[label*="Title"], ytcp-mention-textbox[label*="Title"], input[placeholder*="Title"]').first
            if title_loc.count() > 0 and title_loc.is_visible():
                details_ready = True
                break
            if sec % 15 == 0:
                take_screenshot(username)

        if not details_ready:
            print(f"[{username}] ⚠ YouTube details form not detected, attempting anyway")

        take_screenshot(username)

        # Title — keep it short & accurate, with #Shorts for discovery.
        try:
            if title:
                t = (title[:58] + " #Shorts") if len(title) <= 58 else (title[:58] + "…")
            else:
                t = "Daily Short #Shorts"
            tl = page.locator('#title-textarea, #textbox[label*="Title"], ytcp-mention-textbox[label*="Title"], input[placeholder*="Title"]').first
            if tl.count() > 0:
                tl.click(timeout=4000, force=True)
                time.sleep(0.3)
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                tl.type(t, delay=25)
                print(f"[{username}] ✓ YouTube title set: {t}")
        except Exception as ce:
            print(f"[{username}] YouTube title EXCEPTION: {ce}")

        # Description — paste the full accurate caption.
        try:
            dl = page.locator('#description-textarea, #textbox[label*="Description"], ytcp-mention-textbox[label*="Description"], textarea[placeholder*="Description"]').first
            if dl.count() > 0:
                dl.click(timeout=4000, force=True)
                time.sleep(0.3)
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                dl.type(caption, delay=12)
                print(f"[{username}] ✓ YouTube description set")
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
        # Robustly select "No, it's not made for kids" (matches "No" inside the
        # radio whose label contains "made for kids" / "not made for kids").
        try:
            not_kids = page.locator(
                'tp-yt-paper-radio-button:has-text("No"), '
                'paper-radio-button:has-text("No"), '
                'ytcp-radio-button:has-text("No"), '
                '[role="radio"]:has-text("No")'
            ).first
            if not_kids.count() > 0 and not_kids.is_visible():
                not_kids.click(timeout=4000, force=True)
                time.sleep(0.5)
                print(f"[{username}] ✓ set Not made for kids = No")
                log_event(username, "Set 'Not made for kids' = No")
        except Exception as e:
            print(f"[{username}] ⚠ could not set Not-made-for-kids: {e}")
            log_event(username, f"Could not set Not-made-for-kids: {e}")

        # Next -> Next -> Next (Details -> Video elements -> Checks -> Public).
        # We VERIFY each click actually advanced the form by comparing a page
        # "signature" (which dialogs/steps are present) before and after — if the
        # click didn't advance, we retry with the alternative Next control.
        def _page_signature():
            try:
                return page.evaluate("""() => {
                    const ids = [...document.querySelectorAll('[id]')].map(e => e.id).join('|');
                    const titles = [...document.querySelectorAll('ytcp-video-metadata-editor, ytcp-upload-renderer, [class*="step"], [role="dialog"]')]
                        .map(e => (e.textContent||'').slice(0,30)).join('|');
                    return ids + '###' + titles;
                }""")
            except Exception:
                return str(time.time())

        for step in range(6):
            # If the verify dialog needs user action (code / multi-step), STOP the
            # whole upload here — don't hammer Next behind the dialog. The worker
            # will wait and we surface it in the live cam + Logs.
            auth = _handle_youtube_auth_dialog(page, username)
            if auth == "needs_code":
                print(f"[{username}] ⚠ verify dialog needs user action — aborting upload step, will wait")
                log_event(username, "Verify dialog needs user action — upload paused, waiting in live cam")
                take_screenshot(username)
            if auth == "needs_code":
                # Verify dialog still needs user input — keep automation running.
                log_event(username, "Verify dialog still needs input — automation continues")
                take_screenshot(username)
                time.sleep(5)
                continue

            # Before each Next: reveal "Made for kids" and set "No" (required to proceed).
            try:
                more = page.locator('button:has-text("Show more")').first
                if more.count() > 0 and more.is_visible():
                    more.click(timeout=3000, force=True)
                    time.sleep(0.8)
            except Exception:
                pass
            try:
                not_kids = page.locator(
                    'tp-yt-paper-radio-button:has-text("No"), paper-radio-button:has-text("No"), '
                    'ytcp-radio-button:has-text("No"), [role="radio"]:has-text("No")'
                ).first
                if not_kids.count() > 0 and not_kids.is_visible():
                    not_kids.click(timeout=3000, force=True)
                    time.sleep(0.4)
            except Exception:
                pass

            before = _page_signature()
            _log_click_targets(page, username, f"NEXT_STEP_{step+1}")
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

            print(f"[{username}] ✓ clicked Next (step {step+1})")
            time.sleep(random.uniform(2.0, 3.5))
            after = _page_signature()
            if after == before:
                print(f"[{username}] ⚠ Next did NOT advance the form (step {step+1}) — retrying with dialog Next")
                try:
                    _click_dialog_button_js(page, 'ytcp-upload-renderer, ytcp-video-metadata-editor, [role="dialog"]', 'Next')
                except Exception:
                    pass
                time.sleep(2)
            take_screenshot(username)

        # Set visibility to Public (Everyone)
        _handle_youtube_auth_dialog(page, username)
        try:
            public = page.locator(
                'tp-yt-paper-radio-button:has-text("Public"), paper-radio-button:has-text("Public"), '
                'ytcp-radio-button:has-text("Public"), [role="radio"]:has-text("Public"), '
                'tp-yt-paper-radio-button:has-text("Everyone"), paper-radio-button:has-text("Everyone")'
            ).first
            if public.count() > 0 and public.is_visible():
                public.click(timeout=4000, force=True)
                time.sleep(0.5)
                print(f"[{username}] ✓ set visibility = Public")
                log_event(username, "Set visibility = Public")
        except Exception as e:
            print(f"[{username}] ⚠ could not set Public visibility: {e}")
            log_event(username, f"Could not set Public visibility: {e}")

        take_screenshot(username)
        _save_debug_screenshot(page, "yt_pre_publish", username)
        _save_debug_html(page, "YT_PRE_PUBLISH", username)

        # Click Publish
        published = False
        for attempt in range(4):
            auth = _handle_youtube_auth_dialog(page, username)
            if auth == "needs_code":
                print(f"[{username}] ⚠ verify dialog needs user action before Publish — waiting")
                log_event(username, "Verify dialog blocks Publish — waiting for user in live cam")
                take_screenshot(username)
            if auth == "needs_code":
                # Verify dialog still needs user input — keep automation running.
                log_event(username, "Verify dialog still needs input — automation continues")
                take_screenshot(username)
                time.sleep(5)
                continue
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
                            p.click();
                            return {ok:true, tag:p.tagName.toLowerCase(), id:p.id||'',
                                    text:(p.textContent||'').trim().slice(0,40),
                                    cx:Math.round(r.x+r.width/2), cy:Math.round(r.y+r.height/2)};
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
            log_event(username, "YouTube: video published")
            try:
                page.goto("https://studio.youtube.com", timeout=20000)
            except Exception:
                pass
            return True
        else:
            _save_debug_html(page, "YT_FAIL", username)
            log_event(username, "YouTube: publish NOT confirmed (YT_FAIL)")
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

    try:
        cookies = json.loads(account["session_data"])
    except Exception:
        return False

    proxy_cfg = _get_proxy(account)
    session = _start_browser_session(username, account)
    clean_cookies = []
    for c in cookies:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
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
        return False

    # Navigate to the platform home first, then add cookies, so the cookie
    # domain is recognised (same pattern as connect_account).
    try:
        page = session["page"]
        home_url = "https://www.youtube.com" if platform == "YouTube" else "https://www.tiktok.com"
        page.goto(home_url, timeout=30000)
        session["context"].add_cookies(clean_cookies)
        page.reload(timeout=30000)
        time.sleep(3)
    except Exception as e:
        # If a proxy was configured and the site timed out, fall back to a DIRECT
        # (no proxy) browser so a bad proxy setting never kills the worker.
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
    log_event(username, "Worker started")
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

            # --- Step 1: search TikTok in the browser ---
            # NOTE: searches ALWAYS happen on TikTok (never YouTube) to source
            # the clips — both platforms reuse the TikTok search.
            log(f"[{username}] Step 1: Searching TikTok '{category}' (platform={platform})")
            update_account(username, current_task=f"Step 1: Searching '{category}'...")

            page = _get_page(username)
            if page:
                handle_captcha_if_present(page, username)
                handle_content_check_dialog(page, username)

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
    log_event(username, "Automation started")
    thread = threading.Thread(target=automation_worker, args=(username,), daemon=True)
    workers[username] = thread
    thread.start()


def stop_automation(username):
    # Signal-only: the worker thread owns the browser, so it must close it (doing
    # so from the Flask thread would raise greenlet 'different thread' errors).
    log_event(username, "Automation stopped")
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
