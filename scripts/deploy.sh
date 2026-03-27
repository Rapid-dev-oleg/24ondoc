#!/usr/bin/env bash
set -euo pipefail

# 24ondoc VPS Deploy Script
# Usage: ./scripts/deploy.sh [service]
# Examples:
#   ./scripts/deploy.sh           # deploy all
#   ./scripts/deploy.sh backend   # deploy backend only

APP_DIR="/app/24ondoc"
COMPOSE="docker compose"
SERVICE="${1:-}"

cd "$APP_DIR"

echo "=== $(date '+%Y-%m-%d %H:%M:%S') Starting deploy ==="

# Pull latest code
echo "--- Pulling latest code from main ---"
git pull origin main

if [ -z "$SERVICE" ]; then
    echo "--- Full stack deploy ---"
    $COMPOSE pull
    $COMPOSE build backend
    $COMPOSE up -d
else
    echo "--- Deploying service: $SERVICE ---"
    if [ "$SERVICE" = "backend" ]; then
        $COMPOSE build backend
    fi
    $COMPOSE up -d --no-deps "$SERVICE"
fi

# Wait and check health
echo "--- Checking service health ---"
sleep 15
$COMPOSE ps

# Verify backend health
if $COMPOSE ps backend 2>/dev/null | grep -q "healthy"; then
    echo "=== Backend: HEALTHY ==="
else
    echo "!!! WARNING: Backend may not be healthy yet. Check logs: docker compose logs backend"
fi

echo "=== Deploy completed at $(date '+%Y-%m-%d %H:%M:%S') ==="
