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
    : "${R2_SEED_KEY:=seed/current.json}"
    if python /app/cloudflare/container/restore_seed.py \
      --database "$DB" \
      --settings-download /tmp/radar-seed-settings.json \
      --settings "$DATA_DIR/settings.json" \
      --bucket "$R2_BUCKET" \
      --manifest-key "$R2_SEED_KEY" \
      --endpoint "$ENDPOINT"; then
      :
    elif [ ! -s "$DB" ]; then
      echo "Chunked seed unavailable; trying the legacy single-file seed." >&2
      aws s3 cp "s3://${R2_BUCKET}/seed/radar.db" "$DB" \
        --endpoint-url "$ENDPOINT" --region auto
      if [ ! -s "$DATA_DIR/settings.json" ]; then
        : "${R2_SETTINGS_KEY:=seed/settings.json}"
        aws s3 cp "s3://${R2_BUCKET}/${R2_SETTINGS_KEY}" "$DATA_DIR/settings.json" \
          --endpoint-url "$ENDPOINT" --region auto
      fi
    else
      echo "Could not restore runtime settings from the chunked seed." >&2
      exit 1
    fi
  elif [ ! -s "$DATA_DIR/settings.json" ]; then
    : "${R2_SEED_KEY:=seed/current.json}"
    python /app/cloudflare/container/restore_seed.py \
      --database "$DB" \
      --settings "$DATA_DIR/settings.json" \
      --bucket "$R2_BUCKET" \
      --manifest-key "$R2_SEED_KEY" \
      --endpoint "$ENDPOINT" \
      --settings-only
  fi
fi

exec litestream replicate -config /etc/litestream.yml \
  -exec "python /app/run_radar.py --no-browser"
