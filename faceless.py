"""Faceless Short generator.

For accounts whose category is "Faceless" we DO NOT source a finished clip.
Instead we build a "chat reaction" Short locally:

  1. Download an ASMR background clip (no text, no sound) from TikTok/stock.
  2. Ask a local, free LLM (Ollama) for a ~45s WhatsApp/Snap-style chat
     conversation (random theme, hook at the end like "The end is crazy").
  3. Render a phone-style chat overlay (bubbles, avatars, typing) over the
     background with Pillow, frame by frame, via ffmpeg.
  4. Synthesize each speaker's lines with Piper TTS (a DIFFERENT voice per
     person) and mix them on the timeline with ffmpeg.
  5. Return a vertical 1080x1920 Shorts-ready mp4 WITH audio.

Everything degrades gracefully: if Ollama is missing we use offline templates,
if Piper is missing we render text-only (no audio) but keep the video, and if
ffmpeg is missing the function raises so the worker can retry/skip.
"""

import os
import re
import json
import time
import random
import shutil
import subprocess
import tempfile

import requests

# Reuse the TikWM search API constants from bot.py
from bot import (
    TIKWM_SEARCH_API, DOWNLOADS_DIR, YOUTUBE_SHORTS_WIDTH, YOUTUBE_SHORTS_HEIGHT,
    log,
)

# ---------------------------------------------------------------------------
# Configuration (env-overridable so the deploy can point at its own tools)
# ---------------------------------------------------------------------------
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
def _piper_bin():
    """Locate the Piper executable (env override -> /opt/piper -> PATH)."""
    env = os.environ.get("PIPER_BIN")
    if env:
        return env
    cand = ["/opt/piper/piper", "/usr/local/bin/piper", "piper"]
    for c in cand:
        if c == "piper":
            return "piper" if shutil.which("piper") else None
        if os.path.exists(c):
            return c
    return None


PIPER_BIN = _piper_bin()
PIPER_VOICE_DIR = os.environ.get("PIPER_VOICE_DIR", "/opt/piper/voices")
# Two distinct voices so the two chat people sound different.
# Prefer the downloaded onnx voices under PIPER_VOICE_DIR if present.
def _default_voice(name):
    p = os.path.join(PIPER_VOICE_DIR, f"{name}.onnx")
    return p if os.path.exists(p) else name
PIPER_VOICE_A = os.environ.get("PIPER_VOICE_A", _default_voice("en_US-ryan-high"))
PIPER_VOICE_B = os.environ.get("PIPER_VOICE_B", _default_voice("en_US-libritts_r-medium"))

WIDTH, HEIGHT = YOUTUBE_SHORTS_WIDTH, YOUTUBE_SHORTS_HEIGHT
TARGET_DURATION = 45  # seconds of chat
FONT_SIZE = 30


def check_faceless_deps():
    """Return a list of HARD-missing dependencies (ffmpeg/ffprobe are
    required for rendering; Piper TTS is optional — without it we produce a
    silent text-only chat video instead of failing)."""
    missing = []
    import shutil
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            missing.append(tool)
    return missing


