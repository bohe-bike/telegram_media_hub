# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
# --mount=type=cache 让 apt 缓存在构建器磁盘上，重建时不再重复下载
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -e; \
    for i in 1 2 3; do \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    build-essential \
    python3-dev \
    libssl-dev \
    libffi-dev \
    && break || { echo "apt-get failed (attempt $i), retrying..."; sleep 5; }; \
    done

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

# Install Python dependencies
# --mount=type=cache 让 pip wheel 缓存留在构建器磁盘上，requirements.txt
# 未变化时直接从缓存安装，无需联网
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# Copy application code
COPY . .

# Create sessions directory for Pyrogram and set ownership
RUN mkdir -p sessions config && chown -R appuser:appuser /app

USER appuser

# Default command (overridden by docker-compose for workers)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
