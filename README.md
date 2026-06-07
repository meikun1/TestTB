# Telegram Contacts Broadcaster

Система рассылки сообщений **существующим контактам** из множества Telegram-аккаунтов. Под UZ-сегмент (+998), без прокси, с реальным device-fingerprint каждого аккаунта.

> Это не cold outreach к незнакомцам. Это рассылка людям с которыми у каждого аккаунта **уже есть переписка** — туда антифрод реагирует на порядок мягче.

## Что внутри

- **PostgreSQL** — хранилище аккаунтов, контактов, очереди, логов
- **N sender-воркеров** (10 по умолчанию) с шардингом по `account.id % WORKER_COUNT`
- **Очередь через `SELECT FOR UPDATE SKIP LOCKED`** — без Redis/Celery
- **Email-верификация** при логине через свой catch-all IMAP-сервер
- **Captcha framework** (manual + расширение под 2captcha/anticaptcha)
- **Geo-консистентность UZ:** carrier-валидация номеров, active-hours по Asia/Tashkent, lang_code/system_lang_code дефолты под UZ
- **Глобальный opt-out:** слушатель входящих с автодетектом STOP-фраз (ru + uz)
- **Вложения:** photo / video / audio / voice / document / link
- **Sticky device-fingerprint:** реальные `device_model`, `system_version`, `app_version`, `lang_code` каждого аккаунта подаются в Telethon

## Структура проекта

```
src/
├── config.py           # все настройки из .env
├── db.py               # asyncpg engine + init
├── models.py           # SQLAlchemy 2.0 модели
├── carrier.py          # детект оператора по UZ-префиксу
├── language.py         # эвристический детект ru/uz_cyrl/uz_latn
├── timewindow.py       # active hours по Asia/Tashkent
├── telethon_client.py  # build_client с реальными device-полями
├── email_verify.py     # генерация email + IMAP poll + парсинг кода
├── captcha.py          # framework для решения капч
├── attachments.py      # отправка вложений всех типов
├── templates.py        # банк шаблонов ru + uz_latn + uz_cyrl
├── import_accounts.py  # импорт CSV + выгрузка контактов
├── generate.py         # генерация Draft'ов для всех контактов
├── sender.py           # sender-воркер с шардом
└── optout.py           # слушатель входящих → blocklist
```

## Требования

- VPS с Docker (≥ 16 GB RAM, 8 vCPU для 500-1000 акк)
- Желательно хостинг в UZ (Uzonline/BCCloud) или KZ (ps.kz)
- Catch-all почтовый сервер с IMAP (для email-верификации)
- TG_API_ID + TG_API_HASH с https://my.telegram.org/apps

## Быстрый старт

```bash
# 1. Клон
git clone <repo-url> tg-broadcaster
cd tg-broadcaster

# 2. Конфиг
cp .env.example .env
nano .env
# Заполни как минимум:
#   TG_API_ID, TG_API_HASH
#   POSTGRES_PASSWORD (случайная строка)
#   IMAP_HOST, IMAP_USER, IMAP_PASSWORD, EMAIL_DOMAIN
#   ALERT_BOT_TOKEN / ALERT_CHAT_ID (опционально)

# 3. Сборка
docker compose build

# 4. БД на старт
docker compose up -d postgres

# 5. Положи CSV аккаунтов
cp accounts.example.csv accounts.csv
nano accounts.csv  # заменить на реальные данные

# 6. Положи файл-вложение
cp /path/to/your/offer.png attachments/offer.png

# 7. Импорт аккаунтов + выгрузка контактов
#    (если у каких-то session_string просрочен — попросит phone-код в консоли,
#     email-подтверждения обработает автоматом)
make import CSV=accounts.csv

# 8. Сначала проверь что IMAP работает (опционально)
make test-imap

# 9. Сгенерь очередь драфтов для всех контактов
make generate ATTACH=photo:attachments/offer.png CAPTION="посмотри" DELAY=15

# 10. Запусти всё — sender'ы + optout
docker compose up -d

# 11. Смотри что происходит
make logs
```

Через 6-24 часа рассылка завершится.

## CSV-формат аккаунтов

```
name,phone,session_string,device_model,system_version,app_version,lang_code,system_lang_code
acc01,+998901234567,1BVtsOK4Bu...,iPhone 14,iOS 16.5.1,10.2.0,ru,ru-UZ
```

Поля:
- `name` — уникальный идентификатор аккаунта в системе
- `phone` — UZ номер (+998 XX XXXXXXX), валидируется по carrier-префиксу
- `session_string` — Telethon StringSession (выгружается из tdata или после логина)
- `device_model`, `system_version`, `app_version` — реальный device-fingerprint того клиента где аккаунт изначально логинился (если не знаешь — оставь пустым, тогда дефолты Telethon)
- `lang_code`, `system_lang_code` — если пусто, дефолты из `.env` (по умолчанию `ru` / `ru-UZ`)

## Поддерживаемые типы вложений