# ---------------------------------------------------------------------------
# 1. ASMR background
# ---------------------------------------------------------------------------
def _make_local_background(username):
    """Fallback when TikWM/ASMR download is unreachable: generate a
    clean, SILENT, text-free animated background entirely with ffmpeg
    (smooth drifting gradient). Guarantees Faceless always has a valid
    watermark-free, sound-free clip even with no network to TikTok."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", username)
    path = os.path.join(DOWNLOADS_DIR, f"{safe}_localbg_{int(time.time())}.mp4")
    # A slowly drifting dark gradient (no audio, no text). Built from a proper
    # lavfi source (`gradients`) so it works without any external clip.
    src = (
        f"gradients=s={WIDTH}x{HEIGHT}:c0=0x12141c:c1=0x2a2f45:"
        f"c2=0x101218:c3=0x1c2333:x0=0:y0=0:"
        f"x1={WIDTH}:y1={HEIGHT}:nb_colors=4:speed=0.01:type=linear,"
        f"format=yuv420p,fps=30,loop=loop=-1:size=1"
    )
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", src,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
             "-pix_fmt", "yuv420p", "-an", "-t", str(TARGET_DURATION), path],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0:
            log(f"[{username}] local bg gen failed: {proc.stderr.decode('utf-8', 'ignore')[-500:]}")
    except Exception as e:
        log(f"[{username}] local bg gen failed: {e}")
        return None, None
    if os.path.exists(path) and os.path.getsize(path) > 10 * 1024:
        return path, "local_" + str(int(time.time()))
    # Last-resort: a static solid-color clip so generation never dies on bg.
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"color=c=0x1a1d27:s={WIDTH}x{HEIGHT}:d={TARGET_DURATION},format=yuv420p",
             "-c:v", "libx264", "-t", str(TARGET_DURATION), "-pix_fmt", "yuv420p",
             "-an", path],
            capture_output=True, timeout=120,
        )
    except Exception:
        pass
    if os.path.exists(path) and os.path.getsize(path) > 10 * 1024:
        return path, "local_" + str(int(time.time()))
    return None, None


def _source_asmr_background(username):
    """Download ASMR bg; fall back to a locally-generated silent clip
    if the TikWM/TikTok network path is unreachable."""
    bg, vid = _source_asmr_background_real(username)
    if bg:
        return bg, vid
    log(f"[{username}] ASMR download failed; using local generated background")
    return _make_local_background(username)


def _source_asmr_background_real(username):
    """Download a silent, text-free ASMR background clip. Returns (path, video_id)."""
    # Prefer text-free, watermark-light loops (satisfying/relaxing tend to be
    # cleaner than "asmr" which is full of TikTok UI/watermarks).
    keywords = ["satisfying", "relaxing", "rain sounds", "fireplace",
                "asmr", "ocean waves", "cloud", "night lights"]
    for attempt in range(3):
        try:
            r = requests.post(
                TIKWM_SEARCH_API,
                data={"keywords": random.choice(keywords),
                      "count": 20, "cursor": random.randint(0, 50), "HD": 1},
                timeout=25,
            )
            data = r.json()
            if data.get("code") != 0:
                time.sleep(1.5)
                continue
            videos = [v for v in data.get("data", {}).get("videos", [])
                      if 5 <= v.get("duration", 0) <= 120]
            if not videos:
                continue
            v = random.choice(videos)
            play = v.get("play") or v.get("hdplay") or v.get("wmplay")
            if not play:
                continue
            vid = str(v.get("video_id") or int(time.time()))
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", username)
            path = os.path.join(DOWNLOADS_DIR, f"{safe}_asmr_{vid}.mp4")
            with requests.get(play, stream=True, timeout=120,
                             headers={"User-Agent": "Mozilla/5.0"}) as resp:
                resp.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        if chunk:
                            f.write(chunk)
            if os.path.getsize(path) > 50 * 1024:
                return path, vid
            os.remove(path)
        except Exception as e:
            log(f"[{username}] ASMR source attempt {attempt} failed: {e}")
            time.sleep(2)
    return None, None


# ---------------------------------------------------------------------------
# 2. Chat script (local LLM with offline fallback)
# ---------------------------------------------------------------------------
THEMES = [
    ("crush", "two friends texting about a crush"),
    ("ghost", "two friends texting about a creepy experience"),
    ("secret", "two siblings sharing a family secret"),
    ("ex", "two friends debriefing a wild ex story"),
    ("lottery", "two friends reacting to impossible news"),
    ("night", "two friends texting late at night and something feels off"),
]

END_HOOKS = [
    "wait till the end", "the ending is insane", "you won't believe the last text",
    "the end is crazy", "don't skip the last message", "the final text broke me",
    "last text hit different", "watch till the end",
]

OFFLINE_SCRIPTS = [
    [("A", "bro you will NOT believe what just happened"),
     ("B", "what happened??"),
     ("A", "i was home alone and i heard knocking at 3am"),
     ("B", "no way"),
     ("A", "i opened the door and NO ONE was there"),
     ("B", "stop"),
     ("A", "but the camera outside caught a figure"),
     ("B", "im never sleeping again"),
     ("A", "the end is crazy")],
    [("A", "so i texted her and she left me on read for 6 hours"),
     ("B", "then what"),
     ("A", "she replied with just a pic of MY house"),
     ("B", "excuse me??"),
     ("A", "she was outside the whole time"),
     ("B", "that's actually insane"),
     ("A", "the last text broke me")],
]


def _generate_script_llm(username):
    """Ask Ollama for a 45s chat script. Returns list of (speaker, text)."""
    theme_name, theme_desc = random.choice(THEMES)
    prompt = (
        f"Write a realistic, casual phone chat between two friends, {theme_desc}. "
        f"Output EXACTLY 7 to 9 short messages alternating between A and B. "
        f"Each message must be ONE line. End with a hook line like "
        f"'{random.choice(END_HOOKS)}'. "
        f"Format STRICTLY as:\nA: message\nB: message\nA: message\n..."
        f"No extra text, no explanations."
    )
    try:
        r = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                  "options": {"temperature": 1.1, "num_predict": 400}},
            timeout=60,
        )
        r.raise_for_status()
        text = (r.json().get("response") or "").strip()
        lines = []
        for line in text.splitlines():
            m = re.match(r"^\s*([AB])\s*[:\-.]?\s*(.+)$", line)
            if m:
                speaker = "A" if m.group(1).upper() == "A" else "B"
                msg = m.group(2).strip().strip('"').strip()
                if msg:
                    lines.append((speaker, msg))
        if len(lines) >= 5:
            return lines
    except Exception as e:
        log(f"[{username}] Ollama script gen failed ({e}); using offline template")
    return random.choice(OFFLINE_SCRIPTS)


# ---------------------------------------------------------------------------
# 3. TTS (Piper) — one voice per speaker
# ---------------------------------------------------------------------------
def _synth_line_piper(text, voice, out_wav):
    """Synthesize one line with Piper. Returns True on success."""
    if not PIPER_BIN:
        return False
    try:
        proc = subprocess.run(
            [PIPER_BIN, "--model", voice, "--output_file", out_wav],
            input=text.encode("utf-8"), capture_output=True, timeout=60,
        )
        return proc.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 100
    except Exception:
        return False


def _synth_line_espeak(text, out_wav, variant="en-us"):
    """Synthesize one line with espeak-ng (free, offline). Returns True on success.

    espeak-ng writes raw or wav output; we ask for wav and resample to 24k mono
    so it matches the rest of the pipeline."""
    bin_cands = ["espeak-ng", "espeak"]
    bin_name = next((b for b in bin_cands if shutil.which(b)), None)
    if not bin_name:
        return False
    try:
        raw = os.path.join(os.path.dirname(out_wav), "_espeak_raw.wav")
        proc = subprocess.run(
            [bin_name, "-v", variant, "-s", "155", "-w", raw, text],
            capture_output=True, timeout=60,
        )
        if proc.returncode != 0 or not os.path.exists(raw) or os.path.getsize(raw) <= 100:
            return False
        # Normalize to 24k mono wav for consistent downstream handling.
        rc = subprocess.run(
            ["ffmpeg", "-y", "-i", raw, "-ar", "24000", "-ac", "1", out_wav],
            capture_output=True, timeout=30,
        )
        return rc.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 100
    except Exception:
        return False


def _synth_line_gtts(text, out_wav, lang="en"):
    """Synthesize one line with gTTS (free, online, needs network)."""
    try:
        from gtts import gTTS
    except Exception:
        return False
    try:
        mp3 = os.path.join(os.path.dirname(out_wav), "_gtts.mp3")
        gTTS(text=text, lang=lang, slow=False).save(mp3)
        if not os.path.exists(mp3) or os.path.getsize(mp3) <= 100:
            return False
        rc = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3, "-ar", "24000", "-ac", "1", out_wav],
            capture_output=True, timeout=30,
        )
        return rc.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 100
    except Exception:
        return False


def _synth_line(text, voice, out_wav):
    """Synthesize one line. Tries Piper, then espeak-ng, then gTTS.

    `voice` is only used by Piper. Returns (True, backend) on success or
    (False, None) on failure."""
    if _synth_line_piper(text, voice, out_wav):
        return True, "piper"
    if _synth_line_espeak(text, out_wav):
        return True, "espeak"
    if _synth_line_gtts(text, out_wav):
        return True, "gtts"
    return False, None


def _wav_duration(wav):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", wav],
            capture_output=True, timeout=20,
        )
        return float(out.stdout.strip() or 1.0)
    except Exception:
        return 1.5


def _make_silence(path, seconds):
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             f"anullsrc=r=24000:cl=mono:d={seconds:.2f}", "-t", f"{seconds:.2f}", path],
            capture_output=True, timeout=30,
        )
    except Exception:
        pass


def _build_audio(username, script, tmp):
    """Synthesize each line with a different Piper voice, concat with gaps.

    Returns (final_wav, segments) where segments is a list of
    (speaker, wav_path, dur_sec). If Piper is unavailable, returns (None, []).
    """
    if not PIPER_BIN:
        log(f"[{username}] Faceless: Piper TTS not installed -> trying free fallback TTS (espeak-ng / gTTS).")
    missing_voices = [v for v in (PIPER_VOICE_A, PIPER_VOICE_B) if not os.path.exists(v)]
    if missing_voices:
        log(f"[{username}] Faceless: Piper voice file(s) missing: {missing_voices} "
            f"-> trying free fallback TTS (espeak-ng / gTTS).")
    segs = []
    ok = True
    backend_used = None
    for i, (speaker, text) in enumerate(script):
        voice = PIPER_VOICE_A if speaker == "A" else PIPER_VOICE_B
        wav = os.path.join(tmp, f"line_{i}.wav")
        success, backend = _synth_line(text, voice, wav)
        if success:
            backend_used = backend_used or backend
        if not success:
            ok = False
            break
        dur = _wav_duration(wav)
        segs.append((speaker, wav, dur))
    if not ok or not segs:
        log(f"[{username}] Faceless: no TTS backend available (piper/espeak/gTTS) "
            f"-> silent video (voices skipped).")
        return None, []

    gap = 0.4
    total_speech = sum(s[2] for s in segs)
    available = TARGET_DURATION - total_speech
    per_gap = max(gap, (available / max(1, len(segs) - 1))) if available > 0 else gap

    silence_wav = os.path.join(tmp, "sil.wav")
    _make_silence(silence_wav, per_gap)
    start = 0.0
    segments = []
    final = os.path.join(tmp, "final_audio.wav")
    concat_list = os.path.join(tmp, "concat.txt")
    with open(concat_list, "w") as f:
        for idx, (speaker, wav, dur) in enumerate(segs):
            f.write(f"file '{os.path.basename(wav)}'\n")
            if idx < len(segs) - 1:
                f.write(f"file '{os.path.basename(silence_wav)}'\n")
            segments.append((speaker, wav, round(start, 2), dur))
            start += dur + per_gap
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
         "-c", "copy", final],
        capture_output=True, cwd=tmp, timeout=60,
    )
    if os.path.exists(final) and os.path.getsize(final) > 100:
        return final, segments
    return None, []


# ---------------------------------------------------------------------------
# 4. Chat overlay render (Pillow) + ffmpeg composite
# ---------------------------------------------------------------------------
def _draw_chat_frame(path, messages, font, small, last_speaker):
    """Draw a centered, phone-style chat (Snapchat/WhatsApp look).

    A centered dark "phone" panel holds a status bar at top, the message
    bubbles stacked from the top (A left / B right with small avatars),
    and a typing indicator pinned near the bottom when the last speaker is
    still "typing". No edge/corner bubbles.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Centered phone panel (slightly inset so ASMR edges/watermarks hide).
    pad_x = int(WIDTH * 0.04)
    pad_top = int(HEIGHT * 0.05)
    pad_bot = int(HEIGHT * 0.06)
    px0, py0, px1, py1 = pad_x, pad_top, WIDTH - pad_x, HEIGHT - pad_bot
    d.rounded_rectangle([px0, py0, px1, py1], radius=40, fill=(18, 19, 26, 235))

    # Status bar
    bar_h = 96
    d.rounded_rectangle([px0, py0, px1, py0 + bar_h], radius=40, fill=(26, 27, 36, 255))
    d.rectangle([px0, py0 + bar_h - 30, px1, py0 + bar_h], fill=(26, 27, 36, 255))
    # avatar circle
    ax, ay = px0 + 54, py0 + bar_h // 2
    d.ellipse([ax - 30, ay - 30, ax + 30, ay + 30], fill=(58, 110, 240, 255))
    d.text((ax - 12, ay - 16), "A", fill=(255, 255, 255, 255), font=small)
    d.text((ax + 48, py0 + 28), "Alex", fill=(240, 240, 240, 255), font=small)
    d.text((ax + 48, py0 + 56), "online", fill=(130, 200, 130, 255), font=small)

    # Messages area
    area_top = py0 + bar_h + 24
    area_bot = py1 - 120  # leave room for typing indicator
    max_bubble_w = int((px1 - px0) * 0.74)
    y = area_top
    for (speaker, text) in messages:
        is_me = (speaker == "B")
        bubble_col = (20, 122, 240, 255) if is_me else (46, 48, 56, 255)
        lines = _wrap(text, font, max_bubble_w - 40)
        lh = FONT_SIZE + 8
        bh = 22 + len(lines) * lh
        bw = min(max_bubble_w, max((d.textlength(l, font=font) for l in lines)) + 40)
        if is_me:
            bx = px1 - 26 - bw
        else:
            bx = px0 + 26
        if y + bh > area_bot:
            break
        d.rounded_rectangle([bx, y, bx + bw, y + bh], radius=20, fill=bubble_col)
        ty = y + 14
        for l in lines:
            d.text((bx + 20, ty), l, fill=(255, 255, 255, 255), font=font)
            ty += lh
        y += bh + 16

    # Typing indicator near bottom
    if last_speaker:
        is_me = (last_speaker == "B")
        tw, th = 120, 52
        tx = (px1 - 26 - tw) if is_me else (px0 + 26)
        ty2 = py1 - 92
        d.rounded_rectangle([tx, ty2, tx + tw, ty2 + th], radius=20, fill=(46, 48, 56, 255))
        for dot in range(3):
            cx = tx + 28 + dot * 26
            d.ellipse([cx, ty2 + 18, cx + 14, ty2 + 32], fill=(170, 172, 180, 255))

    img.convert("RGB").save(path)


