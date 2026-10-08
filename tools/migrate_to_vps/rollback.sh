#!/usr/bin/env bash
# Откат выкладки: вернуть данные, .env и код, какими они были до deploy.sh.
#
#     bash rollback.sh /opt/norwik-helper-backup/<время>
#
# Каталог резервной копии печатает deploy.sh в конце. Данные тома заменяются копией ЦЕЛИКОМ:
# всё, что новый бот успел сделать после выкладки, при откате теряется.
set -euo pipefail

BACKUP="${1:?Укажите каталог резервной копии: bash rollback.sh /opt/norwik-helper-backup/...}"
APP=/opt/norwik-helper
SERVICE=shop-helper

[ -f "$BACKUP/git-rev" ] && [ -d "$BACKUP/data" ] || { echo "Это не резервная копия deploy.sh: $BACKUP" >&2; exit 1; }
cd "$APP"
VOLUME=$(docker volume inspect -f '{{.Mountpoint}}' norwik-helper_bot-data)

docker compose stop "$SERVICE"
find "$VOLUME" -mindepth 1 -delete
cp -a "$BACKUP/data/." "$VOLUME/"
git checkout -q "$(cat "$BACKUP/git-rev")"
cp -a "$BACKUP/.env" .env
docker compose up -d --build "$SERVICE"
sleep 20
docker compose ps "$SERVICE"
echo "Откат выполнен: коммит $(git rev-parse --short HEAD), данные из $BACKUP"
