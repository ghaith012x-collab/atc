FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Provide a virtual display for headed Playwright/Google verification flows,
# plus ffmpeg (video/audio processing for the Faceless generator).
RUN apt-get update && apt-get install -y --no-install-recommends xvfb ffmpeg && rm -rf /var/lib/apt/lists/*

# ---- Optional Faceless deps (NON-FATAL: build succeeds even if these fail) ----
# Piper TTS static binary + two English voices for distinct chat speakers.
# Set BUILD_FACELESS=1 to attempt downloading them at build time.
ARG BUILD_FACELESS=0
RUN if [ "$BUILD_FACELESS" = "1" ]; then \
      set -x; \
      apt-get update && apt-get install -y --no-install-recommends wget unzip || true; \
      mkdir -p /opt/piper /opt/piper/voices && cd /opt/piper; \
      wget -q https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_linux_x86_64.tar.gz -O piper.tar.gz || true; \
      tar -xzf piper.tar.gz 2>/dev/null || true; \
      (cd voices && wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx -O en_US-ryan-high.onnx || true); \
      (cd voices && wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx -O en_US-libritts_r-medium.onnx || true); \
      true; \
    fi

# Ollama (local LLM for the chat script) — only if explicitly enabled, since
# it's a large download. The bot falls back to offline templates without it.
ARG BUILD_OLLAMA=0
RUN if [ "$BUILD_OLLAMA" = "1" ]; then \
      set -x; \
      curl -fsSL https://ollama.com/install.sh | sh || true; \
      true; \
    fi

COPY . .

# Create sessions folder
RUN mkdir -p sessions

# Railway uses the Dockerfile CMD; keep Playwright headed under a virtual X server.
CMD xvfb-run -a --server-args="-screen 0 1280x720x24" gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 120 --keep-alive 5
