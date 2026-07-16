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
from database import get_account, update_account

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
# always ~10 minutes (not 25+ as before).
POST_INTERVAL_SECONDS = 600  # 10 minutes

# How many of the top-ranked candidates to randomly choose between, so we never
# keep picking the exact same viral video every cycle.
VIDEO_CHOICE_POOL = 6
# Category key -> the ACTUAL TikTok search query used (search page + tikwm API).
# Keys are the exact display labels chosen in the dashboard (so what you pick
# is what gets stored and searched).
CATEGORY_SEARCH = {
    "Dance": "dance",
    "Horror": "horror",
    "Story Animation": "Horror Story Animation",
    "Gin Stories": "Jinn stories Islam",       # shown as "Gin Stories"
    "Scary facts": "Scary Facts",              # shown as "Scary facts"
    "Funny Videos": "Funny Videos",
    "Predator Catches": "Pred catches",
}

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
        handle_captcha_if_present(page, username)  # <--- CAPTCHA SOLVER (isolated)
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

        # FIX: We no longer spawn a separate screenshot loop here, nor keep the browser open.
        # This function is now strictly for verification.
        
        # Close the verification browser to free it up for the automation worker
        try:
            page.close()
            context.close()
            browser.close()
            pw.stop()
            del browser_sessions[username]
        except Exception:
            pass

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


def generate_caption(video_info, category):
    """Generate category-aware captions that actually match the content."""
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
    }

    base = random.choice(templates.get(cat, templates["dance"]))

    # Add relevant hashtags
    pool = CATEGORY_HASHTAGS.get(cat, ["#fyp", "#viral", "#trending"])
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
            return True
        else:
            print(f"[{username}] ❌ Post never confirmed (see debug HTMLs / screenshots above)")
            _save_debug_html(page, "FINAL_FAIL", username)
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

def _init_worker_browser(username, account):
    """Initialize a Playwright browser session strictly for the worker thread."""
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            browser_sessions[username]["pw"].stop()
        except:
            pass
        del browser_sessions[username]
        
    try:
        cookies = json.loads(account["session_data"])
    except:
        return False

    pw = sync_playwright().start()
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
    
    clean_cookies = []
    for c in cookies:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        cleaned = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".tiktok.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
        }
        if "sameSite" in c and c["sameSite"] in ["Strict", "Lax", "None"]:
            cleaned["sameSite"] = c["sameSite"]
        clean_cookies.append(cleaned)
        
    context.add_cookies(clean_cookies)
    page = context.new_page()
    
    browser_sessions[username] = {
        "pw": pw,
        "browser": browser,
        "context": context,
        "page": page
    }
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

            # --- Step 1: search TikTok in the browser ---
            log(f"[{username}] Step 1: Searching '{category}'")
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

            # --- Step 4: generate category-matched caption ---
            update_account(username, current_task="Step 4: Generating caption...")
            caption = generate_caption(video_info, category)
            print(f"[{username}] caption: {caption}")

            # --- Steps 5-6: upload & post ---
            log(f"[{username}] Step 5: Uploading to TikTok...")
            update_account(username, current_task="Step 5: Uploading to TikTok...")
            success = upload_video_to_tiktok(username, video_file, caption)
            log(f"[{username}] Upload result: {success}")

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
                    current_task=f"Posted! Going to For You Page..."
                )

                # ======================================================
                # After posting -> go to For You, heart & scroll (capped
                # so the full cycle stays ~10 min, then sleep the remainder)
                # ======================================================
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
    if username in workers:
        del workers[username]
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            browser_sessions[username]["pw"].stop()
            del browser_sessions[username]
        except:
            pass


def logout_account(username):
    """Log the TikTok account out: end the server session in the live browser
    (if any), clear cookies, wipe the stored session, and mark it 'Logged out'.
    The account row is kept so it can be reconnected later with a new session.
    """
    try:
        sess = browser_sessions.get(username)
        if sess:
            try:
                page = sess.get("page")
                if page:
                    page.goto("https://www.tiktok.com/logout", timeout=15000, wait_until="domcontentloaded")
            except Exception:
                pass
            try:
                sess.get("context").clear_cookies()
            except Exception:
                pass
            try:
                sess["context"].close()
                sess["browser"].close()
                sess["pw"].stop()
            except Exception:
                pass
            browser_sessions.pop(username, None)
        # Stop the worker loop (it will break once it sees connected=0)
        workers.pop(username, None)
        # Remove any saved session file
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
    )
    return True


