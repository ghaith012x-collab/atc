TikTok Post Debug Directory (ENHANCED)

Runtime files created on real runs:
- LATEST_*.html (full page.content() at critical moments)
- timestamped HTML dumps
- Screenshots via take_screenshot() (before click / right after / +5s)

Current implementation (upload_video_to_tiktok) - UPDATED VERIFIED VERSION:
- MAIN: Accurate 100% wait loops (progress gone + EXACT attrs: aria-disabled=false + data-disabled=false + data-loading=false + visible)
- Uses EXACT button from pasted HTML (data-e2e + Button__root--type-primary + inner .Button__content "Publiser")
- Full pre/post debug:
  * URL + upload_url_used
  * outerHTML verbatim
  * visible/enabled/count
  * ALL 3 flags (data-disabled/aria/data-loading)
  * elementFromPoint + overlap/pointer-events detection
  * frame/iframe verification
- Network capture (REQ/RESP for publish/upload)
- 4 click strategies (logged, no silent):
  1. scroll + force click
  2. inner div.Button__content (exact)
  3. JS pointerdown/mousedown/click dispatch
  4. coordinate fallback
- Post-click: screenshots (immediate +5s), HTML dumps, evidence (URL/nav, toast text, button gone, network, body keywords)
- Explicit _save_debug_html + _save_debug_screenshot at PRE_POST / POST_CLICKED / FAIL / TIMEOUT etc. (LATEST_*.html + timestamped)
- NO silent try/except in post path (full tracebacks)
- Timeouts: 5min upload + 3min final button
- Verified selectors from wkaisertexas + user paste integrated (creator-center, post_video_button, resolution, etc.)
- Single attempt + evidence check only (no blind retries)

To debug the real cause:
1. Run a post attempt.
2. Copy the block starting from:
   "=== WAITING FOR UPLOAD TO HIT 100% (accurate) ==="
   through the "POST-CLICK EVIDENCE CHECK"
3. Check the latest screenshot and LATEST_*.html

Typical root causes this will expose:
- elementFromPoint hits an overlay (pointer-events, z-index)
- Button still has data-loading="true" at click time
- Page inside hidden iframe
- No network publish call fired (JS handler not attached)
- TikTok A/B or region (Norwegian "Publiser") state not fully ready

The code is now instrumented to tell us exactly why the click is ignored.
