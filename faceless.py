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
PIPER_BIN = os.environ.get("PIPER_BIN", "/opt/piper/piper")
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
def _source_asmr_background(username):
    """Download a silent, text-free ASMR background clip. Returns (path, video_id)."""
    keywords = ["asmr", "satisfying", "relaxing", "rain sounds", "fireplace"]
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
def _synth_line(text, voice, out_wav):
    """Synthesize one line with Piper. Returns True on success."""
    try:
        proc = subprocess.run(
            [PIPER_BIN, "--model", voice, "--output_file", out_wav],
            input=text.encode("utf-8"), capture_output=True, timeout=60,
        )
        return proc.returncode == 0 and os.path.exists(out_wav) and os.path.getsize(out_wav) > 100
    except Exception:
        return False


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
    segs = []
    ok = True
    for i, (speaker, text) in enumerate(script):
        voice = PIPER_VOICE_A if speaker == "A" else PIPER_VOICE_B
        wav = os.path.join(tmp, f"line_{i}.wav")
        if not _synth_line(text, voice, wav):
            ok = False
            break
        dur = _wav_duration(wav)
        segs.append((speaker, wav, dur))
    if not ok or not segs:
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
def _seg_wav(segments, idx):
    for s in segments:
        if idx < len(segments) and segments[idx][1] == s[1]:
            return s[1]
    return ""


def _draw_chat_frame(path, messages, font, small, last_speaker):
    """Draw a phone chat screen with the given message history."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (WIDTH, HEIGHT), (10, 12, 18, 255))
    d = ImageDraw.Draw(img)

    header_h = 90
    d.rectangle([0, 0, WIDTH, header_h], fill=(18, 20, 28, 255))
    d.text((40, 30), "Messages", fill=(235, 235, 235, 255), font=small)
    d.line([(0, header_h), (WIDTH, header_h)], fill=(40, 42, 50, 255), width=2)

    y = header_h + 30
    max_bubble_w = int(WIDTH * 0.72)
    for (speaker, text) in messages:
        is_me = (speaker == "B")  # B = "me" on the right
        color = (40, 120, 255, 255) if is_me else (45, 47, 54, 255)
        text_col = (255, 255, 255, 255)
        lines = _wrap(text, font, max_bubble_w - 36)
        bh = 24 + len(lines) * (FONT_SIZE + 6)
        bw = min(max_bubble_w, max((d.textlength(l, font=font) for l in lines)) + 36)
        bx = WIDTH - bw - 30 if is_me else 30
        d.rounded_rectangle([bx, y, bx + bw, y + bh], radius=22, fill=color)
        ty = y + 14
        for l in lines:
            d.text((bx + 18, ty), l, fill=text_col, font=font)
            ty += FONT_SIZE + 6
        y += bh + 18
        if y > HEIGHT - 120:
            break

    if last_speaker:
        ty2 = y + 6
        tw = 90
        tx = WIDTH - tw - 30 if last_speaker == "B" else 30
        d.rounded_rectangle([tx, ty2, tx + tw, ty2 + 46], radius=22,
                            fill=(45, 47, 54, 255))
        for dot in range(3):
            cx = tx + 26 + dot * 22
            d.ellipse([cx, ty2 + 18, cx + 12, ty2 + 30], fill=(180, 180, 180, 255))

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


def _render_chat_video(username, bg_path, script, audio_segments, out_path, tmp):
    """Draw the chat overlay frame-by-frame and mux with the TTS audio."""
    from PIL import Image, ImageDraw, ImageFont

    bg_tmp = os.path.join(tmp, "bg_scaled.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", bg_path,
         "-vf", f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
         "-t", str(TARGET_DURATION), bg_tmp],
        capture_output=True, timeout=120,
    )
    if not os.path.exists(bg_tmp):
        return None

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", FONT_SIZE)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
        small = font

    frames = []
    t = 0.0
    for i, (speaker, text) in enumerate(script):
        png = os.path.join(tmp, f"frame_{i}.png")
        _draw_chat_frame(png, script[:i + 1], font, small, speaker)
        dur = 2.0
        if audio_segments:
            for s in audio_segments:
                if s[1] and i < len(audio_segments) and os.path.basename(s[1]) == os.path.basename(_seg_wav(audio_segments, i)):
                    dur = s[3] or 2.0
                    break
        frames.append((round(t, 2), png, dur))
        t += dur

    overlay_concat = os.path.join(tmp, "overlay.txt")
    with open(overlay_concat, "w") as f:
        for start, png, dur in frames:
            f.write(f"file '{os.path.basename(png)}'\nduration {dur:.2f}\n")
    overlay_vid = os.path.join(tmp, "overlay.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", overlay_concat,
         "-vf", f"scale={WIDTH}:{HEIGHT},format=rgba",
         "-c:v", "png", "-pix_fmt", "rgba", "-t", str(TARGET_DURATION), overlay_vid],
        capture_output=True, cwd=tmp, timeout=60,
    )

    have_audio = bool(audio_segments) and any(
        os.path.exists(s[1]) for s in audio_segments)
    if have_audio:
        final_audio = os.path.join(tmp, "final_audio.wav")
        cmd = ["ffmpeg", "-y", "-i", bg_tmp, "-i", overlay_vid, "-i", final_audio,
               "-filter_complex",
               "[1:v]format=rgba,colorchannelmixer=aa=0.92[ov];[0:v][ov]overlay=0:0:format=auto[v]",
               "-map", "[v]", "-map", "2:a",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-t", str(TARGET_DURATION), "-shortest", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", bg_tmp, "-i", overlay_vid,
               "-filter_complex",
               "[1:v]format=rgba,colorchannelmixer=aa=0.92[ov];[0:v][ov]overlay=0:0:format=auto",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
               "-pix_fmt", "yuv420p",
               "-t", str(TARGET_DURATION), out_path]
    subprocess.run(cmd, capture_output=True, timeout=180)
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
              " - install ffmpeg/ffprobe/piper (Ollama optional)."
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
            log(f"[{username}] Faceless: no ASMR background downloaded (TikWM/network issue)")
            return None, None, None

        script = _generate_script_llm(username)
        audio_final, segments = _build_audio(username, script, tmp)

        out = os.path.join(DOWNLOADS_DIR, f"{re.sub(r'[^A-Za-z0-9_-]', '_', username)}_faceless_{int(time.time())}.mp4")
        final = _render_chat_video(username, bg, script, segments, out, tmp)
        if not final:
            return None, None, None

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
