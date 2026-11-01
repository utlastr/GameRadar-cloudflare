#!/bin/sh
set -eu

DB="${RADAR_DB_PATH:-/var/lib/gameradar/radar.db}"
DATA_DIR="${RADAR_DATA_DIR:-/var/lib/gameradar}"
mkdir -p "$(dirname "$DB")"

if [ ! -s "$DB" ] || [ ! -s "$DATA_DIR/settings.json" ]; then
  if [ -z "${R2_ACCOUNT_ID:-}" ] || [ -z "${R2_BUCKET:-}" ] ||
     [ -z "${R2_ACCESS_KEY_ID:-}" ] || [ -z "${R2_SECRET_ACCESS_KEY:-}" ]; then
    echo "Radar database/settings are missing and R2 restore credentials are not configured." >&2
    exit 1
  fi

  export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
  export AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
  export AWS_DEFAULT_REGION=auto
  ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

  if [ ! -s "$DB" ]; then
    litestream restore -config /etc/litestream.yml -if-replica-exists "$DB"
  fi
  if [ ! -s "$DB" ]; then
    : "${R2_SEED_KEY:=seed/radar.db}"
    aws s3 cp "s3://${R2_BUCKET}/${R2_SEED_KEY}" "$DB" \
      --endpoint-url "$ENDPOINT" --region auto
  fi
  if [ ! -s "$DATA_DIR/settings.json" ]; then
    : "${R2_SETTINGS_KEY:=seed/settings.json}"
    aws s3 cp "s3://${R2_BUCKET}/${R2_SETTINGS_KEY}" "$DATA_DIR/settings.json" \
      --endpoint-url "$ENDPOINT" --region auto
  fi
fi

exec litestream replicate -config /etc/litestream.yml \
  -exec "python /app/run_radar.py --no-browser"
