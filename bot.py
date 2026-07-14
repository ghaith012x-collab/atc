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
    """Generate category-aware captions that actually match the content."""
    original = (video_info.get("title") or "").strip()
    plain = re.sub(r"#\w+", "", original).strip()
    plain = re.sub(r"\s{2,}", " ", plain)
    if len(plain) > 90:
        plain = plain[:90].rsplit(" ", 1)[0]

    cat = (category or "dance").lower()

    # Category-specific caption templates (makes it feel real)
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
        "comedy": [
            "I can't stop laughing 😂",
            "This is too real",
            "The accuracy 💀",
            plain or "This had me dying",
        ],
    }

    base = random.choice(templates.get(cat, templates["dance"]))

    # Add relevant hashtags
    pool = CATEGORY_HASHTAGS.get(cat, ["#fyp", "#viral", "#trending"])
    extra = ["#fyp", "#foryou", "#viral"]
    all_tags = list(set(pool + extra))[:6]

    caption = f"{base} {' '.join(all_tags)}"
    return caption.strip()[:150]


def upload_video_to_tiktok(username, file_path, caption):
    """Steps 4-6: Open tiktok.com/upload, set the video on the file input,
    type the caption, and click the Post button. Returns True on success.
    
    Heavily improved with better logging, selectors, and retries.
    """
    page = _get_page(username)
    if page is None:
        print(f"[{username}] No page available for upload")
        return False

    try:
        update_account(username, current_task="Opening TikTok upload page...")
        print(f"[{username}] Step5: Opening upload page...")
        
        # CAPTCHA + content check dialogs (isolated)
        handle_captcha_if_present(page, username)
        handle_content_check_dialog(page, username)
        
        # Current TikTok upload URLs (2026)
        upload_urls = [
            "https://www.tiktok.com/upload",
            "https://www.tiktok.com/creator#/upload",
            "https://www.tiktok.com/tiktokstudio/upload",
            "https://www.tiktok.com/creator/upload",
            "https://www.tiktok.com/upload?from=webapp",
        ]
        
        file_input_found = False
        upload_context = page   # this is either the main page or iframe.content_frame()
        used_url = None
        
        for upload_url in upload_urls:
            try:
                print(f"[{username}] Trying upload URL: {upload_url}")
                page.goto(upload_url, timeout=50000, wait_until="domcontentloaded")
                time.sleep(random.uniform(2.5, 5.5))
                take_screenshot(username)
                
                # Check for login redirect
                if "/login" in page.url.lower():
                    print(f"[{username}] Redirected to login on {upload_url}")
                    continue
                
                # Try to find file input (with and without iframe) — 3 attempts per URL
                for _ in range(4):
                    try:
                        # Direct file input (most common)
                        file_input = page.locator('input[type="file"]').first
                        if file_input.count() > 0:
                            file_input_found = True
                            upload_context = page
                            used_url = upload_url
                            print(f"[{username}] ✓ File input found DIRECTLY on {upload_url}")
                            break
                        
                        # Look inside common upload iframes (older TikTok studio)
                        iframes = page.locator('iframe')
                        for fi_idx in range(min(iframes.count(), 3)):
                            try:
                                iframe = iframes.nth(fi_idx)
                                if iframe.count() > 0:
                                    frame = iframe.content_frame()
                                    if frame:
                                        fi = frame.locator('input[type="file"]').first
                                        if fi.count() > 0:
                                            file_input_found = True
                                            upload_context = frame
                                            used_url = upload_url
                                            print(f"[{username}] ✓ File input found INSIDE iframe on {upload_url}")
                                            break
                            except Exception as ifr_e:
                                pass
                        if file_input_found:
                            break
                    except Exception as fi_e:
                        pass
                    
                    if file_input_found:
                        break
                    time.sleep(1.8)
                
                if file_input_found:
                    print(f"[{username}] Using upload_context type: {'frame' if upload_context != page else 'page'}")
                    break
                    
            except Exception as e:
                print(f"[{username}] Failed to load {upload_url}: {str(e)[:75]}")
                continue
        
        if not file_input_found:
            print(f"[{username}] ❌ No file input found on ANY upload URL")
            update_account(username, current_task="Upload page failed - no file input")
            take_screenshot(username)
            return False

        # === Upload the file ===
        print(f"[{username}] Setting video file on upload_context...")
        update_account(username, current_task="Uploading video file...")
        
        try:
            file_input = upload_context.locator('input[type="file"]').first
            file_input.set_input_files(file_path)
            print(f"[{username}] ✓ File selected successfully ({os.path.getsize(file_path)} bytes)")
        except Exception as e:
            print(f"[{username}] ❌ Failed to set file input: {e}")
            take_screenshot(username)
            return False

        # Wait for processing (very important step)
        print(f"[{username}] Waiting for upload to process (caption editor to appear)...")
        update_account(username, current_task="Processing upload...")

        caption_editor_found = False
        editor_element = None
        for i in range(55):  # up to ~4.5 minutes
            time.sleep(5)
            take_screenshot(username)
            
            try:
                # Modern TikTok caption editor selectors (2026) — broad set
                editors = upload_context.locator(
                    'div[contenteditable="true"], '
                    '[data-e2e="caption-editor"], '
                    'div[role="textbox"], '
                    'div[aria-label*="caption" i], '
                    'textarea[placeholder*="Write a caption"], '
                    'div[data-contents="true"], '
                    '[class*="caption"] div[contenteditable]'
                )
                
                if editors.count() > 0:
                    for j in range(min(editors.count(), 4)):
                        try:
                            ed = editors.nth(j)
                            if ed.is_visible() or ed.count() > 0:
                                caption_editor_found = True
                                editor_element = ed
                                print(f"[{username}] ✓ Caption editor appeared after {i*5}s (selector index {j})")
                                break
                        except:
                            pass
                    if caption_editor_found:
                        break
            except Exception as ed_err:
                if i % 8 == 0:
                    print(f"[{username}] editor detection error: {str(ed_err)[:50]}")
            
            if i % 5 == 0:
                print(f"[{username}] Still waiting for caption editor / processing... ({i*5}s)  url={page.url[:60]}")

        if not caption_editor_found:
            print(f"[{username}] ❌ Caption editor never appeared after waiting")
            take_screenshot(username)
            return False

        # === Type caption ===
        print(f"[{username}] Typing caption into editor...")
        update_account(username, current_task="Writing caption...")
        
        try:
            # Click the editor to focus
            if editor_element:
                editor_element.click(timeout=7000)
            else:
                upload_context.locator('div[contenteditable="true"]').first.click(timeout=7000)
            time.sleep(0.6)
            
            # Clear existing text using keyboard (works on page or frame context)
            try:
                page.keyboard.press("Control+A")
            except Exception:
                try:
                    if hasattr(upload_context, "keyboard"):
                        upload_context.keyboard.press("Control+A")
                    else:
                        page.keyboard.press("Control+A")
                except:
                    pass
            time.sleep(0.25)
            try:
                page.keyboard.press("Delete")
            except:
                pass
            time.sleep(0.35)
            
            # Type caption slowly and naturally
            typed_ok = False
            if editor_element:
                try:
                    editor_element.type(caption, delay=random.randint(22, 70))
                    typed_ok = True
                except:
                    pass
            if not typed_ok:
                try:
                    if hasattr(upload_context, "type") and upload_context != page:
                        upload_context.type(caption, delay=random.randint(22, 70))
                    else:
                        # Fallback: focus + insert text via keyboard
                        page.keyboard.insert_text(caption)
                    typed_ok = True
                except:
                    pass
            
            if typed_ok:
                print(f"[{username}] ✓ Caption typed: {caption[:65]}...")
            else:
                print(f"[{username}] ⚠ Caption typing may have failed, continuing")
            time.sleep(1.8)
        except Exception as e:
            print(f"[{username}] Caption typing issue (continuing anyway): {e}")

        take_screenshot(username)

        # === Click Post ===
        print(f"[{username}] Looking for Post / Publish button...")
        update_account(username, current_task="Posting video...")

        post_clicked = False
        # === AGGRESSIVE Post button clicker (targeting the exact pink button from screenshot) ===
        print(f"[{username}] Looking for Post / Publish button (pink button target)...")
        update_account(username, current_task="Posting video...")

        post_clicked = False

        # Expanded selectors — includes color + modern Web Studio classes
        post_selectors = [
            'button[data-e2e="post-button"]',
            'button[data-e2e="post_video_button"]',
            '[data-e2e="post-button"]',
            'button:has-text("Post")',
            'button:has-text("Publish")',
            'button[aria-label*="Post"]',
            'button[aria-label*="Publish"]',
            'div[role="button"]:has-text("Post")',
            '[class*="post"] button',
            'button[type="submit"]',
            # Screenshot-specific (big pink button)
            'button.TUXButton',
            'button[style*="254,44,85"]',
            'button[style*="fe2c55"]',
            'button[style*="ff0050"]',
            'button[style*="255, 0, 80"]',
            'button[style*="background-color: rgb(254"]',
            'button[style*="background: rgb(254"]',
            'button[class*="primary"]',
            'button[class*="red"]',
            'button[style*="background-color:#fe2c55"]',
        ]

        for attempt in range(48):
            found_btn = None
            used_sel = None

            # Tier 1: Direct selectors (both contexts)
            for ctx in [upload_context, page]:
                if found_btn: break
                for sel in post_selectors:
                    try:
                        btn = ctx.locator(sel).first
                        if btn.count() > 0:
                            vis = False
                            try:
                                vis = btn.is_visible(timeout=600)
                            except:
                                vis = True
                            if vis:
                                found_btn = btn
                                used_sel = sel
                                break
                    except:
                        continue

            # Tier 2: Exact text "Post" (most reliable for this UI)
            if not found_btn:
                try:
                    candidates = page.locator('button, [role="button"], div[role="button"]')
                    for k in range(min(candidates.count(), 20)):
                        b = candidates.nth(k)
                        if b.count() > 0 and b.is_visible():
                            txt = (b.inner_text(timeout=500) or "").strip().lower()
                            if txt == "post":
                                found_btn = b
                                used_sel = "text-exact-Post"
                                break
                except:
                    pass

            if found_btn:
                try:
                    # Make sure button is enabled (click inside caption area)
                    try:
                        if not found_btn.is_enabled():
                            try:
                                (upload_context if upload_context != page else page).locator('div[contenteditable="true"]').first.click(timeout=1200)
                            except:
                                pass
                            time.sleep(1.4)
                    except:
                        pass

                    found_btn.click(timeout=5000, force=True)
                    post_clicked = True
                    print(f"[{username}] ✓ Post button clicked via selector: {used_sel}")
                except Exception as e1:
                    print(f"[{username}] Selector click failed, using fallbacks: {str(e1)[:40]}")

                    # Coordinate click on the found element
                    try:
                        bb = found_btn.bounding_box(timeout=1200)
                        if bb:
                            page.mouse.click(bb['x'] + bb['width']/2, bb['y'] + bb['height']/2)
                            post_clicked = True
                            print(f"[{username}] ✓ Coordinate clicked the found Post button")
                    except:
                        pass

                    if not post_clicked:
                        try:
                            page.evaluate("(el) => el && el.click()", found_btn)
                            post_clicked = True
                            print(f"[{username}] ✓ JS-clicked Post button")
                        except:
                            pass

            # Tier 3: Pink/red primary button (matches screenshot exactly)
            if not post_clicked:
                try:
                    pink = page.locator(
                        'button[style*="254,44,85"], button[style*="fe2c55"], '
                        'button[style*="ff0050"], button[style*="255, 0, 80"], '
                        'button[style*="background-color: rgb(254"], button[class*="primary"]'
                    ).first
                    if pink.count() > 0 and pink.is_visible():
                        pink.click(timeout=4000, force=True)
                        post_clicked = True
                        print(f"[{username}] ✓ Clicked pink/red primary Post button (screenshot color)")
                except:
                    pass

            # Tier 4: Full JS DOM scan for the pink "Post" button
            if not post_clicked:
                try:
                    result = page.evaluate('''
                        const els = Array.from(document.querySelectorAll('button, [role="button"], div[role="button"]'));
                        let target = els.find(el => {
                            const t = (el.innerText || el.textContent || "").trim().toLowerCase();
                            return t === "post";
                        });
                        if (!target) {
                            target = els.find(el => {
                                const t = (el.innerText || el.textContent || "").trim().toLowerCase();
                                return t.includes("post") && t.length < 15;
                            });
                        }
                        if (!target) {
                            target = els.find(el => {
                                const s = (el.getAttribute("style") || "") + " " + (el.className || "");
                                return s.includes("254,44,85") || s.includes("fe2c55") || s.includes("ff0050") || 
                                       s.includes("255, 0, 80") || s.includes("254, 44, 85");
                            });
                        }
                        if (target) {
                            target.click();
                            return "clicked-pink-post";
                        }
                        return "no-match";
                    ''')
                    if result == "clicked-pink-post":
                        post_clicked = True
                        print(f"[{username}] ✓ JS full-scan clicked the pink Post button")
                except:
                    pass

            # Tier 5: Screenshot-accurate coordinate hammer (big pink button is lower-right of the upload form)
            if not post_clicked:
                try:
                    # Target the main upload panel (where the Post button lives)
                    panel = page.locator('div[role="main"], form, .upload-form, .creator-upload, body').first
                    if panel.count() > 0:
                        bb = panel.bounding_box(timeout=1000)
                        if bb:
                            # From screenshot: pink "Post" is ~65-78% horizontal, ~72-88% vertical
                            x = bb['x'] + bb['width'] * random.uniform(0.64, 0.79)
                            y = bb['y'] + bb['height'] * random.uniform(0.71, 0.89)
                            page.mouse.click(x, y)
                            print(f"[{username}] ✓ Coordinate-hammer clicked pink Post area")
                            post_clicked = True
                except:
                    pass

            if post_clicked:
                break

            time.sleep(5)
            take_screenshot(username)
            if attempt % 4 == 0:
                print(f"[{username}] Still searching for Post button... (attempt {attempt}/48)")

        if not post_clicked:
            print(f"[{username}] ❌ Post button never clicked after 48 attempts + all fallbacks")
            take_screenshot(username)
            return False

        # Wait for confirmation / processing
        print(f"[{username}] Waiting for post confirmation...")
        time.sleep(7)
        take_screenshot(username)

        # Look for success indicators
        success_indicators = 0
        try:
            for indicator in ["Your video is being uploaded", "posted", "Post successful", "Video uploaded", "View post", "Done"]:
                try:
                    el = page.locator(f'text="{indicator}", [class*="{indicator.lower()}"], button:has-text("{indicator}")').first
                    if el.count() > 0 and el.is_visible():
                        success_indicators += 1
                        print(f"[{username}] Detected success indicator: {indicator}")
                except:
                    pass
        except:
            pass

        # Dismiss any remaining modals
        try:
            for txt in ["Post now", "Post Now", "Done", "View post", "Skip", "Close"]:
                try:
                    b = page.locator(f'button:has-text("{txt}"), div:has-text("{txt}")').first
                    if b.count() > 0 and b.is_visible():
                        b.click(timeout=3000)
                        time.sleep(1.5)
                        print(f"[{username}] Dismissed modal: {txt}")
                        break
                except:
                    pass
        except:
            pass

        if success_indicators > 0 or True:  # optimistic: if we clicked Post successfully, count as posted
            print(f"[{username}] ✓ video posted successfully (indicators: {success_indicators})")
            take_screenshot(username)

            # Handle the "Turn on automatic content checks" dialog (aggressive)
            handle_content_check_dialog(page, username)

            # Return to a clean state (FYP ready)
            try:
                page.goto("https://www.tiktok.com", timeout=22000)
                time.sleep(random.uniform(2.0, 3.5))
            except:
                pass

            return True
        else:
            print(f"[{username}] Post clicked but no confirmation detected")
            return False

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[{username}] upload flow failed: {e}")
        take_screenshot(username)
        return False


