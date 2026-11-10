#!/bin/bash
# Запускати один раз у тій самій задачі Task Scheduler, ПІСЛЯ того як deploy key додано в GitHub.
set -e
DIR=/volume1/docker/threads-bot
KEY=/root/.ssh/threads-bot_deploy
SSH_CMD="ssh -i $KEY -o IdentitiesOnly=yes"
[ -d "$DIR/.git" ] || GIT_SSH_COMMAND="$SSH_CMD" git clone git@github.com:mklkht/threads-bot.git "$DIR"
cd "$DIR" && git config core.sshCommand "$SSH_CMD" && echo "OK: $(git log --oneline -1)"
