#!/usr/bin/env sh
# Restaura um backup gerado por backup.sh.
#   sh scripts/restore.sh backups/arquivo.dump              -> restaura num banco temporário exclusivo
#   ./scripts/restore.sh backups/arquivo.dump --replace    -> SUBSTITUI o banco principal (para a API antes)
set -eu

cd "$(dirname "$0")/.."
[ $# -ge 1 ] && [ $# -le 2 ] || { echo "uso: $0 <arquivo.dump> [--replace]"; exit 1; }
DUMP="$1"; MODE="${2:-check}"
case "$MODE" in check|--replace) ;; *) echo "opção inválida: $MODE"; exit 1 ;; esac
[ -f "$DUMP" ] || { echo "arquivo não encontrado: $DUMP"; exit 1; }

drop_database() {
  docker compose exec -T db sh -c 'exec dropdb -U "$POSTGRES_USER" --if-exists --force -- "$1"' sh "$TARGET"
}
create_database() {
  docker compose exec -T db sh -c 'exec createdb -U "$POSTGRES_USER" -- "$1"' sh "$TARGET"
}
TEMP_CREATED=0
cleanup() {
  if [ "$TEMP_CREATED" = 1 ]; then drop_database; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

if [ "$MODE" = "--replace" ]; then
  TARGET="$(docker compose exec -T db sh -c 'printf "%s" "$POSTGRES_DB"')"
  printf 'Isto vai APAGAR e recriar o banco "%s". Digite o nome do banco para confirmar: ' "$TARGET"
  read -r CONFIRM
  [ "$CONFIRM" = "$TARGET" ] || { echo "cancelado"; exit 1; }
  docker compose stop api
  drop_database
  create_database
else
  TARGET="pi_restore_$(date -u +%Y%m%d%H%M%S)_$$"
  create_database
  TEMP_CREATED=1
fi

docker compose exec -T db sh -c 'exec pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --exit-on-error' sh "$TARGET" < "$DUMP"

echo "restaurado em \"$TARGET\". Versão das migrações:"
docker compose exec -T db sh -c 'exec psql -U "$POSTGRES_USER" -d "$1" -v ON_ERROR_STOP=1 -tAc "SELECT version_num FROM alembic_version;"' sh "$TARGET"

if [ "$MODE" = "--replace" ]; then
  docker compose start api
else
  drop_database
  TEMP_CREATED=0
  echo "teste de restauração ok (banco temporário removido)."
fi
