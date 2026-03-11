#!/usr/bin/env bash
# Restart the gateway and/or dashboard, clearing __pycache__ first.
#
# Usage:
#   scripts/restart.sh              # restart gateway + dashboard
#   scripts/restart.sh gateway      # restart gateway only
#   scripts/restart.sh dashboard    # restart dashboard only
#   scripts/restart.sh bridges      # restart WhatsApp bridge only
#   scripts/restart.sh all          # restart everything including bridges
#
# Runs from the project root directory.

set -euo pipefail
cd "$(dirname "$0")/.."
VENV_PYTHON="./venv/bin/python"

RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[0;90m'
RESET='\033[0m'

TARGET="${1:-core}"

WHATSAPP_BRIDGE_DIR="./scripts/whatsapp-bridge"
WHATSAPP_SESSION_DIR="$HOME/.hermes/whatsapp/session"

# Signal bridge (Docker container — signal-cli-rest-api)
# Uncomment SIGNAL_ENABLED when ready to use
# SIGNAL_ENABLED=1
SIGNAL_CONTAINER_NAME="signal-cli-rest-api"
SIGNAL_IMAGE="bbernhard/signal-cli-rest-api:latest"
SIGNAL_DATA_DIR="$HOME/.hermes/signal/data"
SIGNAL_PORT="8080"

# ── Clear __pycache__ ──────────────────────────────────────────────
echo -e "${DIM}Clearing __pycache__...${RESET}"
find . -path ./venv -prune -o -path ./node_modules -prune -o \
  -name '__pycache__' -type d -print -exec rm -rf {} + 2>/dev/null || true

# ── Gateway ────────────────────────────────────────────────────────
restart_gateway() {
  echo -e "${DIM}Stopping gateway...${RESET}"
  # Use the CLI's kill logic if available, fall back to pkill
  if $VENV_PYTHON -m hermes_cli.main gateway stop 2>/dev/null; then
    sleep 1
  else
    pkill -f "hermes_cli.main gateway run" 2>/dev/null || true
    sleep 1
  fi

  echo -e "${GREEN}Starting gateway...${RESET}"
  nohup $VENV_PYTHON -m hermes_cli.main gateway run --replace \
    > ~/.hermes/logs/gateway-stdout.log 2>&1 &
  local pid=$!
  echo -e "${GREEN}Gateway started (PID $pid)${RESET}"
}

# ── Dashboard ──────────────────────────────────────────────────────
restart_dashboard() {
  echo -e "${DIM}Stopping dashboard...${RESET}"
  pkill -f "dashboard.server.*run_server" 2>/dev/null || true
  sleep 1

  echo -e "${GREEN}Starting dashboard...${RESET}"
  nohup $VENV_PYTHON -c \
    "import sys; sys.path.insert(0, '.'); from dashboard.server import run_server; run_server(port=18799)" \
    > ~/.hermes/logs/dashboard-stdout.log 2>&1 &
  local pid=$!
  echo -e "${GREEN}Dashboard started (PID $pid) → http://localhost:18799${RESET}"
}

# ── Bridges ────────────────────────────────────────────────────────
restart_bridges() {
  # WhatsApp bridge (Node.js / Baileys)
  if [ -f "$WHATSAPP_BRIDGE_DIR/bridge.js" ]; then
    echo -e "${DIM}Stopping WhatsApp bridge...${RESET}"
    pkill -f "whatsapp-bridge/bridge.js" 2>/dev/null || true
    sleep 2

    echo -e "${GREEN}Starting WhatsApp bridge...${RESET}"
    nohup node "$WHATSAPP_BRIDGE_DIR/bridge.js" \
      --port 3000 \
      --session "$WHATSAPP_SESSION_DIR" \
      --mode bot \
      > ~/.hermes/logs/whatsapp-bridge-stdout.log 2>&1 &
    local pid=$!
    echo -e "${GREEN}WhatsApp bridge started (PID $pid) → localhost:3000${RESET}"
  else
    echo -e "${DIM}WhatsApp bridge not found, skipping${RESET}"
  fi

  # Signal bridge (Docker — signal-cli-rest-api)
  if [ "${SIGNAL_ENABLED:-}" = "1" ]; then
    echo -e "${DIM}Stopping Signal bridge...${RESET}"
    docker stop "$SIGNAL_CONTAINER_NAME" 2>/dev/null || true
    docker rm "$SIGNAL_CONTAINER_NAME" 2>/dev/null || true
    sleep 1

    mkdir -p "$SIGNAL_DATA_DIR"

    echo -e "${GREEN}Starting Signal bridge...${RESET}"
    docker run -d \
      --name "$SIGNAL_CONTAINER_NAME" \
      --restart unless-stopped \
      -p "${SIGNAL_PORT}:8080" \
      -v "${SIGNAL_DATA_DIR}:/home/.local/share/signal-cli" \
      "$SIGNAL_IMAGE"
    echo -e "${GREEN}Signal bridge started → localhost:${SIGNAL_PORT}${RESET}"
  else
    echo -e "${DIM}Signal bridge disabled (set SIGNAL_ENABLED=1 in script to enable)${RESET}"
  fi
}

# ── Dispatch ───────────────────────────────────────────────────────
case "$TARGET" in
  gateway)
    restart_gateway
    ;;
  dashboard)
    restart_dashboard
    ;;
  bridges)
    restart_bridges
    ;;
  core)
    restart_gateway
    restart_dashboard
    ;;
  all)
    restart_gateway
    restart_dashboard
    restart_bridges
    ;;
  *)
    echo "Usage: scripts/restart.sh [gateway|dashboard|bridges|core|all]"
    exit 1
    ;;
esac

echo -e "${GREEN}Done.${RESET}"
