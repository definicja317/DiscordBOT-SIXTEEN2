#!/usr/bin/env bash
set -Eeuo pipefail

# Render zwykle ustawia $PORT. Jeśli nie — aiohttp wybierze fallback (10000/0)
export HOST="${HOST:-0.0.0.0}"

# Uruchom bota
exec python -u bot.py
