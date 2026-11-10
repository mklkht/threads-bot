#!/bin/bash
# Запускати один раз у Task Scheduler (User-defined script, user root) на NAS.
# Виводить публічний ключ — додай його в GitHub: Settings -> Deploy keys -> Add deploy key (read-only).
KEY=/root/.ssh/threads-bot_deploy
mkdir -p /root/.ssh && chmod 700 /root/.ssh
[ -f "$KEY" ] || ssh-keygen -t ed25519 -N "" -C "threads-bot-deploy@nas" -f "$KEY"
grep -q github.com /root/.ssh/known_hosts 2>/dev/null || ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts 2>/dev/null
echo "=== PUBLIC KEY ==="
cat "$KEY.pub"
