#!/bin/bash
set -e
cd "$(dirname "$0")/.."
if command -v docker-compose >/dev/null 2>&1; then DC="docker-compose"; else DC="docker compose"; fi
$DC down --remove-orphans
$DC up -d
docker ps --filter name=threads-bot --format 'deployed: {{.Names}} {{.Status}}'
