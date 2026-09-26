#!/usr/bin/env sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <backup.sql>" >&2
  exit 2
fi

COMPOSE_FILE="${COMPOSE_FILE:-./deploy/docker-compose.production.yml}"
ENV_FILE="${ENV_FILE:-./.env.production}"
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-evalrag-production}"
DB_NAME="${DB_NAME:-evalrag}"

docker compose --project-name "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" \
  exec -T postgres psql -U evalrag -d "$DB_NAME" < "$1"

echo "Restore completed from $1"
