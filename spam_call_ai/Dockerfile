FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.type="app" \
  io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3 py3-pip curl ca-certificates

# Cloudflare Quick Tunnel remains available as a test fallback.
RUN case "${BUILD_ARCH}" in \
      amd64) CF_ARCH="amd64" ;; \
      aarch64) CF_ARCH="arm64" ;; \
      *) echo "Unsupported architecture: ${BUILD_ARCH}" && exit 1 ;; \
    esac \
  && curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" -o /usr/local/bin/cloudflared \
  && chmod +x /usr/local/bin/cloudflared \
  && /usr/local/bin/cloudflared --version

# ngrok provides the stable public HTTPS/WSS endpoint for Twilio.
RUN case "${BUILD_ARCH}" in \
      amd64) NGROK_ARCH="amd64" ;; \
      aarch64) NGROK_ARCH="arm64" ;; \
      *) echo "Unsupported architecture: ${BUILD_ARCH}" && exit 1 ;; \
    esac \
  && curl -fsSL "https://bin.ngrok.com/c/bNyj1mQVY4c/ngrok-v3-stable-linux-${NGROK_ARCH}.tgz" \
     | tar -xz -C /usr/local/bin \
  && chmod +x /usr/local/bin/ngrok \
  && /usr/local/bin/ngrok version

WORKDIR /app

COPY requirements.txt /app/requirements.txt

RUN python3 -m venv /opt/venv \
  && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
  && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt

COPY app.py /app/app.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD ["/run.sh"]
