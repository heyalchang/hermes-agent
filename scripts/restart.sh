#!/usr/bin/env bash
# Restart the gateway and/or dashboard, clearing __pycache__ first.
#
# Usage:
#   scripts/restart.sh            # restart both
#   scripts/restart.sh gateway    # restart gateway only
#   scripts/restart.sh dashboard  # restart dashboard only
#
# Runs from the project root directory.

set -euo pipefail
cd "$(dirname "$0")/.."
VENV_PYTHON="./venv/bin/python"

RED='\033[0;31m'
GREEN='\033[0;32m'
DIM='\033[0;90m'
RESET='\033[0m'

TARGET="${1:-all}"

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

  # Clean stale PID file
  rm -f ~/.hermes/gateway.pid 2>/dev/null || true

  echo -e "${GREEN}Starting gateway...${RESET}"
  nohup $VENV_PYTHON -m hermes_cli.main gateway run \
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

# ── Dispatch ───────────────────────────────────────────────────────
case "$TARGET" in
  gateway)
    restart_gateway
    ;;
  dashboard)
    restart_dashboard
    ;;
  all|both)
    restart_gateway
    restart_dashboard
    ;;
  *)
    echo "Usage: scripts/restart.sh [gateway|dashboard|all]"
    exit 1
    ;;
esac

echo -e "${GREEN}Done.${RESET}"
