# threads-bot

Автономний бот для одного Threads-акаунта (Meta Threads Graph API):

- два автопости на день за розкладом (`POST_CRON`, за замовчуванням 09:30 і 19:30 Europe/Kyiv), текст генерує LLM у заданій персоні;
- опрацювання коментарів під останніми постами: LLM класифікує кожен коментар як `reply` / `ignore` / `hide_spam`;
- оновлення long-lived токена приблизно раз на 25 днів;
- стан (пости, відповіді, токен, конфіг) зберігається в SQLite (`data/bot.db`).

Налаштування розділені на два файли:

- `config.env` — несекретні (розклад, персона, ліміти, модель). Уже в git, редагуй напряму.
- `.env` — секрети (токен Threads, ключ LLM, Telegram). Не в git, створюється локально з `.env.example`.

## Запуск

```bash
cp .env.example .env
# заповни THREADS_ACCESS_TOKEN і LLM_API_KEY
docker compose -f compose.yaml up -d
```

`config.env` за замовчуванням має `DRY_RUN=true`: бот повністю проходить пайплайн, але нічого не публікує, а в логах видно,
що саме він зробив би. Коли переконаєшся, що тексти й рішення адекватні, постав `DRY_RUN=false` у `config.env` і задеплой знову.

Деплой на Synology NAS із GitHub — `docs/NAS-DEPLOY.md`.

## Перевірка

```bash
docker compose -f compose.yaml logs -f
docker compose -f compose.yaml exec bot python -c "import os,time; print(time.time()-os.path.getmtime('/app/data/heartbeat'))"
```

Пауза без зупинки контейнера: `touch data/PAUSE` (зняти: `rm data/PAUSE`). Поки файл існує, автопости й відповіді не виконуються.

## Тести

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```

## Структура

- `bot.py` — точка входу, планувальник, graceful shutdown;
- `app/config.py` — налаштування (pydantic-settings, `.env`);
- `app/store.py` — SQLite-шар (aiosqlite);
- `app/threads_api.py` — клієнт Threads API з retry і логуванням;
- `app/llm.py`, `app/prompts.py` — генерація постів і класифікація коментарів;
- `app/filters.py` — перевірки тексту перед публікацією;
- `app/alerts.py` — логи і опційні Telegram-алерти;
- `app/jobs/` — `autopost`, `check_replies`, `refresh_token`.
