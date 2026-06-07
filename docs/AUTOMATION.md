# Автоматизация для 10к-аккаунтного потока

Когда аккаунты постоянно меняются (приходят через intake API, банятся, новые
заменяют старые), нужно чтобы система сама подхватывала изменения без
ручного вмешательства. Этот документ описывает что автоматизировано.

## Что автоматически работает после `docker compose up -d`

| Что делает | Где работает | Триггер |
|---|---|---|
| Принимает новые аккаунты (sync или async) | `intake` (FastAPI) | HTTP POST |
| Обрабатывает async-intake в фоне | `intake-worker` | каждые 30 сек |
| Создаёт драфты для новых аккаунтов | `scheduler` task `auto_generate` | каждые 15 мин |
| Рассылает по drafts с шардингом | `sender-0..N` | непрерывно |
| Ловит STOP-ответы → blocklist | `optout` | polling каждые 10 мин |
| Чистит старые send_log/drafts | `scheduler` task `auto_cleanup` | каждый час |
| Прунит мёртвые контакты | `scheduler` task `auto_prune` | раз в сутки |
| Мониторит здоровье + алерты в TG | `scheduler` task `auto_health` | каждые 5 мин |
| Heartbeat от каждого шарда | `sender` (внутри) | каждые 60 сек |

## Поток нового аккаунта от warmup до рассылки

```
1. Warmup-пайплайн на твоей стороне готовит акк
2. POST /api/accounts/intake?process_async=true  (200 за <100ms)
3. Account записан в БД, intake_status='pending_intake'
4. intake-worker через 30 сек видит, забирает (SKIP LOCKED)
5. Telethon connect + email-verify (если надо) + GetNearestDc + fetch dialogs
6. intake_status='done', status='active'
7. Scheduler через 15 мин видит активный акк без pending драфтов
8. Создаёт ему драфты (один на каждого контакта с history) с DEFAULT_ATTACH
9. Sender-shard этого аккаунта через 30-120 сек подбирает из очереди
10. Начинает слать (account-centric: коннект, шлёт все 200 драфтов с
    паузами 30-90 сек, дисконнект). Сообщения уходят получателям.
```

**Время от intake до первого отправленного сообщения: ~17-30 минут** в среднем
(зависит от того где попал момент next-auto_generate-цикла).

Если важна латентность от intake до старта рассылки — снижай
`AUTO_GENERATE_INTERVAL` (например, до 300 секунд = 5 минут).

## Что делать оператору (минимум)

После настройки `.env` (см. README) — практически ничего ежедневно.

**Раз в день (или реже):**
- Глянуть алерты в Telegram-боте (если что-то горит — туда придёт)
- `make psql` → проверить `SELECT status, COUNT(*) FROM accounts GROUP BY status`

**Когда:**
- Меняешь оффер → обновить `DEFAULT_ATTACH` в `.env`, рестартануть scheduler
- Аккаунты массово банятся → проверить причины через `status_reason`,
  возможно поправить лимиты или паузы

**Не надо:**
- Запускать generate руками (scheduler это делает)
- Чистить БД (scheduler чистит)
- Перезапускать sender'ы (они restart: unless-stopped)
- Следить за памятью (account-centric pattern с дисконнектами не течёт)

## Время полной рассылки на 10 000 аккаунтов

Базовые цифры:
- 10 000 аккаунтов × ~200 контактов = **2 000 000 сообщений**
- Темп одного аккаунта: 1 msg/мин (`MIN_DELAY=30, MAX_DELAY=90`)
- Active-окно: 14 часов/сутки (`09:00-23:00` Asia/Tashkent)

Параллелизм:
- `WORKER_COUNT=100` шард × `SENDER_PARALLEL_ACCOUNTS=20` = **2 000 параллельных**
- 2000 × 1 msg/min = **120 000 msg/час** sustained

Расчёт wall-clock:
- 2 000 000 / 120 000 = **~16.7 часов чистых отправок**
- При 14ч active/день → **~1.2 дня wall-clock** на полную рассылку

### Сценарии скорости

| Профиль | min/max delay | parallel | Throughput | На 2M msg |
|---|---|---|---|---|
| **Conservative** | 60-180 сек | 10 | 30k/час | ~5 дней |
| **Default** | 30-90 сек | 20 | 120k/час | **~1.2 дня** |
| **Aggressive** | 15-45 сек | 30 | 240k/час | ~16 часов |
| **Yolo** | 10-30 сек | 50 | 600k/час | ~7 часов* |

