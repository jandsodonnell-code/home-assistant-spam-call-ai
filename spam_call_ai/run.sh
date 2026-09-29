#!/usr/bin/with-contenv bashio
set -e

bashio::log.info "Starting Spam Call AI"

/opt/venv/bin/uvicorn app:app \
  --host 0.0.0.0 \
  --port 8000 \
  --proxy-headers \
  --forwarded-allow-ips "*" &
UVICORN_PID=$!

cleanup() {
  if [[ -n "${CLOUDFLARED_PID:-}" ]]; then
    kill "${CLOUDFLARED_PID}" 2>/dev/null || true
  fi
  kill "${UVICORN_PID}" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

# Wait briefly for the local web server.
for _ in $(seq 1 30); do
  if wget -q -O /dev/null http://127.0.0.1:8000/health 2>/dev/null; then
    break
  fi
  sleep 1
done

PUBLIC_URL="$(bashio::config 'public_base_url' 2>/dev/null || true)"
AUTO_TUNNEL="$(bashio::config 'auto_tunnel' 2>/dev/null || echo true)"

# Home Assistant may return the JSON literal "null" for an unused optional option.
# Treat null/None as empty so the automatic test tunnel can start.
case "${PUBLIC_URL}" in
  null|NULL|None|none|""null""|"""")
    PUBLIC_URL=""
    ;;
esac

if [[ -n "${PUBLIC_URL}" ]]; then
  printf '%s\n' "${PUBLIC_URL%/}" > /data/public_url.txt
  bashio::log.info "Using configured public base URL: ${PUBLIC_URL%/}"
elif [[ "${AUTO_TUNNEL}" == "true" ]]; then
  rm -f /data/public_url.txt /tmp/cloudflared.log
  bashio::log.info "Starting free Cloudflare Quick Tunnel for initial testing"
  cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8000 > /tmp/cloudflared.log 2>&1 &
  CLOUDFLARED_PID=$!

  for _ in $(seq 1 60); do
    if ! kill -0 "${CLOUDFLARED_PID}" 2>/dev/null; then
      bashio::log.error "Cloudflare Quick Tunnel exited before a public URL was assigned"
      cat /tmp/cloudflared.log || true
      exit 1
    fi

    PUBLIC_URL="$(grep -Eo 'https://[-A-Za-z0-9]+\.trycloudflare\.com' /tmp/cloudflared.log | head -n 1 || true)"
    if [[ -n "${PUBLIC_URL}" ]]; then
      printf '%s\n' "${PUBLIC_URL%/}" > /data/public_url.txt
      bashio::log.info "============================================================"
      bashio::log.info "PUBLIC BASE URL: ${PUBLIC_URL%/}"
      bashio::log.info "TWILIO WEBHOOK:  ${PUBLIC_URL%/}/twiml"
      bashio::log.info "This Quick Tunnel URL changes whenever the app/tunnel restarts."
      bashio::log.info "============================================================"
      break
    fi
    sleep 1
  done

  if [[ -z "${PUBLIC_URL}" ]]; then
    bashio::log.error "Timed out waiting for a Cloudflare Quick Tunnel URL"
    cat /tmp/cloudflared.log || true
    exit 1
  fi
else
  bashio::log.warning "No public_base_url configured and auto_tunnel is disabled"
fi

# Keep the app alive. Also fail if the quick tunnel unexpectedly exits.
while true; do
  if ! kill -0 "${UVICORN_PID}" 2>/dev/null; then
    wait "${UVICORN_PID}" || true
    bashio::log.error "Spam Call AI web server stopped"
    exit 1
  fi

  if [[ -n "${CLOUDFLARED_PID:-}" ]] && ! kill -0 "${CLOUDFLARED_PID}" 2>/dev/null; then
    wait "${CLOUDFLARED_PID}" || true
    bashio::log.error "Cloudflare Quick Tunnel stopped"
    exit 1
  fi

  sleep 5
done
