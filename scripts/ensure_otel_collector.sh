#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRACKER_ROOT="${CODEX_TOKEN_TRACKER_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CONFIG_FILE="${CODEX_TOKEN_TRACKER_COLLECTOR_CONFIG:-$TRACKER_ROOT/ops/otel/otel-codex.yaml}"
CONTAINER_NAME="${CODEX_TOKEN_TRACKER_CONTAINER:-codex-otel-collector}"
IMAGE="${CODEX_TOKEN_TRACKER_COLLECTOR_IMAGE:-otel/opentelemetry-collector-contrib:latest}"
DATA_HOME="${XDG_DATA_HOME:-${HOME:-/tmp}/.local/share}"
DATA_DIR="${CODEX_TOKEN_TRACKER_DATA_DIR:-$DATA_HOME/codex-token-tracker}"
RUN_UID="${CODEX_TOKEN_TRACKER_UID:-$(id -u)}"
RUN_GID="${CODEX_TOKEN_TRACKER_GID:-$(id -g)}"

warn() {
  printf 'codex-token-tracker: %s\n' "$*" >&2
}

RECREATE=0
case "${1:-}" in
  --recreate)
    RECREATE=1
    shift
    ;;
  --help|-h)
    cat <<EOF
Usage: $0 [--recreate]

Ensure the local Codex OpenTelemetry collector container is running.

Options:
  --recreate  Remove and recreate the collector container before starting it.
              Use this after changing collector config or deleting the JSONL file.
EOF
    exit 0
    ;;
esac

if [[ $# -gt 0 ]]; then
  warn "unknown argument: $1"
  exit 0
fi

if ! command -v docker >/dev/null 2>&1; then
  warn "docker is not available; collector not started"
  exit 0
fi

if ! docker_info_error="$(docker info 2>&1 >/dev/null)"; then
  if printf '%s' "$docker_info_error" | grep -qiE 'permission denied|access denied|not authorized|unauthorized'; then
    warn "docker is installed but not accessible without sudo; collector not started"
    warn "make plain 'docker info' work for this user because Codex hooks cannot answer sudo prompts"
  else
    warn "docker is not running or not reachable; collector not started"
  fi
  exit 0
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  warn "collector config not found: $CONFIG_FILE"
  exit 0
fi

state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)"

if [[ "$RECREATE" == 1 && -n "$state" ]]; then
  if ! docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1; then
    warn "failed to remove existing collector container; collector not started"
    exit 0
  fi
  state=""
fi

case "$state" in
  running)
    exit 0
    ;;
  created|paused|exited)
    if ! docker start "$CONTAINER_NAME" >/dev/null; then
      warn "failed to start existing collector container; collector not started"
    fi
    exit 0
    ;;
  restarting|dead)
    if ! docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1; then
      warn "failed to remove unhealthy collector container; collector not started"
      exit 0
    fi
    ;;
  removing)
    warn "collector container is currently being removed; collector not started"
    exit 0
    ;;
esac

mkdir -p "$DATA_DIR"
touch "$DATA_DIR/codex-otel.jsonl" 2>/dev/null || true

if ! docker run \
  --detach \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --user "$RUN_UID:$RUN_GID" \
  --publish 127.0.0.1:4318:4318 \
  --volume "$CONFIG_FILE:/etc/otelcol-contrib/config.yaml:ro" \
  --volume "$DATA_DIR:/data" \
  "$IMAGE" >/dev/null; then
  warn "failed to create collector container; collector not started"
fi