```bash
make generate ATTACH=photo:attachments/offer.png CAPTION="посмотри"
make generate ATTACH=video:attachments/promo.mp4 CAPTION="вот видео"
make generate ATTACH=audio:attachments/track.mp3
make generate ATTACH=voice:attachments/intro.ogg
make generate ATTACH=document:attachments/brochure.pdf
make generate ATTACH=link:https://example.com/offer
```

Параметр `DELAY=15` (или 0 чтобы вложение пошло caption'ом).

## Email-верификация

Когда Telegram при логине требует email-подтверждение, система автоматически:

1. Генерит `tg_<8hex>@<EMAIL_DOMAIN>` (например `tg_3a8f9c2e@veximail.space`)
2. Записывает в таблицу `used_emails` с UNIQUE-индексом (адрес никогда не повторяется)
3. Подаёт Telegram'у
4. Поллит IMAP-ящик `IMAP_USER` каждые 5 секунд
5. Парсит 5-6-значный код из тела письма
6. Подтверждает в Telegram

Проверить настройки IMAP до начала:
```bash
make test-imap
```
Сгенерит адрес → отправь ему любое письмо с числом → если код извлечётся, всё ок.

## Captcha-handling

По умолчанию `CAPTCHA_SOLVER=manual`. При запросе капчи система:

1. Записывает в таблицу `captcha_challenges` со статусом `pending`
2. Логирует warning
3. Ждёт `CAPTCHA_TIMEOUT_SECONDS` (300 по умолчанию) пока админ обновит запись:

```bash
make psql
# в psql:
UPDATE captcha_challenges SET status='solved', solution='<решение>' WHERE id=<N>;
```

Расширение `CAPTCHA_SOLVER=2captcha` / `anticaptcha` — есть stubs, нужны API-credentials и реальные детали challenge_data Telegram'а.

## UZ-специфика

- Активные часы рассылки: 9:00-23:00 по `ACTIVE_TIMEZONE` (Asia/Tashkent, UTC+5)
- Ночью (23:00-9:00) sender спит
- Carrier-throttling: Beeline UZ (+998 90/91) ограничен 60% от общего carrier-cap
- Глобальный carrier-cap: 50 сообщений в час суммарно по всем аккаунтам одного оператора
- Дефолты `lang_code=ru`, `system_lang_code=ru-UZ` для пустых полей в CSV
- Шаблоны: 15 русских + 8 узбекских латиницей + 5 узбекских кириллицей

Узбекские шаблоны (`src/templates.py`) — placeholder-уровень. Перед production проверь у носителя языка.

## Хостинг рекомендации

| Локация | Оценка |
|---|---|
| 🟢 UZ (Uzonline, BCCloud) | Идеально |
| 🟢 KZ (ps.kz, hoster.kz) | Очень хорошо |
| 🟡 RU (Aeza, Selectel) | Приемлемо |
| 🔴 EU/US/Asia-far | Не рекомендую |

## Мониторинг

Без отдельного дашборда — всё через psql:

```sql
-- Общая статистика по аккаунтам
SELECT status, COUNT(*) FROM accounts WHERE enabled GROUP BY status;

-- Сколько драфтов в очереди
SELECT status, COUNT(*) FROM drafts GROUP BY status;

-- Активность за последний час
SELECT COUNT(*) FROM send_log
WHERE sent_at > now() - interval '1 hour' AND success = true;

-- Топ ошибок
SELECT error, COUNT(*) FROM send_log
WHERE success = false
GROUP BY error ORDER BY 2 DESC LIMIT 20;

-- Капчи требующие ручного решения
SELECT id, account_id, challenge_type, requested_at FROM captcha_challenges
WHERE status = 'pending' ORDER BY requested_at DESC;

-- Сгоревшие аккаунты
SELECT name, phone, status, status_reason FROM accounts
WHERE status IN ('banned', 'needs_email', 'needs_captcha', 'geo_mismatch');

-- Глобальный opt-out blocklist
SELECT COUNT(*) FROM blocked_contacts;
```

## Что НЕ делает система

- ❌ Не использует прокси (PROXIES_ENABLED логики нет — все ходят с IP сервера)
- ❌ Не использует LLM для генерации текстов (шаблоны достаточно для рассылки знакомым)
- ❌ Не делает typing-симуляцию, чтение входящих (избыточно)
- ❌ Не делает ramp-up периодов
- ❌ Не делает скоринг контактов (берёт всех)
- ❌ Не предоставляет web-dashboard (psql достаточно)

Если что-то из этого захочется добавить — см. опциональные улучшения в `docs/`.

## Безопасность

- `.env` в `.gitignore` — секреты не уезжают в репо
- Sessions в `sessions/` (если используются файловые) — тоже игнор
- `accounts.csv` — игнор
- Доступ к серверу: только SSH-ключ, fail2ban, минимум открытых портов
- Postgres внутри docker-сети — не выставлен наружу
- Не использовать basic-аккаунт root для деплоя — отдельный user

См. `docs/SECURITY.md` (TODO).

## Лицензия

Внутренний проект, без публичной лицензии.
