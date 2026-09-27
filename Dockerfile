FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AUTOTUNNEL_DATA_DIR=/data \
    AUTOTUNNEL_ORIGIN_HOST=127.0.0.1 \
    AUTOTUNNEL_PORT=18770
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home autotunnel \
    && mkdir /data && chown autotunnel:autotunnel /data
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ ./backend/
COPY --from=frontend /build/frontend/dist/ ./frontend/dist/
USER autotunnel
EXPOSE 18770
CMD ["uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "18770"]
