#!/bin/bash
# Тонка обгортка: стежить за гілкою release, підтягує нове і передає керування post-deploy.sh.
# Логіку деплою тримаємо в scripts/post-deploy.sh, щоб саму цю обгортку більше не редагувати.
cd "$(dirname "$0")" || exit 1
# Synology створює @eaDir у .git, коли теку відкривають по SMB.
# git fetch тоді падає на refs/heads/@eaDir/*@SynoEAStream.
find .git -type d -name '@eaDir' -prune -exec rm -rf {} + 2>/dev/null || true
BRANCH="${DEPLOY_BRANCH:-release}"
git fetch -q origin "$BRANCH" || exit 1
if [ "$(git rev-parse HEAD)" = "$(git rev-parse FETCH_HEAD)" ]; then exit 0; fi
echo "$(date) deploying $(git rev-parse --short FETCH_HEAD) from $BRANCH"
git checkout -q -B "$BRANCH" FETCH_HEAD || exit 1
exec bash ./scripts/post-deploy.sh
