FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Create sessions folder
RUN mkdir -p sessions

CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120