#!/usr/bin/env sh
set -eu

OUTPUT_DIR="${1:-./backups}"
COMPOSE_FILE="${COMPOSE_FILE:-./deploy/docker-compose.production.yml}"
ENV_FILE="${ENV_FILE:-./.env.production}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-evalrag-production}"
DB_NAME="${DB_NAME:-evalrag}"

mkdir -p "$OUTPUT_DIR"
TARGET="$OUTPUT_DIR/evalrag-$(date +%Y%m%d-%H%M%S).sql"

docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
  exec -T postgres pg_dump -U evalrag -d "$DB_NAME" > "$TARGET"

echo "Backup written to $TARGET"
