#!/bin/sh
set -eu

mkdir -p /app/data/uploads
chown -R appuser:appuser /app/data/uploads

exec runuser -u appuser -- "$@"
