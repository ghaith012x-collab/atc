FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Provide a virtual display for headed Playwright/Google verification flows,
# plus ffmpeg (video/audio processing for the Faceless generator).
RUN apt-get update && apt-get install -y --no-install-recommends xvfb ffmpeg espeak-ng tor && rm -rf /var/lib/apt/lists/*

# ---- Faceless deps (NON-FATAL: build succeeds even if a download fails) ----
# Piper TTS static binary + two DISTINCT, natural-sounding English voices
# (a male + a female) so the two chat speakers sound human and different.
# Installed by DEFAULT so Faceless produces voiced Shorts out of the box;
# the generator falls back to espeak-ng / gTTS if Piper is somehow absent.
RUN set -x; \
    apt-get update && apt-get install -y --no-install-recommends wget curl unzip >/dev/null 2>&1 || true; \
    mkdir -p /opt/piper /opt/piper/voices && cd /opt/piper; \
    for url in \
      https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz \
      https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_linux_x86_64.tar.gz ; do \
        wget -q "$url" -O piper.tar.gz && tar -xzf piper.tar.gz && [ -x /opt/piper/piper ] && break; \
    done; \
    chmod +x /opt/piper/piper 2>/dev/null || true; \
    echo "[docker] piper binary: $( [ -x /opt/piper/piper ] && /opt/piper/piper --help >/dev/null 2>&1 && echo OK || echo MISSING )"; \
    cd /opt/piper/voices; \
    for v in en_US-lessac-medium en_US-ryan-medium; do \
      for base in \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium" \
        "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/medium" ; do \
        wget -q "$base/$v.onnx" -O "$v.onnx" 2>/dev/null && [ -s "$v.onnx" ] && break; \
      done; \
      wget -q "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/${v#en_US-}/medium/${v}.onnx.json" -O "$v.onnx.json" 2>/dev/null || true; \
    done; \
    echo "[docker] voices: $(ls /opt/piper/voices 2>/dev/null | tr '\n' ' ')"; \
    true

# Ollama (local LLM for the chat script) — optional, large download.
# Enable with BUILD_OLLAMA=1. The bot falls back to offline templates without it.
ARG BUILD_OLLAMA=0
RUN if [ "$BUILD_OLLAMA" = "1" ]; then \
      set -x; \
      curl -fsSL https://ollama.com/install.sh | sh || true; \
      true; \
    fi

COPY . .
COPY bootstrap.sh /opt/bootstrap.sh
RUN chmod +x /opt/bootstrap.sh

# Create sessions folder
RUN mkdir -p sessions

# Railway entry: bootstrap ensures Faceless deps, then starts the server
# under a virtual X server (needed for headed Playwright/Google flows).
CMD ["/opt/bootstrap.sh"]
