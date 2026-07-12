# syntax=docker/dockerfile:1
#
# MEIC bot — paper-mode control panel image.
# Multi-stage: build the React panel with Node, then serve it from the Python
# backend (FastAPI + uvicorn). The runtime layout mirrors the repo so
# server.py finds <root>/frontend/dist and <root>/backend/src.

# --- Stage 1: build the React panel -----------------------------------------
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build          # -> /web/dist

# --- Stage 2: Python runtime ------------------------------------------------
FROM python:3.13-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/backend/src \
    MEIC_DATA_DIR=/data
WORKDIR /app

COPY requirements-runtime.txt ./
RUN pip install --no-cache-dir -r requirements-runtime.txt

# app code + spec + the built panel
COPY backend/ ./backend/
COPY spec/ ./spec/
COPY --from=web /web/dist ./frontend/dist

# durable state (event log + KV) lives on a mounted volume so it survives a
# container restart / recreate (REC-07)
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
# 0.0.0.0 is the container-internal bind; publish it to host 127.0.0.1 only
# (see docker-compose.yml) to keep the panel localhost-exposed per NFR-06.
CMD ["uvicorn", "meic.adapters.api.server:paper_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8000", "--ws", "websockets"]
