#!/bin/bash
set -euo pipefail

IMAGE="${1:-hermes-wrapper:reliability}"
CONTAINER="hermes-wrapper-smoke-$$"
COOKIE_FILE="/tmp/${CONTAINER}.cookies"
HEALTH_FILE="/tmp/${CONTAINER}.health.json"
PYTHON_BIN=python
command -v "${PYTHON_BIN}" >/dev/null 2>&1 || PYTHON_BIN=python3

cleanup() {
  docker rm -f "${CONTAINER}" >/dev/null 2>&1 || true
  rm -f "${COOKIE_FILE}" "${HEALTH_FILE}"
}
trap cleanup EXIT

open_dashboard() {
  for _ in $(seq 1 20); do
    if curl -fsS -b "${COOKIE_FILE}" 'http://127.0.0.1:18080/?force=1' >/dev/null; then
      return 0
    fi
    sleep 1
  done
  return 1
}

docker run --rm --entrypoint hermes "${IMAGE}" --version | grep -F "v0.18.2 (2026.7.7.2)"
docker run --rm --entrypoint hermes "${IMAGE}" gateway run --help | grep -F -- "--replace"
docker run --rm --entrypoint hermes "${IMAGE}" dashboard --help | grep -F -- "--skip-build"

docker run -d --name "${CONTAINER}" \
  -p 127.0.0.1:18080:8080 \
  -e ADMIN_PASSWORD=smoke-admin-password \
  -e COOKIE_SECURE=false \
  -e HERMES_DASHBOARD_IDLE_SECONDS=2 \
  -e LLM_MODEL=deepseek-chat \
  -e HERMES_MODEL_PROVIDER=deepseek \
  -e DEEPSEEK_API_KEY=smoke-provider-key \
  "${IMAGE}" >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:18080/health > "${HEALTH_FILE}"; then
    break
  fi
  sleep 1
done
"${PYTHON_BIN}" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["status"] == "ok" and d["gateway"] == "running" and d["dashboard"] == "stopped", d' "${HEALTH_FILE}"

curl -sS -c "${COOKIE_FILE}" -X POST \
  -d username=admin -d password=smoke-admin-password -d returnTo=/ \
  http://127.0.0.1:18080/login >/dev/null
open_dashboard

for _ in $(seq 1 15); do
  curl -fsS http://127.0.0.1:18080/health > "${HEALTH_FILE}"
  if "${PYTHON_BIN}" -c 'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1]))["dashboard"] == "running" else 1)' "${HEALTH_FILE}"; then
    break
  fi
  sleep 1
done
"${PYTHON_BIN}" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["dashboard"] == "running", d' "${HEALTH_FILE}"

for _ in $(seq 1 15); do
  sleep 1
  curl -fsS http://127.0.0.1:18080/health > "${HEALTH_FILE}"
  if "${PYTHON_BIN}" -c 'import json,sys; raise SystemExit(0 if json.load(open(sys.argv[1]))["dashboard"] == "stopped" else 1)' "${HEALTH_FILE}"; then
    break
  fi
done
"${PYTHON_BIN}" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["dashboard"] == "stopped", d' "${HEALTH_FILE}"

open_dashboard
curl -fsS http://127.0.0.1:18080/health > "${HEALTH_FILE}"
"${PYTHON_BIN}" -c 'import json,sys; d=json.load(open(sys.argv[1])); assert d["dashboard"] == "running", d' "${HEALTH_FILE}"

echo "Hermes image smoke passed"
