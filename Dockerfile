# Stage 1: Build Vue frontend
FROM node:22-alpine AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --network-timeout=120000 || npm ci --registry=https://registry.npmmirror.com --network-timeout=120000
COPY frontend/index.html frontend/vite.config.js frontend/jsconfig.json ./
COPY frontend/src/ src/
COPY frontend/public/ public/
RUN npm run build

# Stage 2: Python runtime with Playwright
FROM python:3.12-slim-bookworm

# System deps for Playwright Chromium + CJK fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libxdamage1 \
    libxfixes3 libxshmfence1 libx11-xcb1 \
    fonts-noto-cjk \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies
# --only-shell: 容器内永远 headless（无显示器），只装 headless shell，
# 不装只有「有头」模式才用的完整 Chromium（省 ~620MB）。宿主机 login.py
# 用的是宿主机自装的 Playwright，不受影响。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium --only-shell

# Application source
COPY extract.py export.py scheduler.py ./
COPY common/ common/
COPY extractor/ extractor/
COPY backend/ backend/
COPY docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Built frontend from stage 1
COPY --from=frontend-builder /app/frontend/dist frontend/dist

# Environment defaults
ENV MODE=all \
    HEADLESS=true \
    SCRAPER_INCREMENTAL=true \
    SCRAPER_FILTER="" \
    SCRAPER_SCHEDULE="" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

ENTRYPOINT ["bash", "docker-entrypoint.sh"]
