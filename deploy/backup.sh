#!/usr/bin/env bash
# aegis: бэкап, проверка восстановления и откат (пункт 1.11 — обязателен до шага 2).
#
# Почему не «один pg_dump в cron»: неразведанный бэкап — это не бэкап. verify поднимает
# временную БД, restore-ит туда последний дамп и сверяет таблицы — только так узнаёшь, что
# дамп живой, а не битый или снятый не с той базы.
#
# Настройки (можно положить в .env рядом с корнем репозитория):
#   AEGIS_BACKUP_DIR    куда класть дампы (по умолчанию ./backups)
#   AEGIS_BACKUP_KEEP   сколько локальных дампов хранить (по умолчанию 14)
#   AEGIS_RCLONE_REMOTE куда copies в облако, напр. "crypt:aegis-backups" (пусто = не copies)
#   AEGIS_PG_SERVICE    сервис Postgres в compose (по умолчанию postgres)
#   AEGIS_PG_DB / AEGIS_PG_USER  имя БД и пользователь (aegis / aegis)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${AEGIS_BACKUP_DIR:-$ROOT/backups}"
KEEP="${AEGIS_BACKUP_KEEP:-14}"
RCLONE_REMOTE="${AEGIS_RCLONE_REMOTE:-}"
PG_SERVICE="${AEGIS_PG_SERVICE:-postgres}"
PG_DB="${AEGIS_PG_DB:-aegis}"
PG_USER="${AEGIS_PG_USER:-aegis}"
COMPOSE_FILE="$ROOT/deploy/docker-compose.yml"
ENVFILE_ARGS=()
[ -f "$ROOT/.env" ] && ENVFILE_ARGS=(--env-file "$ROOT/.env")

compose() { docker compose "${ENVFILE_ARGS[@]}" -f "$COMPOSE_FILE" "$@"; }
# утилиты postgres запускаем внутри контейнера: хостовый клиент может быть другой масти
pg() { compose exec -T "$PG_SERVICE" "$@"; }

log() { printf '[backup] %s\n' "$*"; }
die() { printf '[backup] !! %s\n' "$*" >&2; exit 1; }

# таблицы, пустота или отсутствие которых = потеря данных
TABLES=(
  "platform.events"
  "platform.llm_calls"
  "governance.tool_runs"
  "memory.facts"
  "knowledge.notes"
)

do_backup() {
  mkdir -p "$BACKUP_DIR"
  local ts file tmp
  ts="$(date -u +%Y-%m-%dT%H%M%SZ)"
  file="$BACKUP_DIR/aegis_$ts.dump"
  tmp="$file.part"

  # -Fc = custom format: сжатие + выборочный pg_restore
  pg pg_dump -U "$PG_USER" -Fc --no-owner --no-privileges "$PG_DB" > "$tmp"
  [ -s "$tmp" ] || die "пустой дамп: pg_dump ничего не вернул"
  # читаемость проверяем до того, как файл станет «последним хорошим»
  pg pg_restore --list < "$tmp" > /dev/null || die "дамп не читается: $tmp"
  mv "$tmp" "$file"
  chmod 600 "$file"

  if command -v sha256sum > /dev/null; then
    (cd "$BACKUP_DIR" && sha256sum "$(basename "$file")" > "$(basename "$file").sha256")
  fi
  log "дамп: $file ($(du -h "$file" | cut -f1))"

  # ротация: локально держим N дампов, дальше — только копия в облаке
  local -a dumps
  mapfile -t dumps < <(ls -1t "$BACKUP_DIR"/aegis_*.dump 2>/dev/null || true)
  if [ "${#dumps[@]}" -gt "$KEEP" ]; then
    local old
    for old in "${dumps[@]:$KEEP}"; do
      rm -f "$old" "$old.sha256"
      log "ротация: удалён $old"
    done
  fi

  if [ -n "$RCLONE_REMOTE" ]; then
    command -v rclone > /dev/null || die "AEGIS_RCLONE_REMOTE задан, но rclone не установлен"
    rclone copy "$file" "$RCLONE_REMOTE" --checksum
    [ -f "$file.sha256" ] && rclone copy "$file.sha256" "$RCLONE_REMOTE" --checksum
    log "отправлено в $RCLONE_REMOTE"
  else
    log "AEGIS_RCLONE_REMOTE пуст — в облако не отправляем (локальный дамп есть)"
  fi
}