def _must_click(page, labels_or_selectors, task="", max_attempts=8, verify_opened=None):
    norm = [l.lower().strip() for l in labels_or_selectors if l and not l.startswith(('[', 'button:', 'a:', 'input[', 'div[', 'span[', '#', '.'))]
    selectors = [s for s in labels_or_selectors if s and (s.startswith('[') or s.startswith('button:') or s.startswith('a:') or s.startswith('input[') or s.startswith('div[') or s.startswith('span[') or s.startswith('#') or s.startswith('.'))]
    
    for attempt in range(max_attempts):
        clicked = False
        
        for sel in selectors:
            try:
                if page.locator(sel).count() > 0:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.scroll_into_view_if_needed(timeout=2000)
                        try:
                            page.evaluate("""(el) => el.click()""", el)
                            clicked = True
                            break
                        except Exception:
                            try:
                                el.click(timeout=6000, force=True)
                                clicked = True
                                break
                            except Exception:
                                continue
            except Exception:
                continue
        
        if not clicked and norm:
            for sel in ['button', 'a', '[role="button"]', '[role="link"]', 'div', 'span']:
                try:
                    els = page.locator(sel).all()
                except Exception:
                    continue
                for el in els[:300]:
                    try:
                        if not el.is_visible(timeout=800):
                            continue
                        txt = (el.inner_text(timeout=800) or "").strip().lower()
                        if any(n in txt for n in norm):
                            el.scroll_into_view_if_needed(timeout=2000)
                            try:
                                page.evaluate("""(el) => el.click()""", el)
                                clicked = True
                            except Exception:
                                try:
                                    el.click(timeout=6000, force=True)
                                    clicked = True
                                except Exception:
                                    continue
                            if clicked:
                                break
                    except Exception:
                        continue
                if clicked:
                    break
        
        if clicked:
            time.sleep(1)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            time.sleep(1)
            
            if verify_opened:
                try:
                    if verify_opened(page):
                        log(f"[{task}] Clicked '{labels_or_selectors[0]}' and verified opened on attempt {attempt+1}")
                        return True
                except Exception:
                    pass
            else:
                log(f"[{task}] Clicked '{labels_or_selectors[0]}' on attempt {attempt+1}")
                return True
        
        log(f"[{task}] Attempt {attempt+1}/{max_attempts}: click not registered or page didn't open, retrying...")
        time.sleep(2)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
    
    log(f"[{task}] FAILED to click '{labels_or_selectors[0]}' after {max_attempts} attempts")
    return False


