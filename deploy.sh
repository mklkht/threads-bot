#!/bin/bash
# Тонка обгортка: стежить за гілкою release, підтягує нове і передає керування post-deploy.sh.
# Логіку деплою тримаємо в scripts/post-deploy.sh, щоб саму цю обгортку більше не редагувати.
cd "$(dirname "$0")" || exit 1
BRANCH="${DEPLOY_BRANCH:-release}"
git fetch -q origin "$BRANCH" || exit 1
if [ "$(git rev-parse HEAD)" = "$(git rev-parse FETCH_HEAD)" ]; then exit 0; fi
echo "$(date) deploying $(git rev-parse --short FETCH_HEAD) from $BRANCH"
git checkout -q -B "$BRANCH" FETCH_HEAD || exit 1
exec bash ./scripts/post-deploy.sh
