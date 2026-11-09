# ===== AIMS Backend - FastAPI application image =====
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps needed to build psycopg2 / other wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app ./app
COPY frontend.html ./frontend.html
COPY cores ./cores

# Run unprivileged
RUN useradd --system --uid 10001 --home-dir /app aims \
    && mkdir -p /app/aims_data /app/data \
    && chown -R aims:aims /app
USER aims

EXPOSE 8000

# /health is exempt from the HTTPS redirect/reject so the plain-HTTP probe works
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# --proxy-headers: trust X-Forwarded-* ONLY from TRUSTED_PROXY_IPS (default: loopback).
# No --reload in the image.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${TRUSTED_PROXY_IPS:-127.0.0.1}\""]
