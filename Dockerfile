# syntax=docker/dockerfile:1.7
# Media Integrity Scanner – Multi-Stage-Build (linux/amd64, linux/arm64)
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d
ARG RUNTIME_IMAGE=ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=secret,id=ca_cert,required=false,mode=0444 \
    if [ -f /run/secrets/ca_cert ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_cert; fi \
 && npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM ${RUNTIME_IMAGE} AS runtime
LABEL org.opencontainers.image.title="Media Integrity Scanner" \
      org.opencontainers.image.description="Prüft Filmsammlungen (MKV, MP4, AVI, MOV, M4V, TS) auf beschädigte Dateien" \
      org.opencontainers.image.version="1.0.0"

# Zuerst über die signierten Ubuntu-APT-Quellen CA-Zertifikate installieren.
# Danach APT auf HTTPS umstellen. Keine TLS-Prüfung wird deaktiviert.
ARG DEBIAN_FRONTEND=noninteractive
RUN --mount=type=secret,id=ca_cert,required=false,mode=0444 \
    set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    update-ca-certificates; \
    if [ -f /run/secrets/ca_cert ]; then \
      cp /run/secrets/ca_cert /usr/local/share/ca-certificates/build-proxy.crt; \
      update-ca-certificates; \
    fi; \
    sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 python3-venv ffmpeg mkvtoolnix util-linux passwd tzdata; \
    rm -f /usr/local/share/ca-certificates/build-proxy.crt; \
    update-ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    python3 -m venv /opt/venv

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Europe/Berlin \
    CONFIG_DIR=/config \
    MEDIA_ROOT=/media \
    STATIC_DIR=/app/static \
    PORT=8080 \
    PUID=3000 \
    PGID=3000 \
    MAX_CONCURRENT_SCANS=1

WORKDIR /app
COPY backend/requirements.txt /app/requirements.txt
RUN --mount=type=secret,id=ca_cert,required=false,mode=0444 \
    if [ -f /run/secrets/ca_cert ]; then export PIP_CERT=/run/secrets/ca_cert; fi \
 && pip install -r /app/requirements.txt

RUN groupadd -g 3000 app && useradd -u 3000 -g 3000 -d /config -s /usr/sbin/nologin -M app \
 && mkdir -p /config /media && chown app:app /config

COPY backend/app /app/app
COPY backend/alembic.ini /app/alembic.ini
COPY --from=frontend /src/dist /app/static
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh && python -m compileall -q /app/app

VOLUME ["/config"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'), timeout=5)"
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--workers", "1", "--no-access-log", "--timeout-graceful-shutdown", "20"]