def _wrap(text, font, max_w, draw=None):
    from PIL import Image, ImageDraw
    if draw is None:
        draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    words = text.split()
    lines, cur = [], ""
    for w in words:
        test = (cur + " " + w).strip()
        if draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [text]


def _ffmpeg_ok(proc, what, username):
    """Return True if the ffmpeg run succeeded, else log why."""
    if proc.returncode == 0 and (proc.stdout or proc.stderr or True):
        return True
    err = (proc.stderr or proc.stdout or b"").decode("utf-8", "ignore")[-600:]
    log(f"[{username}] Faceless ffmpeg FAILED ({what}): {err}")
    return False


def _has_filter(filter_name):
    try:
        out = subprocess.run(["ffmpeg", "-filters"], capture_output=True, timeout=20)
        return filter_name in out.stdout.decode("utf-8", "ignore")
    except Exception:
        return True  # assume present if we can't check


def _render_chat_video(username, bg_path, script, audio_segments, out_path, tmp):
    """Render the chat overlay over a CLEAN, muted, watermark-hidden ASMR bg.

    - Mute the ASMR audio (ASMR backgrounds must be silent).
    - Fit to 9:16 WITHOUT cropping (pad), so nothing is butchered.
    - Blur+darken the background edges so any TikTok @/watermark UI
      that rides the clip borders is hidden under the phone panel.
    - High quality (crf 18, medium preset).

    Every ffmpeg step is return-code checked and logs its stderr on failure
    so a broken filter/build can never silently produce "no file".
    """
    from PIL import Image, ImageDraw, ImageFont

    # 1) Clean, muted, padded 9:16 background (no crop, no quality loss).
    #    Only apply gblur if the filter actually exists in this ffmpeg build;
    #    otherwise just darken (a missing filter would abort the whole step).
    blur = "gblur=28:30," if _has_filter("gblur") else ""
    bg_tmp = os.path.join(tmp, "bg_clean.mp4")
    p1 = subprocess.run(
        ["ffmpeg", "-y", "-i", bg_path, "-an",
         "-vf", (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                   f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
                   f"{blur}eq=brightness=0.55,format=yuv420p"),
         "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-t", str(TARGET_DURATION), bg_tmp],
        capture_output=True, timeout=120,
    )
    if not _ffmpeg_ok(p1, "background clean", username) or not os.path.exists(bg_tmp):
        return None

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", FONT_SIZE)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
        small = font

    # 2) Build per-message frames; durations come straight from the
    #    synthesized audio segments (index-aligned to script).
    frames = []
    t = 0.0
    for i, (speaker, text) in enumerate(script):
        png = os.path.join(tmp, f"frame_{i}.png")
        _draw_chat_frame(png, script[:i + 1], font, small, speaker)
        dur = 2.2
        if i < len(audio_segments):
            dur = audio_segments[i][3] or 2.2
        frames.append((round(t, 2), png, dur))
        t += dur

    overlay_concat = os.path.join(tmp, "overlay.txt")
    with open(overlay_concat, "w") as f:
        for start, png, dur in frames:
            f.write(f"file '{os.path.basename(png)}'\nduration {dur:.2f}\n")
    overlay_vid = os.path.join(tmp, "overlay.mp4")
    p2 = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", overlay_concat,
         "-vf", f"scale={WIDTH}:{HEIGHT},format=rgba",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(TARGET_DURATION), overlay_vid],
        capture_output=True, cwd=tmp, timeout=60,
    )
    if not _ffmpeg_ok(p2, "overlay build", username) or not os.path.exists(overlay_vid):
        return None

    have_audio = bool(audio_segments) and any(
        os.path.exists(s[1]) for s in audio_segments)
    if have_audio:
        final_audio = os.path.join(tmp, "final_audio.wav")
        cmd = ["ffmpeg", "-y", "-i", bg_tmp, "-i", overlay_vid, "-i", final_audio,
               "-filter_complex",
               "[1:v]format=rgba,colorchannelmixer=aa=0.95[ov];[0:v][ov]overlay=0:0:format=auto[v]",
               "-map", "[v]", "-map", "2:a",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-t", str(TARGET_DURATION), "-shortest", out_path]
    else:
        # No TTS audio: keep the silent chat video (ASMR is muted anyway).
        cmd = ["ffmpeg", "-y", "-i", bg_tmp, "-i", overlay_vid,
               "-filter_complex",
               "[1:v]format=rgba,colorchannelmixer=aa=0.95[ov];[0:v][ov]overlay=0:0:format=auto",
               "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p",
               "-t", str(TARGET_DURATION), out_path]
    p3 = subprocess.run(cmd, capture_output=True, timeout=180)
    if not _ffmpeg_ok(p3, "final composite", username):
        return None
    return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 10 * 1024 else None


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------
def generate_faceless_short(username):
    """Full pipeline. Returns (video_path, title, caption) or (None, None, None).
    Logs a LOUD, specific reason on every failure mode so 'Faceless gen
    failed' is never a mystery."""
    missing = check_faceless_deps()
    if missing:
        msg = "Faceless deps MISSING: " + ", ".join(missing) + \
              " - install ffmpeg/ffprobe (piper/espeak/gTTS optional, Ollama optional)."
        log(f"[{username}] {msg}")
        try:
            update_account(username, current_task=msg[:200])
        except Exception:
            pass
        return None, None, None
    tmp = tempfile.mkdtemp(prefix=f"faceless_{re.sub(r'[^A-Za-z0-9]', '_', username)}_")
    try:
        bg, vid = _source_asmr_background(username)
        if not bg:
            log(f"[{username}] Faceless: NO background available (TikWM + local gen both failed) -> cannot render")
            return None, None, None
        log(f"[{username}] Faceless: background ready ({vid})")

        script = _generate_script_llm(username)
        log(f"[{username}] Faceless: script = {script}")
        audio_final, segments = _build_audio(username, script, tmp)
        log(f"[{username}] Faceless: audio segments = {len(segments)} (None => silent)")

        out = os.path.join(DOWNLOADS_DIR, f"{re.sub(r'[^A-Za-z0-9_-]', '_', username)}_faceless_{int(time.time())}.mp4")
        final = _render_chat_video(username, bg, script, segments, out, tmp)
        if not final:
            log(f"[{username}] Faceless: render produced no file -> gen failed")
            return None, None, None
        log(f"[{username}] Faceless: generated {final} ({os.path.getsize(final)} bytes)")

        title = random.choice([
            "POV: the texts got weird", "the last message broke me",
            "this convo is unforgettable", "wait till you see the end",
        ])
        caption = f"{random.choice(END_HOOKS)} " + " ".join(["#shorts", "#asmr", "#chat", "#fyp", "#viral"])
        return final, title, caption
    except Exception as e:
        log(f"[{username}] Faceless generation error: {e}")
        return None, None, None
    finally:
        try:
            for f in os.listdir(tmp):
                try:
                    os.remove(os.path.join(tmp, f))
                except Exception:
                    pass
            os.rmdir(tmp)
        except Exception:
            pass
