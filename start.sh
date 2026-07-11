#!/bin/bash
set -e

mkdir -p /data/.hermes/cron /data/.hermes/sessions /data/.hermes/logs \
         /data/.hermes/memories /data/.hermes/skills /data/.hermes/platforms/pairing \
         /data/.hermes/hooks /data/.hermes/cache/images /data/.hermes/cache/audio \
         /data/.hermes/workspace /data/.hermes/skins /data/.hermes/plans \
         /data/.hermes/home

# Preserve the pre-upgrade state on the persistent Railway volume. The backup
# is intentionally idempotent so a restart never replaces the known-good copy.
UPGRADE_BACKUP_DIR=/data/.hermes/backups/pre-v2026.7.7.2
mkdir -p "${UPGRADE_BACKUP_DIR}"
chmod 700 /data/.hermes/backups "${UPGRADE_BACKUP_DIR}"

backup_once() {
  local source_path="$1"
  local backup_name="$2"
  if [ -e "${source_path}" ] && [ ! -e "${UPGRADE_BACKUP_DIR}/${backup_name}" ]; then
    cp -a "${source_path}" "${UPGRADE_BACKUP_DIR}/${backup_name}"
  fi
}

backup_once /data/.hermes/config.yaml config.yaml
backup_once /data/.hermes/.env env
backup_once /data/.hermes/auth.json auth.json
backup_once /data/.hermes/pairing legacy-pairing
backup_once /data/.hermes/platforms/pairing pairing

printf 'docker\n' > /data/.hermes/.install_method

if [ ! -f /data/.hermes/config.yaml ] && [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
  cp /opt/hermes-agent/cli-config.yaml.example /data/.hermes/config.yaml
fi

[ ! -f /data/.hermes/.env ] && touch /data/.hermes/.env
chmod 600 /data/.hermes/.env /data/.hermes/config.yaml 2>/dev/null || true

if [ ! -f /data/.hermes/auth.json ] && [ -n "${HERMES_AUTH_JSON_BOOTSTRAP}" ]; then
  printf '%s' "${HERMES_AUTH_JSON_BOOTSTRAP}" > /data/.hermes/auth.json
  chmod 600 /data/.hermes/auth.json
fi

[ -f /data/.hermes/auth.json ] && chmod 600 /data/.hermes/auth.json

rm -f /data/.hermes/gateway.pid

exec python /app/server.py