\* На «Yolo» антифрод реагирует — реальный throughput будет ниже из-за FloodWait.

### Когда аккаунты постоянно ротируются (steady state)

В реальности новые аккаунты приходят через intake throughout the day, старые
банятся. Steady-state метрики:

```
Throughput: ~100-120k msg/час (с дефолтными настройками)
Per-account: ~200 контактов / (60 минут × 14 часов) → почти 4 дня
            на полную обработку одного акк, если он попал в самый низ
            очереди

Но: account-centric pattern означает что когда акк начинает обрабатываться,
он закрывает свой queue за 3.3 часа (200 × 60 сек). Просто слотов на 2000
параллельных не всегда хватает, очередь acc-of-acc стоит в shard.
```

## Память при ротации — как не упереться

Проблема: 10к акк × 40 MB Telethon-объекта = 400 GB если все висят в памяти.

Решение которое уже в коде:

1. **Account-centric: connect-process-disconnect** — TelegramClient живёт
   только пока шлёшь queue этого акк. После всех 200 драфтов — disconnect.
   В памяти одновременно: `WORKER_COUNT × SENDER_PARALLEL_ACCOUNTS` коннектов.

2. **Polling-based optout** — НЕ держит persistent connections для всех 10к.
   Раз в 10 мин подключается, читает unread, отключается.

3. **Intake-worker сам управляет своими коннектами** — параллельность
   ограничена `INTAKE_WORKER_PARALLEL=10`. Никакого глобального пула.

4. **Postgres connections** через PgBouncer (опционально) — мультиплексирует
   тысячи клиентов на 50 реальных коннектов.

5. **session_string scrubbing** в auto_prune — у банкнутых аккаунтов
   очищается session (может быть 100 KB+) чтобы не забивать память.

**Итоговая память при 10к акк с дефолтом:**

| Что | RAM |
|---|---|
| 100 sender-шард × 20 параллельных × 40 MB | **80 GB** |
| Optout polling (max 10 одновременных connects на шард) | 1-2 GB |
| Intake-worker × 10 parallel | 0.4 GB |
| Scheduler | 0.1 GB |
| Postgres (с tuning под 10к) | 4-8 GB |
| PgBouncer | 0.1 GB |
| **Всего на сервер при single-node** | **~90 GB** |

Берёшь сервер 128 GB, оставляет 30+ GB запаса для OS и пиков.

При multi-server: senders на 2-3 серверах по 32-64 GB каждый, БД отдельно.

## Что НЕ автоматизировано (и почему)

| Не автомат | Почему |
|---|---|
| Перезапуск стучащего sender контейнера | Docker уже делает (restart: unless-stopped) — alert + ручное вмешательство если что |
| Авто-замена банкнутого акк на новый | Это бизнес-логика: какой парк где брать. Делается через intake-стрим из warmup |
| Изменение DEFAULT_ATTACH налету | Нужно рестартовать scheduler после правки .env. Простой вариант. |
| Auto-scaling шард | Шарды по `account.id % N` — менять N означает миграцию всех данных. Делается вручную: gen_compose + restart |
| Backup БД | Нужно настроить через cron хост-машины (pg_dump → S3). Не вшито чтобы не зависеть от конкретного storage |

## Базовый troubleshooting через psql

```sql
-- Что в очереди сейчас
SELECT status, COUNT(*) FROM drafts GROUP BY status;

-- Какие аккаунты застряли в intake
SELECT name, intake_status, status, status_reason
FROM accounts WHERE intake_status != 'done' LIMIT 20;

-- Шарды которые давно не пинговали (потенциально мёртвые)
SELECT shard_id, last_beat, sent_count_total,
       (NOW() - last_beat) AS stale_for
FROM shard_heartbeats
ORDER BY last_beat ASC LIMIT 10;

-- Последние auto_generate-запуски
SELECT id, started_at, finished_at, accounts_touched, drafts_created
FROM generate_runs ORDER BY id DESC LIMIT 10;

-- Активность за последний час
SELECT COUNT(*) FROM send_log
WHERE sent_at > NOW() - INTERVAL '1 hour' AND success = true;

-- Размер БД
SELECT pg_size_pretty(pg_database_size(current_database()));

-- Топ таблиц по размеру
SELECT relname, pg_size_pretty(pg_total_relation_size(relid))
FROM pg_catalog.pg_statio_user_tables
ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;
```
