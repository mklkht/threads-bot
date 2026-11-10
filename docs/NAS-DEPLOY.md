# Деплой на Synology NAS

Репо: `mklkht/threads-bot`. Шлях на NAS: `/volume1/docker/threads-bot`. Deploy key: `threads-bot_deploy`.
Задача Task Scheduler: `threads-bot-deploy`. Проєкт у Container Manager: `threads-bot` (контейнер `threads-bot-bot-1`).

NAS не має SSH — усе йде через Task Scheduler (User-defined script, user `root`, кнопка Run → Action → View Result)
і Container Manager. Детальний загальний процес і граблі — у скілі `nas-github-deploy`; тут лише кроки саме для цього репо.

## 1. Deploy key на NAS

Task Scheduler → Create → Scheduled Task → User-defined script → user `root`, назва `threads-bot-deploy`.
У полі скрипта встав вміст `scripts/nas-setup-1-key.sh`, Run → View Result → скопіюй публічний ключ (`ssh-ed25519 ...`).

GitHub → `mklkht/threads-bot` → Settings → Deploy keys → Add deploy key → встав публічний ключ, **без** "Allow write access".

## 2. Клонування на NAS

У тій самій задачі заміни скрипт на вміст `scripts/nas-setup-2-clone.sh`, Run → View Result.
Очікуваний результат: `OK: <hash> <перший коміт>`.

## 3. Секрети

Створи `/volume1/docker/threads-bot/.env` (через ще один тимчасовий скрипт у тій самій задачі, `cat > .env <<'ENV' ... ENV`,
значення вписуєш сам у текстовому полі DSM перед запуском). Поля: `THREADS_ACCESS_TOKEN`, `LLM_API_KEY`,
за бажанням `ALERT_TELEGRAM_BOT_TOKEN` / `ALERT_TELEGRAM_CHAT_ID` (див. `.env.example`).
Одразу після цього заміни скрипт задачі на деплой (крок 5), щоб токени не лишались у тексті задачі.

## 4. Container Manager

Package Center → Container Manager → Project → Create:
- Name: `threads-bot`
- Path: `/volume1/docker/threads-bot`
- Source: Use existing `compose.yaml`
- Done (перший старт ~1 хв — `pip install` усередині контейнера).

Лог: Container → `threads-bot-bot-1` → Log. Якщо бачиш `connected as @...` і `scheduler started` — усе гаразд.

## 5. Фінальний скрипт задачі (більше не редагується)

```bash
bash /volume1/docker/threads-bot/deploy.sh 2>&1
```

Schedule → Run on the following days → Daily, 00:00; за бажанням додатково "Continue running within the same day" щогодини.

## 6. Деплой

```bash
git push origin main:release
```
Потім Task Scheduler → задача `threads-bot-deploy` → Run → View Result. Очікувано: `deploying <hash> from release`
і рядок `deployed: threads-bot-bot-1 Up ...`. Якщо вивід порожній ("No data") — деплой не знайшов нових комітів, це нормально.

## Зміна конфігурації

- Несекретні налаштування (`DRY_RUN`, розклад, персона, ліміти) — правка `config.env` у репо, коміт, `git push origin main:release`, Run задачі.
- Секрети (`.env` на NAS) — редагуються вручну на NAS; після зміни: Container → `threads-bot-bot-1` → Action → Restart.