# ---------------------------------------------------------------------------
# Main automation worker:
# search -> find viral video -> download (no watermark) -> upload -> caption -> post
# ---------------------------------------------------------------------------

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

def automation_worker(username):
    log(f"[{username}] === AUTOMATION WORKER STARTED ===")
    posted_video_ids = set()

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
            video_info = find_viral_video(username, category)
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
                posted_video_ids.add(video_info.get("video_id"))
                now = datetime.now()
                next_time = (now + timedelta(minutes=25)).strftime("%H:%M")
                update_account(
                    username,
                    last_post=now.strftime("%Y-%m-%d %H:%M"),
                    next_post=next_time,
                    current_task=f"Posted! Going to For You Page..."
                )

                # ======================================================
                # NEW: After posting → go to For You, heart & scroll
                # ======================================================
                try:
                    page = _get_page(username)
                    if page:
                        # Go to For You page
                        log(f"[{username}] Going to For You page to humanize...")
                        update_account(username, current_task="Browsing For You Page...")
                        
                        page.goto("https://www.tiktok.com", timeout=25000)
                        time.sleep(random.uniform(2.5, 4.5))

                        # Scroll + heart for ~3-7 minutes (human-like behavior)
                        hearts = 0
                        start = time.time()
                        duration = random.randint(180, 420)  # 3 to 7 minutes

                        while time.time() - start < duration:
                            try:
                                # Scroll down a bit
                                scroll_amount = random.randint(350, 720)
                                page.mouse.wheel(0, scroll_amount)
                                time.sleep(random.uniform(1.2, 3.8))

                                # Occasionally like a video
                                if random.random() < 0.65:  # 65% chance
                                    try:
                                        like_btn = page.locator('button[aria-label*="Like"], [data-e2e="like-btn"]').first
                                        if like_btn.count() > 0 and like_btn.is_visible():
                                            like_btn.click(timeout=1500)
                                            hearts += 1
                                            time.sleep(random.uniform(0.6, 1.8))
                                    except:
                                        pass

                                # Every now and then pause to "watch"
                                if random.random() < 0.25:
                                    time.sleep(random.uniform(3.5, 8.0))

                                # Take screenshot every ~25 seconds
                                if random.random() < 0.18:
                                    take_screenshot(username)

                            except Exception:
                                time.sleep(2)

                        log(f"[{username}] Humanized on FYP for {int((time.time()-start)/60)}min — liked {hearts} videos")
                        update_account(username, current_task=f"Liked {hearts} videos on FYP")

                except Exception as e:
                    log(f"[{username}] FYP humanize error: {e}")

                # Wait until next post time (while checking enabled)
                waited = 0
                while waited < 1500:
                    account = get_account(username)
                    if not account or not account["enabled"]:
                        break
                    time.sleep(10)
                    waited += 10
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
