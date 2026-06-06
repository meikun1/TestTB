# Архитектура под 500+ аккаунтов

## Сервисы

```
┌──────────────────────────────────────────────────────────┐
│ PostgreSQL  (state)         Redis  (pub/sub координация) │
└──┬───────────────────┬───────────────────────────────────┘
   │                   │
   │   ┌───────────────┴──────────────┐
   │   │                              │
   │   │  ┌──── dashboard (1)         │
   │   │  │     • HTML + JSON API     │
   │   │  │     • intake → publish    │
   │   │  └─                          │
   │   │  ┌──── fetcher (1)           │
   │   │  │     • раз в 24ч на акк    │
   │   │  └─                          │
   │   │  ┌──── healthcheck (1)       │
   │   │  │     • метрики → алерты    │
   │   │  └─                          │
   │   │  ┌──── sender-0..N (N=10)    │  ← N процессов в docker-compose
   │   └──┘     • shard_id = id % N   │
   │            • subscribe Redis     │
   │            • SELECT FOR UPDATE   │
   │              SKIP LOCKED         │
   │
   └─ 500 аккаунтов
      каждый ~30-50 МБ RAM в воркере его шарда
```

## Шардинг

`WORKER_COUNT=10` в `.env` означает: 10 sender-процессов, каждый держит
~10% пула. Распределение по `account.id % WORKER_COUNT`. При 500 акк = ~50 на воркер.

Зачем шардинг:
- Один процесс с 500 telethon-клиентами = ~20 GB RAM + GIL boundary
- 10 процессов по ~50 клиентов = 2-3 GB RAM на процесс, GIL не давит
- Если один воркер крашится — остальные продолжают работать

**SKIP LOCKED** — несколько воркеров одновременно дёргают очередь, но
Postgres гарантирует что один драфт возьмёт ровно один воркер. SQLite такого
не умеет, поэтому для 500 акк он **отключён**.

## Очередь, отправка, вложение

`Draft` теперь содержит:

| Поле | Что |
|---|---|
| `attachment_kind` | `photo` / `video` / `audio` / `voice` / `document` / `link` / NULL |
| `attachment_ref` | путь к файлу (от корня проекта) или URL для `link` |
| `attachment_caption` | подпись к медиа (опционально) |
| `attachment_delay_seconds` | сколько секунд между текстом и вложением (0 = ?? используем дефолт из env) |

Логика отправки:

1. Sender имитирует typing → отправляет текст
2. Если есть вложение → пауза `attachment_delay_seconds` (или [5..30] по умолчанию) → отправка вложения
3. Каждый шаг логируется отдельной записью в `send_log.kind` (`message` / `attachment`)

Это даёт «по-человечески»: написал → подкрепил файлом отдельным сообщением через ~10 секунд.

## Live reload через Redis

Старая схема: воркеры опрашивали БД раз в 5 мин — для 10 воркеров × 500 акк это лишняя нагрузка.

Новая:
- `bus.publish_pool_reload(account_id=N)` — публикация в Redis-канал
- Все sender-воркеры подписаны через `bus.subscribe_pool_reload()`
- На событие получают сигнал и моментально дёргают `pool.refresh()`
- Если `account_id` указан в событии и не принадлежит шарду — воркер игнорирует

Fallback: опрос БД раз в `POOL_RELOAD_INTERVAL_SECONDS` всё ещё работает на случай если Redis недоступен.

Когда срабатывает:
- `POST /api/accounts/intake` (новая/обновлённая сессия)
- Дашборд при enable/disable (в будущем)
- Sender при `mark_banned` — другие воркеры узнают что аккаунт горит

## Фоновый fetcher

Раньше fetcher был CLI-командой. Для 500 акк это нерабочая модель.

Теперь — отдельный сервис `tg-fetcher`:
- При старте смотрит `last_fetched_at` у каждого аккаунта
- Аккаунты с `last_fetched_at IS NULL OR last_fetched_at < now - 24h` идут в очередь
- Обновляет историю последних 200 сообщений по контакту
- Спит между аккаунтами 5 сек чтобы не давить Telegram
- После прохода всех — спит `FETCHER_INTERVAL_HOURS` часов

Можно тюнить `FETCHER_INTERVAL_HOURS=12` чтобы скоринг был свежее, или `=48` если нагрузка слишком высокая.

## Healthcheck + алерты

Сервис `tg-healthcheck` раз в `HEALTH_CHECK_INTERVAL_SECONDS` (по умолчанию 5 мин) считает:

| Метрика | Что значит | Порог по умолчанию |
|---|---|---|
| active ratio | доля active-аккаунтов в `enabled` | алерт если < 70% |
| burn rate | сколько аккаунтов получили PeerFlood за час | алерт если > 5% |
| reply rate | доля отправок где получили ответ за 48ч | алерт если < 10% (на выборке ≥50) |

Алерт шлётся **в твой Telegram через бота**. Настройка:

1. Создай бота через [@BotFather](https://t.me/BotFather) → получи `ALERT_BOT_TOKEN`
2. Напиши боту любое сообщение → открой `https://api.telegram.org/bot<TOKEN>/getUpdates` → найди `chat.id` → положи в `ALERT_CHAT_ID`
3. (или) Добавь бота в группу → `chat.id` группы (с минусом)

Cooldown между одинаковыми алертами — 30 минут, чтобы не спамить.

## Постгрес: первый запуск

При первом `docker compose up -d`:
1. Контейнер `postgres` создаст пустую БД
2. Любой из app-сервисов (dashboard/fetcher/healthcheck) при первом обращении вызовет `init_db()` → SQLAlchemy создаст схему

Миграции схемы (когда захочешь добавить поля) — пока через ручной `DROP+CREATE` либо подключить Alembic (отдельно).

## Запуск (cheatsheet)

```bash
git clone <repo> && cd <repo>
cp .env.example .env
nano .env                              # TG_API_ID, TG_API_HASH, ANTHROPIC_API_KEY,
                                        # ALERT_BOT_TOKEN, ALERT_CHAT_ID,
                                        # DASHBOARD_TOKEN на длинный, проч.

docker compose build
docker compose up -d
docker compose logs -f --tail=100

# залить парк аккаунтов через intake API (стрим)
# или CSV-импортом:
docker compose run --rm dashboard python -m src.accounts.bulk_import /app/accounts.csv

# первый разовый fetch:
docker compose run --rm fetcher python -m src.fetcher.fetch_dialogs --all

# скоринг + генерация:
docker compose run --rm dashboard python -m src.scoring.score_contacts
docker compose run --rm dashboard python -m src.generator.generate_drafts \
    --top 10 --attach photo:attachments/offer.png --attach-caption "посмотри"

# дальше всё крутится фоном, проверять — на http://server:8080
```

## Когда упрёшься в этот предел

10 sender-воркеров × 50 акк × ~50-80 msg/день = ~25k-40k сообщений/день максимум. Если нужно больше:

- Увеличить `WORKER_COUNT` (продублировать `sender-N` в docker-compose)
- Расселить воркеры по нескольким серверам (через PG_HOST + REDIS_URL)
- Перейти на pgbouncer для пула соединений
- Свой LLM-инференс (Anthropic API на 1000+ rps уже больно)
