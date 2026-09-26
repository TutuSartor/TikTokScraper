#!/usr/bin/env sh
# Backup do PostgreSQL em formato custom (pg_dump -Fc) para ./backups/.
# Uso: ./scripts/backup.sh            (rodar na raiz do projeto, com o Compose ativo)
# Cron diário (exemplo, 03:15):  15 3 * * * cd /opt/product-intelligence && ./scripts/backup.sh >> backups/backup.log 2>&1
set -eu

cd "$(dirname "$0")/.."
mkdir -p backups
mkdir backups/.backup.lock 2>/dev/null || { echo "backup já em execução"; exit 1; }
PARTIAL=""
cleanup() {
  [ -z "$PARTIAL" ] || rm -f -- "$PARTIAL"
  rmdir backups/.backup.lock
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PARTIAL="$(mktemp "backups/.backup_${STAMP}_XXXXXX")"
OUT="backups/$(basename "$PARTIAL" | cut -c 2-).dump"

# O Compose lê o .env; as credenciais vêm do ambiente do container.
docker compose exec -T db sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$PARTIAL"
mv -- "$PARTIAL" "$OUT"
PARTIAL=""
echo "$(date -u +%FT%TZ) backup ok: $OUT ($(wc -c < "$OUT") bytes)"

# Mantém os últimos 14 backups.
find backups -maxdepth 1 -type f -name 'backup_*.dump' | sort -r | tail -n +15 |
  while IFS= read -r old; do rm -f -- "$old"; done