def login_with_google(username, email=""):
    account = get_account(username)
    if not account:
        return False

    email_to_use = (email or account.get("gmail") or "").strip().lower()
    if not email_to_use.endswith("@gmail.com"):
        update_account(username, status="Need Gmail", current_task="Enter a @gmail.com address")
        return False

    update_account(username, status="Google login", current_task="Starting browser...")
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )
        page = context.new_page()
        browser_sessions[username] = {"pw": pw, "browser": browser, "context": context, "page": page}
        log(f"[{username}] Google login: browser ready")

        page.goto("https://www.tiktok.com", timeout=30000)
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        time.sleep(3)
        take_screenshot(username)

        update_account(username, current_task="Dismissing popups...")
        for _ in range(5):
            try:
                page.keyboard.press("Escape")
                page.evaluate("""() => {
                    document.querySelectorAll('[role="dialog"], .modal, .overlay, [class*="cookie"], [class*="consent"], [class*="age-gate"]').forEach(el => {
                        el.remove();
                    });
                }""")
            except Exception:
                pass
            time.sleep(1)

        update_account(username, current_task="Clicking Log in...")
        on_google = False
        for attempt in range(30):
            try:
                page.evaluate("""() => {
                    const btn = document.getElementById('top-right-action-bar-login-button');
                    if (btn) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return 'clicked_id';
                    }
                    const buttons = [...document.querySelectorAll('button, a, [role="button"]')];
                    const loginBtn = buttons.find(el => el.textContent.trim().toLowerCase() === 'log in');
                    if (loginBtn) {
                        loginBtn.scrollIntoView({block: 'center'});
                        loginBtn.click();
                        return 'clicked_text';
                    }
                    return 'not_found';
                }""")
                log(f"[{username}] Log in: JS click attempt {attempt+1}")
            except Exception as e:
                log(f"[{username}] Log in click err attempt {attempt+1}: {e}")
            time.sleep(2)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            if "accounts.google.com" in page.url:
                log(f"[{username}] Log in: redirected to Google on attempt {attempt+1}")
                on_google = True
                break
            if page.locator('#loginContainer, [data-e2e="login-modal"], [class*="LoginContainer"], [class*="login-modal"], [data-e2e="channel-item"]').count() > 0 or "/login" in page.url:
                log(f"[{username}] Log in: login UI detected on attempt {attempt+1}")
                break
            take_screenshot(username)
        time.sleep(3)
        take_screenshot(username)

        if not on_google:
            update_account(username, current_task="Waiting for login modal...")
            for _ in range(30):
                if page.locator('#loginContainer, [data-e2e="login-modal"], [class*="LoginContainer"], [class*="login-modal"], [data-e2e="channel-item"]').count() > 0 or "/login" in page.url or "accounts.google.com" in page.url:
                    break
                time.sleep(1)
            time.sleep(2)
            take_screenshot(username)

        update_account(username, current_task="Clicking Continue with Google...")
        for attempt in range(30):
            clicked = False
            
            try:
                btn = page.locator('#loginContainer > div.css-1jwe9yn-5b89d02d--DivLoginContainer.eb92qk53 > div > div > div > div > div:nth-child(4) > div.css-98y45w-5b89d02d--DivBoxContainer.e17788p50')
                if btn.count() > 0:
                    btn.first.click(force=True, timeout=3000)
                    time.sleep(0.3)
                    btn.first.click(force=True, timeout=3000)
                    log(f"[{username}] Continue with Google: double-click attempt {attempt+1}")
                    clicked = True
            except Exception:
                pass
            
            if not clicked:
                try:
                    btn = page.get_by_role("link", name="Continue with Google").first
                    btn.click(force=True, timeout=3000)
                    time.sleep(0.3)
                    btn.click(force=True, timeout=3000)
                    log(f"[{username}] Continue with Google: link double-click attempt {attempt+1}")
                    clicked = True
                except Exception:
                    pass
            
            if not clicked:
                try:
                    page.evaluate("""() => {
                        const btn = document.querySelector('#loginContainer > div.css-1jwe9yn-5b89d02d--DivLoginContainer.eb92qk53 > div > div > div > div > div:nth-child(4) > div.css-98y45w-5b89d02d--DivBoxContainer.e17788p50');
                        if (btn) {
                            btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                            btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                            btn.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                            setTimeout(() => {
                                btn.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                                btn.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                                btn.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                            }, 300);
                            return 'hold_clicked';
                        }
                        return 'not_found';
                    }""")
                    log(f"[{username}] Continue with Google: JS hold/click attempt {attempt+1}")
                    clicked = True
                except Exception as e:
                    log(f"[{username}] Continue with Google: hold/click failed attempt {attempt+1}: {e}")
            
            time.sleep(2)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            if "accounts.google.com" in page.url or page.locator('input[type="email"], input[name="identifier"]').count() > 0:
                log(f"[{username}] Continue with Google: success on attempt {attempt+1}")
                break
            take_screenshot(username)
        time.sleep(3)
        take_screenshot(username)
        time.sleep(3)
        take_screenshot(username)

        update_account(username, current_task="Typing Gmail...")
        try:
            email_input = page.locator('input[type="email"], input[name="identifier"], input[type="text"]').first
            email_input.fill(email_to_use, timeout=10000)
            log(f"[{username}] Google login: filled email")
        except Exception as e:
            log(f"[{username}] fill email err: {e}")
        time.sleep(2)
        _must_click(page, [
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'input[type="submit"]',
            "next", "continue"
        ], task=username)
        time.sleep(3)
        take_screenshot(username)

        update_account(username, current_task="Navigating recovery flow...")
        for i in range(15):
            forgot_ok = _must_click(page, [
                'button:has-text("Forgot password")',
                'a:has-text("Forgot password")',
                '[data-e2e="forgot-password-button"]',
                "forgot password", "forgot your password", "need help", "trouble logging in", "forgot"
            ], task=username)
            if forgot_ok:
                log(f"[{username}] Google login: clicked forgot ({i})")
                time.sleep(3)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                take_screenshot(username)

            another_ok = _must_click(page, [
                'button:has-text("Try another way")',
                'a:has-text("Try another way")',
                '[data-e2e="try-another-way-button"]',
                "try another way", "try another method", "another way", "more options", "use another account"
            ], task=username)
            if another_ok:
                log(f"[{username}] Google login: clicked try another way ({i})")
                time.sleep(3)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                take_screenshot(username)

            if re.search(r"gmail|approve|verify.*device|enter.*code|1\s*[-–]\s*99", page.content(), re.I):
                break

        update_account(username, status="Google login", current_task="Waiting for you to approve on your phone...")

        logged_in = False
        for _ in range(300):
            try:
                if page.locator('[data-e2e="profile-icon"], [data-e2e="top-nav-profile"], a[href*="/@"]').count() > 0:
                    logged_in = True
                    break
                if "/login" not in page.url and page.locator('[data-e2e="top-login-button"], a[href*="/login"]').count() == 0:
                    logged_in = True
                    break
            except Exception:
                pass
            time.sleep(5)

        if logged_in:
            cookies = context.cookies()
            update_account(username, session_data=json.dumps(cookies), connected=1,
                          status="Connected", current_task="Ready")
            log(f"[{username}] Google login SUCCESS")
            return True
        else:
            update_account(username, status="Google login failed",
                          current_task="Timed out waiting for approval")
            return False
    except Exception as e:
        import traceback
        traceback.print_exc()
        update_account(username, status="Google login error",
                      current_task=f"Error: {str(e)[:60]}")
        return False



def delete_account_session(username):
    if username in browser_sessions:
        try:
            browser_sessions[username]["context"].close()
            browser_sessions[username]["browser"].close()
            browser_sessions[username]["pw"].stop()
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
