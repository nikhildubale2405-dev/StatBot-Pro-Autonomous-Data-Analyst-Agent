FROM node:22-alpine AS frontend-build

WORKDIR /app/frontend
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend ./
RUN npm run build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    APP_NAME="StatBot Pro" \
    ENVIRONMENT=production \
    DATA_DIR=/app/data \
    DATABASE_URL=sqlite:////app/data/db/statbot.db \
    FRONTEND_DIST_DIR=/app/frontend/dist \
    ALLOWED_ORIGINS= \
    SANDBOX_MODE=local \
    SANDBOX_LOCAL_RUNNER_PATH=/app/sandbox/runtime/runner.py \
    SANDBOX_TIMEOUT_SECONDS=25 \
    PORT=7860

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /usr/sbin/nologin statbot

COPY backend/requirements.txt /tmp/backend-requirements.txt
COPY sandbox/requirements.txt /tmp/sandbox-requirements.txt
RUN pip install --no-cache-dir -r /tmp/backend-requirements.txt -r /tmp/sandbox-requirements.txt

COPY --chown=statbot:statbot backend/app ./app
COPY --chown=statbot:statbot sandbox/runtime ./sandbox/runtime
COPY --from=frontend-build --chown=statbot:statbot /app/frontend/dist ./frontend/dist

RUN mkdir -p /app/data/uploads /app/data/outputs /app/data/db \
    && chown -R statbot:statbot /app/data

USER statbot

EXPOSE 7860
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]
