#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Spam Call AI"

exec /opt/venv/bin/uvicorn app:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips "*"
