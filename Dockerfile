FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Provide a virtual display for headed Playwright/Google verification flows.
RUN apt-get update && apt-get install -y --no-install-recommends xvfb && rm -rf /var/lib/apt/lists/*

COPY . .

# Create sessions folder
RUN mkdir -p sessions

# Railway uses the Dockerfile CMD; keep Playwright headed under a virtual X server.
CMD xvfb-run -a --server-args="-screen 0 1280x720x24" gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --worker-class gthread --timeout 120 --keep-alive 5
