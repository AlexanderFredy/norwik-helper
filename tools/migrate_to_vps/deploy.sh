#!/usr/bin/env bash
# Шаг 2 переноса: выкладка агента с состоянием на VPS.
#
#     bash deploy.sh /root/agent-state-<время>.tar.gz
#
# Что делает, по порядку (любой сбой останавливает скрипт ДО запуска нового бота):
#   0. проверки: в .env есть ONEC_BASE_URL и ONEC_TOKEN, архив цел и сходится с описью;
#   1. резервная копия: весь том данных, .env, docker-compose.yml и текущий коммит —
#      в /opt/norwik-helper-backup/<время>; откат — rollback.sh с этим каталогом;
#   2. остановка бота;
#   3. код: master, только перемоткой (--ff-only);
#   4. данные: база из снимка + белый список из прежней базы сервера, файлы прайсов;
#   5. сборка и запуск;
#   6. проверка: журнал старта и число файлов прайсов после уборки сирот.
#
# Секреты скрипт не трогает: ONEC_* в .env админ дописывает сам, до запуска.
set -euo pipefail

ARCHIVE="${1:?Укажите архив снимка: bash deploy.sh /root/agent-state-....tar.gz}"
APP=/opt/norwik-helper
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP=/opt/norwik-helper-backup/$STAMP
SERVICE=shop-helper

fail() { echo "ОСТАНОВЛЕНО: $*" >&2; exit 1; }
step() { echo; echo "=== $*"; }

cd "$APP"

step "0. Проверки"
grep -Eq '^ONEC_BASE_URL=.+' .env || fail "в $APP/.env нет ONEC_BASE_URL — допишите и запустите снова"
grep -Eq '^ONEC_TOKEN=.+' .env || fail "в $APP/.env нет ONEC_TOKEN — допишите и запустите снова"
[ -f "$ARCHIVE" ] || fail "нет архива $ARCHIVE"
VOLUME=$(docker volume inspect -f '{{.Mountpoint}}' norwik-helper_bot-data) \
    || fail "нет тома norwik-helper_bot-data"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
tar -xzf "$ARCHIVE" -C "$WORK"
STATE="$WORK/agent-state"
[ -f "$STATE/users.db" ] && [ -f "$STATE/manifest.json" ] || fail "в архиве нет users.db или manifest.json"
python3 - "$STATE" <<'PY' || fail "файлы прайсов в архиве не сходятся с описью"
import hashlib, json, sys
from pathlib import Path
state = Path(sys.argv[1])
manifest = json.loads((state / "manifest.json").read_text(encoding="utf-8"))
bad = [name for name, meta in manifest["prices"].items()
       if hashlib.sha256((state / "prices" / name).read_bytes()).hexdigest() != meta["sha256"]]
print(f"прайсов в архиве: {len(manifest['prices'])}, повреждённых: {len(bad)}")
print(f"таблиц: {len(manifest['tables'])}, прайсов модели: {manifest['tables'].get('price', 0)}, "
      f"задач: {manifest['tables'].get('price_task', 0)}")
sys.exit(1 if bad else 0)
PY

step "1. Резервная копия в $BACKUP"
mkdir -p "$BACKUP/data"
cp -a "$VOLUME/." "$BACKUP/data/"
cp -a .env docker-compose.yml "$BACKUP/"
git rev-parse HEAD > "$BACKUP/git-rev"
echo "коммит до выкладки: $(cat "$BACKUP/git-rev")"

step "2. Остановка бота"
docker compose stop "$SERVICE"

step "3. Код: master"
git fetch -q origin
git checkout -q master
git pull -q --ff-only origin master
echo "коммит после выкладки: $(git rev-parse --short HEAD) $(git log -1 --format=%s)"

step "4. Данные"
cp "$STATE/users.db" "$VOLUME/users.db.new"
# БЕЛЫЙ СПИСОК СЕРВЕРА НЕ ТЕРЯЕМ: в снимке свой, у серверного бота свой. Переносим строки по
# общим колонкам — схема таблицы могла с июня поменяться.
python3 - "$VOLUME/users.db.new" "$BACKUP/data/users.db" <<'PY'
import sqlite3, sys
new, old = sys.argv[1], sys.argv[2]
con = sqlite3.connect(new)
con.execute("ATTACH DATABASE ? AS old", (old,))
tables = {r[0] for r in con.execute("SELECT name FROM old.sqlite_master WHERE type='table'")}
if "allowed_users" in tables:
    mine = [r[1] for r in con.execute("PRAGMA main.table_info(allowed_users)")]
    theirs = [r[1] for r in con.execute("PRAGMA old.table_info(allowed_users)")]
    common = [c for c in theirs if c in mine]
    cols = ", ".join(common)
    before = con.execute("SELECT COUNT(*) FROM main.allowed_users").fetchone()[0]
    con.execute(f"INSERT OR IGNORE INTO main.allowed_users ({cols}) "
                f"SELECT {cols} FROM old.allowed_users")
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM main.allowed_users").fetchone()[0]
    print(f"белый список: было {before}, добавлено с сервера {after - before}")
con.close()
PY
rm -f "$VOLUME/users.db-wal" "$VOLUME/users.db-shm"
mv "$VOLUME/users.db.new" "$VOLUME/users.db"
mkdir -p "$VOLUME/prices"
cp -n "$STATE/prices/"* "$VOLUME/prices/" 2>/dev/null || true
echo "файлов прайсов в томе: $(ls "$VOLUME/prices" | wc -l)"

step "5. Сборка и запуск"
docker compose up -d --build "$SERVICE"

step "6. Проверка старта (ждём 60 с)"
sleep 60
docker compose ps "$SERVICE"
docker compose logs --since 3m "$SERVICE" 2>&1 \
    | grep -E "Пути к прайсам|Уборка прайсов|осиротевших|Цикл модели|Run polling|Даты прайсов|ERROR|Traceback" \
    | tail -20 || true
PRICES_NOW=$(ls "$VOLUME/prices" | wc -l)
PRICES_WAS=$(python3 -c "import json,sys;print(len(json.load(open(sys.argv[1]))['prices']))" "$STATE/manifest.json")
echo "файлов прайсов после старта: $PRICES_NOW из $PRICES_WAS"
[ "$PRICES_NOW" -ge "$PRICES_WAS" ] || echo "ВНИМАНИЕ: после старта файлов меньше — смотрите журнал выше"

echo
echo "Готово. Откат: bash $(dirname "$0")/rollback.sh $BACKUP"
