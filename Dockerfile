FROM golang:1.24 AS litestream-build
RUN go install github.com/benbjohnson/litestream/cmd/litestream@v0.3.13

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    RADAR_DATA_DIR=/var/lib/gameradar \
    RADAR_DB_PATH=/var/lib/gameradar/radar.db \
    RADAR_SERVER_HOST=0.0.0.0 \
    RADAR_SERVER_PORT=8765 \
    RADAR_SQLITE_JOURNAL_MODE=WAL

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir awscli requests beautifulsoup4 lxml

COPY --from=litestream-build /go/bin/litestream /usr/local/bin/litestream
WORKDIR /app
COPY radar ./radar
COPY run_radar.py ./
COPY cloudflare/container/entrypoint.sh /usr/local/bin/gameradar-entrypoint
COPY cloudflare/container/litestream.yml /etc/litestream.yml
COPY cloudflare/container/seed_format.py cloudflare/container/restore_seed.py /app/cloudflare/container/
RUN chmod 0755 /usr/local/bin/gameradar-entrypoint \
    && mkdir -p /var/lib/gameradar

EXPOSE 8765
ENTRYPOINT ["/usr/local/bin/gameradar-entrypoint"]
