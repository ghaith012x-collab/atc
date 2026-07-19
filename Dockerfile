FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Provide a virtual display for headed Playwright/Google verification flows,
# plus ffmpeg (video/audio processing for the Faceless generator).
RUN apt-get update && apt-get install -y --no-install-recommends xvfb ffmpeg espeak-ng tor && rm -rf /var/lib/apt/lists/*

# ---- Faceless deps (NON-FATAL: build succeeds even if a download fails) ----
# Piper TTS static binary + two distinct English voices so the two chat
# speakers sound different. Installed by DEFAULT so Faceless produces
# voiced Shorts out of the box; the generator falls back to a silent
# text-only video if Piper is somehow absent at runtime.
RUN set -x; \
    apt-get update && apt-get install -y --no-install-recommends wget unzip >/dev/null 2>&1 || true; \
    mkdir -p /opt/piper /opt/piper/voices && cd /opt/piper; \
    wget -q https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_linux_x86_64.tar.gz -O piper.tar.gz 2>/dev/null || true; \
    tar -xzf piper.tar.gz 2>/dev/null || true; \
    (cd voices && wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx -O en_US-ryan-high.onnx 2>/dev/null || true); \
    (cd voices && wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx -O en_US-libritts_r-medium.onnx 2>/dev/null || true); \
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