# restore во временную БД того же кластера: проверяем и дамп, и схему
do_verify() {
  local file="${1:-}"
  if [ -z "$file" ]; then
    file="$(ls -1t "$BACKUP_DIR"/aegis_*.dump 2>/dev/null | head -1 || true)"
  fi
  [ -n "$file" ] && [ -f "$file" ] || die "не найден дамп для проверки в $BACKUP_DIR"

  local verify_db="aegis_verify_$$"
  log "проверяю $(basename "$file") на временной БД $verify_db"
  pg psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -c "CREATE DATABASE $verify_db" > /dev/null
  # compose cp (docker compose v2): дамп с хоста в контейнер, иначе stdin занят heredoc'ом
  compose cp "$file" "$PG_SERVICE:/tmp/verify.dump"
  # pg_restore умеет возвращать 0 при ошибках — потому сверяем структуру и данные явно
  pg pg_restore -U "$PG_USER" -d "$verify_db" --no-owner --no-privileges /tmp/verify.dump \
    || log "pg_restore завершился с кодом != 0 — смотрим счётчики таблиц"
  pg rm -f /tmp/verify.dump

  local fail=0 tables row t
  tables="$(pg psql -U "$PG_USER" -d "$verify_db" -tA \
    -c "SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('platform','governance','memory','knowledge')" \
    | tr -d '[:space:]')"
  [ "${tables:-0}" -ge 5 ] || { log "!! таблиц меньше ожидаемого: $tables"; fail=1; }
  for t in "${TABLES[@]}"; do
    row="$(pg psql -U "$PG_USER" -d "$verify_db" -tA -c "SELECT count(*) FROM $t" 2>/dev/null | tr -d '[:space:]' || true)"
    [[ "$row" =~ ^[0-9]+$ ]] || { log "!! $t: недоступна после restore"; fail=1; continue; }
    log "   $t: $row строк"
  done
  pg psql -U "$PG_USER" -d postgres -c "DROP DATABASE $verify_db" > /dev/null 2>&1 || true

  [ "$fail" -eq 0 ] || die "проверка восстановления не прошла — бэкап непригоден"
  log "verify ok: дамп восстанавливается, ожидаемые таблицы на месте"
}

do_restore() {
  local file="${1:-}"
  [ -n "$file" ] && [ -f "$file" ] || die "usage: $0 restore path/to/aegis_x.dump"
  log "ВНИМАНИЕ: восстановление перезатрёт рабочую БД $PG_DB"
  compose stop bot || true
  # схему не «латает» --clean по куску: сначала чистая база, затем дамп целиком
  pg psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 \
    -c "DROP DATABASE IF EXISTS $PG_DB" -c "CREATE DATABASE $PG_DB OWNER $PG_USER"
  compose cp "$file" "$PG_SERVICE:/tmp/restore.dump"
  pg pg_restore -U "$PG_USER" -d "$PG_DB" /tmp/restore.dump
  pg rm -f /tmp/restore.dump
  compose up -d bot || true
  log "готово: проверьте aegis doctor и пару действий в Telegram"
}

do_list() {
  ls -1t "$BACKUP_DIR"/aegis_*.dump 2>/dev/null | head -20 || log "дампов нет ($BACKUP_DIR)"
}

cmd="${1:-help}"
case "$cmd" in
  backup) do_backup ;;
  verify) do_verify "${2:-}" ;;
  restore) do_restore "${2:-}" ;;
  list) do_list ;;
  *)
    cat <<'USAGE'
usage: backup.sh <backup|verify [dump]|restore <dump>|list>
  backup   pg_dump + проверка читаемости + ротация + (опционально) rclone в облако
  verify   восстановить последний (или указанный) дамп во временную БД и сверить таблицы
  restore  опасная операция: перезатереть рабочую БД из дампа
  list     список локальных дампов
USAGE
    exit 2
    ;;
esac
