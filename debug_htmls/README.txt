TikTok Upload Debug HTML Dumps (FULL PAGE HTML)
=================================================

This folder gets **complete page HTML** (page.content()) saved on:
- PRE-POST (right before starting the 6 attempts)
- Every ATTEMPT-1 .. ATTEMPT-6
- POST-CLICKED-X (if we managed to click)
- FINAL-FAIL

Files created automatically at runtime:
- tiktok_upload_<username>_<note>_<unix-ts>.html
- LATEST_<username>.html   ← always the latest full dump (easiest to inspect)

To retrieve when user asks "get the dom" / "do i get the dom":
  ls /home/user/debug_htmls/
  cat /home/user/debug_htmls/LATEST_*.html
  (or the specific timestamped file)

What is now logged on every attempt:
- EXACT post_video_button state:
    aria-disabled=...
    data-disabled=...
    data-loading=...
    text=...
    full outerHTML
- All fallback buttons (Post / Publiser / primary classes)
- Complete HTML saved

User's real button (Norwegian account):
<button ... data-e2e="post_video_button"
        aria-disabled="false"
        data-disabled="false"
        data-loading="false"
        class="... Button__root--type-primary ..."
        style="width: 200px;">
    ...
    <div class="Button__content ...">Publiser</div>
</button>

Changes made to fix clicking:
- Checks ALL three disable flags (aria + data-disabled + data-loading)
- Explicit 60s wait for button to become enabled before the attempt loop
- Supports "Post" AND "Publiser"
- Targets exact class pattern Button__root--type-primary + data-e2e
- Aggressive clicking: force=True + full JS mouse events (mousedown/up/click + PointerEvent) + coordinate hammer
- Full DOM dump on every single attempt (not just every 2nd)

Run a post attempt now → new LATEST_*.html files will appear here.
Then share the content of LATEST_*.html or the latest timestamped file for further debugging.
