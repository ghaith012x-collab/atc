#!/bin/bash
# Ensure Faceless deps (Piper TTS + two English voices) exist.
# The Dockerfile tries to fetch them at build time, but if that download
# was blocked/silent we re-attempt here so Faceless gets real voices.
set -e
mkdir -p /opt/piper/voices
cd /opt/piper

if [ ! -x /opt/piper/piper ]; then
  echo "[bootstrap] fetching Piper binary..."
  ( apt-get update >/dev/null 2>&1 && apt-get install -y --no-install-recommends wget >/dev/null 2>&1 ) || true
  wget -q https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_linux_x86_64.tar.gz -O piper.tar.gz 2>/dev/null || true
  tar -xzf piper.tar.gz 2>/dev/null || true
  chmod +x /opt/piper/piper 2>/dev/null || true
fi

cd /opt/piper/voices
for v in en_US-ryan-high en_US-libritts_r-medium; do
  if [ ! -f "$v.onnx" ]; then
    echo "[bootstrap] fetching Piper voice $v ..."
    case "$v" in
      en_US-ryan-high)      wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/$v.onnx -O $v.onnx 2>/dev/null || true ;;
      en_US-libritts_r-medium) wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/$v.onnx -O $v.onnx 2>/dev/null || true ;;
    esac
  fi
done

echo "[bootstrap] piper=$( [ -x /opt/piper/piper ] && echo present || echo MISSING )"
echo "[bootstrap] voices: $(ls /opt/piper/voices 2>/dev/null | tr '\n' ' ')"

# Hand off to the real server.
exec xvfb-run -a --server-args="-screen 0 1280x720x24" gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 120 --keep-alive 5
