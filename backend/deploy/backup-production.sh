#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "Usage: $0 /path/to/backup.dump" >&2
  exit 2
fi

backup_path=$1
env_file=${SENTINEL_ENV_FILE:-.env.production}
project_name=${COMPOSE_PROJECT_NAME:-api-sentinel-production}
umask 077
mkdir -p "$(dirname "$backup_path")"
docker compose --env-file "$env_file" -p "$project_name" -f docker-compose.production.yml \
  exec -T postgres pg_dump -U sentinel_admin -d sentinel -Fc > "$backup_path"
chmod 600 "$backup_path"
echo "Database backup written to $backup_path"
