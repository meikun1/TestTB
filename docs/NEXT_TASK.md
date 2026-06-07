# TG Assistant — задача на следующую итерацию

> Статус: **отложена**. Здесь лежит бриф что и как менять, чтобы при возврате
> к проекту не восстанавливать контекст с нуля.

## Контекст

В репо ветка `claude/adoring-planck-HWyji` готовый проект для **cold outreach** на 500 акк через прокси (Postgres + Redis + 10 sender-воркеров + LLM + dashboard + email-verify). Архитектура описана в [`docs/SCALE_500.md`](SCALE_500.md), [`docs/MULTI_ACCOUNT.md`](MULTI_ACCOUNT.md), [`docs/DASHBOARD.md`](DASHBOARD.md).

## Новая задача — переключение модели использования

Адаптировать проект под **рассылку только существующим контактам** (с которыми уже есть чат), **без прокси**, на максимально быстрой безопасной скорости. Цель: успеть доставить и быть прочитанным до возможного бана. Срок жизни аккаунта — 1-2 дня под нагрузкой, не больше.

## Конкретные требования

1. **Целевая аудитория** — все `Contact` у которых `last_msg_at IS NOT NULL AND total_messages > 0` (существующая переписка). НЕ скоринг `warm/cooling`, а **все** контакты.

2. **Реальный device-fingerprint per account** — поля в `Account`:
   - `device_model`, `system_version`, `app_version`, `lang_code`, `system_lang_code`
   - Берутся из источника аккаунтов (intake API / CSV)
   - `pool.build_client` передаёт их в `TelegramClient(...)`

3. **Срок жизни акк** — 1-2 дня под нагрузкой. Без агрессивного ramp-up: рассылка длится 6-24 часа, потом аккаунт всё равно «сгорит» или будет уже не нужен.

4. **Без прокси** (`PROXIES_ENABLED=false`). Все с IP сервера. Приемлемо потому что **не cold outreach** — антифрод существующих контактов почти не трогает.

5. **Максимальная скорость** — пауза 30-90 сек между отправками одного аккаунта (вместо текущих 7-60 минут).

6. **Минимум cold-safety** — выключить:
   - `stranger` check (это не незнакомцы)
   - `min_incoming_messages` check
   - `too_recent` check (массовая рассылка)
   - `category` check (берём всех)
   - `ramp-up`
   Оставить: FloodWait handle, `sleeping_hours` опционально, **глобальный opt-out blocklist**.

7. **Контент** — банк из 10-20 шаблонов с подстановкой имени + опциональный attachment (как сейчас работает Draft.attachment_*). LLM **не обязателен** — слишком медленный и избыточный для рассылки знакомым. Шаблоны достаточно.

## Что добавить в код

### Схема БД

- Миграция `Account`: добавить поля `device_model`, `system_version`, `app_version`, `lang_code`, `system_lang_code` (все `String, nullable=True`)

### Pool / build_client

- `src/accounts/pool.py::build_client` читает device-поля из Account и передаёт в `TelegramClient(...)` если они есть

### Intake

- Расширить `POST /api/accounts/intake` в `src/dashboard/app.py` — IntakeRequest получает device-поля
- Расширить `src/accounts/bulk_import.py` — CSV с колонками `name,phone,session_path,proxy,daily_limit,hourly_limit,device_model,system_version,app_version,lang_code,system_lang_code`

### Bulk-режим sender'а

- В `src/sender/send_queue.py` добавить mode `bulk`:
  * Без `simulate_human_behavior` (typing, read_incoming)
  * Паузы 30-90 сек (из `BULK_MIN/MAX_DELAY_SECONDS`)
  * `pre_send_check` — пропускает stranger/too_recent/category/ramp-up
  * Свои лимиты: `BULK_DAILY_LIMIT` (1000-1500), `BULK_HOURLY_LIMIT` (100-150)

### Bulk-генератор очереди

- Новый `src/generator/generate_bulk.py`:
  * Для каждого enabled-аккаунта берёт **все** контакты с историей (`total_messages > 0` AND `last_msg_at NOT NULL`)
  * Для каждого создаёт `Draft` из случайного шаблона с подстановкой имени
  * Прикрепляет `attachment_*` из CLI аргументов

- `src/generator/bulk_templates.py` — банк из 10-20 опенеров с placeholder `{name}`

### Конфиг

- `.env.example`:
  ```
  BULK_MIN_DELAY_SECONDS=30
  BULK_MAX_DELAY_SECONDS=90
  BULK_DAILY_LIMIT=1500
  BULK_HOURLY_LIMIT=120
  ```

### Makefile

- `make bulk-generate ATTACH=photo:attachments/offer.png CAPTION="посмотри"`
- `make bulk-send`

## Что НЕ трогать

- Существующий cold-outreach flow — `generate_drafts.py`, `send_queue.py` без `--mode bulk`
- LLM-инфраструктура (нужна для cold-режима, не трогаем)
- Архитектура шардинга, Redis pub/sub, Postgres
- Dashboard, healthcheck, email-verify

Bulk должен сосуществовать с cold, чтобы можно было использовать оба режима в одном проекте.

## Дополнительно (если есть время)

- **Health-aware proxy rotation** для будущего использования (когда вернёмся к cold или к долгоиграющим аккаунтам):
  * Таблица `ProxyPool(url, region, status, assigned_to, cooldown_until, abuse_count)`
  * Автоматическая смена при `FloodWait > N` подряд
  * Cooldown сгоревшего IP 30 дней
  * Алерт в Telegram-бот при исчерпании резерва
- **Telegram-алерты** при подходе аккаунта к лимиту FloodWait

## Что я ожидаю получить

Один пуш в ветку `claude/adoring-planck-HWyji` с:

- Миграция БД (новые поля Account)
- Bulk-режим sender'а
- Bulk-генератор очереди
- Документация в `docs/BULK_MODE.md` как использовать
- Без поломки текущей cold-outreach функциональности

Для возврата к работе пришлите: «продолжай по `docs/NEXT_TASK.md`».
