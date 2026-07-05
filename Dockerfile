# ENLIGHT TUTOR Backend — production container
FROM python:3.11-slim

WORKDIR /app

# Base image already includes sqlite3 support; keep the image minimal
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Persist the SQLite cache and uploaded proof-of-payment files outside the app layer
# if you mount a volume at /app/data
ENV DB_PATH=/app/data/enlight_cache.db
ENV UPLOAD_DIR=/app/data/uploads
RUN mkdir -p /app/data/uploads

EXPOSE 8000

# Railway/Render inject $PORT; default to 8000 for local `docker run`
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
