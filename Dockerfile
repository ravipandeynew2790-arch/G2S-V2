# Railway / any container: get2short direct-link generator
# Needs a RESIDENTIAL proxy (datacenter IPs are blocked by the shortener).

FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # Playwright / Patchright browser path
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# System deps for Chromium (headless + headed)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libx11-xcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libxshmfence1 \
        libxss1 \
        libxtst6 \
        xdg-utils \
        xvfb \
        xauth \
        wget \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Install Chromium that Patchright/Playwright will use
RUN python -m patchright install chromium \
    || python -m playwright install chromium \
    || true

# App source
COPY . .

# Writable dirs for profile / cache / debug
RUN mkdir -p /app/profile /app/debug /tmp/g2s-profile \
    && chmod -R 777 /app/profile /app/debug /tmp/g2s-profile

# Railway injects $PORT; default 8080 for local docker runs
ENV PORT=8080 \
    HOST=0.0.0.0 \
    G2S_HEADLESS=0 \
    G2S_BROWSER_CHANNEL= \
    G2S_PROFILE_DIR=/tmp/g2s-profile \
    G2S_NO_OPEN=1

EXPOSE 8080

# Healthcheck (Railway also probes /api/health)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD curl -f http://127.0.0.1:${PORT}/api/health || exit 1

CMD ["python", "server.py"]
