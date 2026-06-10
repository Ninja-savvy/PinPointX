#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# ─────────────────────────────────────────────────────────────────────────────
#  PinPointX Startup Script
#  Usage: ./startup.sh
# ─────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[PinPointX]${NC} $*"; }
ok()   { echo -e "${GREEN}[PinPointX]${NC} $*"; }
warn() { echo -e "${YELLOW}[PinPointX]${NC} $*"; }
err()  { echo -e "${RED}[PinPointX]${NC} $*" >&2; }

# ─── Load .env ────────────────────────────────────────────────────────────────
if [[ -f .env ]]; then
    set -a
    source .env
    set +a
fi

# Resolve paths — these must match what docker-compose.yml mounts.
# EMBA_LOG_DIR and EMBA_DIFF_LOG_DIR are mounted into the container at
# /app/logs/emba and /app/logs/emba_diff respectively.
# EMBA_FIRMWARE_DIR is the host path that emba_service.py uses when
# translating /app/uploads/firmware/* paths sent by the container.
EMBA_PATH="${EMBA_PATH:-/opt/emba}"
EMBA_LOG_DIR="${EMBA_LOG_DIR:-$(pwd)/emba_logs}"
EMBA_DIFF_LOG_DIR="${EMBA_DIFF_LOG_DIR:-$(pwd)/emba_diff_logs}"
EMBA_FIRMWARE_DIR="${EMBA_FIRMWARE_DIR:-$(pwd)/uploads/firmware}"

# ─── Pre-flight checks ────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    err "Docker not found. Please install Docker first."
    exit 1
fi

if ! docker compose version &>/dev/null; then
    err "Docker Compose v2 not found. Please install Docker Compose v2."
    exit 1
fi

# ─── Create host-side directories ────────────────────────────────────────────
# logs/         — emba_service.py stdout/stderr log
# uploads/      — firmware uploads written by container, read by emba_service
# EMBA_LOG_DIR  — emba scan output, mounted into container at /app/logs/emba
# EMBA_DIFF_LOG_DIR — diff scan output, mounted at /app/logs/emba_diff
# EMBA_FIRMWARE_DIR — firmware staging area used by emba_service translate path
mkdir -p \
    logs \
    uploads/firmware \
    uploads/firmware_diff \
    uploads/hardware \
    uploads/tools \
    "$EMBA_LOG_DIR" \
    "$EMBA_DIFF_LOG_DIR" \
    "$EMBA_FIRMWARE_DIR"

export EMBA_PATH EMBA_LOG_DIR EMBA_DIFF_LOG_DIR EMBA_FIRMWARE_DIR

# ─── Pull latest image ────────────────────────────────────────────────────────
log "Pulling latest PinPointX image..."
if docker compose pull 2>/dev/null; then
    ok "Image up to date."
else
    warn "Image pull failed — will use locally cached image if available."
fi

# ─── Kill any existing emba_service.py on the host ───────────────────────────
# Handles upgrades: if an old version is already running, stop it cleanly
# before starting the new one. Uses SIGTERM first, SIGKILL as fallback.
if pgrep -f "python.*emba_service\.py" &>/dev/null; then
    OLD_PID=$(pgrep -f "python.*emba_service\.py" | head -1)
    warn "Found existing emba_service.py (PID $OLD_PID). Stopping it for upgrade..."
    kill "$OLD_PID" 2>/dev/null || true
    for i in $(seq 1 8); do
        kill -0 "$OLD_PID" 2>/dev/null || break
        sleep 1
    done
    # Force kill if still alive after 8 seconds
    if kill -0 "$OLD_PID" 2>/dev/null; then
        warn "Process did not stop cleanly. Sending SIGKILL..."
        kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    ok "Old emba_service.py stopped."
fi

# ─── Cleanup handler ─────────────────────────────────────────────────────────
EMBA_PID=""
cleanup() {
    echo ""
    log "Shutting down PinPointX..."
    if [[ -n "$EMBA_PID" ]] && kill -0 "$EMBA_PID" 2>/dev/null; then
        log "Stopping EMBA service (PID $EMBA_PID)..."
        kill "$EMBA_PID" 2>/dev/null || true
        wait "$EMBA_PID" 2>/dev/null || true
    fi
    log "Stopping Docker Compose..."
    docker compose down 2>/dev/null || true
    ok "PinPointX stopped."
}
trap 'cleanup; exit 130' INT TERM

# ─── Start emba_service.py on the host (Linux only) ──────────────────────────
OS="$(uname -s)"

if [[ "$OS" == "Linux" ]]; then
    if [[ ! -d "$EMBA_PATH" ]]; then
        warn "EMBA not found at $EMBA_PATH"
        warn "Firmware analysis will be unavailable until EMBA is installed."
        warn "Install: git clone https://github.com/e-m-b-a/emba.git $EMBA_PATH"
    fi

    log "Starting EMBA host agent on port 5002..."
    # sudo -E preserves the exported env vars (EMBA_LOG_DIR etc.)
    # nohup keeps it running if the terminal closes
    sudo -E nohup python3 emba_service.py > logs/emba_service.log 2>&1 &
    EMBA_PID=$!

    log "Waiting for EMBA service to be ready (up to 15s)..."
    READY=0
    for i in $(seq 1 15); do
        if curl -fsS http://127.0.0.1:5002/health >/dev/null 2>&1; then
            READY=1
            break
        fi
        sleep 1
    done

    if [[ "$READY" -eq 1 ]]; then
        ok "EMBA service is ready (PID $EMBA_PID)"
    else
        warn "EMBA service did not respond within 15s."
        warn "Check logs/emba_service.log for details."
        warn "Continuing anyway — firmware analysis may fail until it starts."
    fi
else
    warn "Non-Linux OS detected ($OS). EMBA firmware analysis is unavailable."
    warn "PCB analysis, hardware analysis, and all other features work normally."
fi

# ─── Start the app container ─────────────────────────────────────────────────
log "Starting PinPointX app container..."
log "Press Ctrl+C to stop everything."
# No --build: image was pulled above from Docker Hub
docker compose up