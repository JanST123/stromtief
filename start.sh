#!/usr/bin/env bash
# Quickstart script for EOS Smart Charge Planner UI

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${SMART_CHARGE_PORT:-8505}"
EOS_URL="${EOS_API_URL:-http://127.0.0.1:8503}"

echo "=========================================="
echo "⚡️ Starting EOS Smart Charge Planner UI"
echo "🌐 UI Port:    http://127.0.0.1:${PORT}"
echo "🔌 EOS Server: ${EOS_URL}"
echo "=========================================="

export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="localhost,127.0.0.1,::1"

if [ -f "/tmp/eos-venv/bin/python" ]; then
    PYTHON_BIN="/tmp/eos-venv/bin/python"
elif [ -f ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
else
    PYTHON_BIN="python3"
fi

exec "$PYTHON_BIN" app.py
