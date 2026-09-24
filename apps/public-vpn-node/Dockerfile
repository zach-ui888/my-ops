FROM debian:12-slim
LABEL org.opencontainers.image.title="public-vpn-node" \
      org.opencontainers.image.source="https://github.com/woyaozuofeiji/vpngate-docker" \
      org.opencontainers.image.revision="14b803685e67b7d50deff8ee768d042d96dcaab1" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 openvpn iproute2 nftables ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app/ /app/
COPY scripts/healthcheck.py /app/healthcheck.py
COPY licenses/ /app/licenses/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 VPNGATE_DATA_DIR=/data
RUN mkdir -p /data && chmod 700 /data
EXPOSE 8787 7928
ENTRYPOINT ["python3", "/app/entrypoint.py"]
