# Мульти-аккаунт

Принцип: каждый аккаунт — отдельная telethon-сессия со своим прокси, своим
дневным/часовым лимитом и своими контактами. Один контакт привязан к одному
аккаунту навсегда (sticky), чтобы один и тот же человек никогда не получил
сообщений с двух номеров — это первый триггер репорта и каскадного бана.

## Схема БД

```
accounts
├── id, name, phone, session_path, proxy
├── daily_limit, hourly_limit
├── status (active | flood | banned | disabled)
├── flood_until, last_send_at
└── enabled

contacts.account_id  → accounts.id   (UNIQUE (account_id, tg_user_id))
drafts.account_id    → accounts.id
send_log.account_id  → accounts.id
```

`Account.daily_limit` / `hourly_limit` — потолки **именно этого** аккаунта.
Глобальные `DEFAULT_DAILY_LIMIT` / `DEFAULT_HOURLY_LIMIT` в `.env` — только
дефолты для импорта новых аккаунтов без своих значений в CSV.

## Импорт аккаунтов из CSV

Формат (см. `accounts.example.csv`):

```csv
name,phone,session_path,proxy,daily_limit,hourly_limit
acc01,+79001234567,sessions/acc01.session,socks5://user:pass@1.2.3.4:1080,80,8
acc02,+79007654321,sessions/acc02.session,socks5://user:pass@1.2.3.5:1080,80,8
acc03,+79009876543,,,,                          # без прокси, дефолтные лимиты
```

Поля `session_path`, `proxy`, `daily_limit`, `hourly_limit` опциональны:

- пустой `session_path` → подставится `sessions/<name>.session`
- пустой `proxy` → аккаунт пойдёт напрямую (на масштабе не рекомендую)
- пустой `daily_limit`/`hourly_limit` → возьмёт из `.env`

Импорт:

```bash
make accounts-import CSV=accounts.csv
# или
python -m src.accounts.bulk_import accounts.csv
```

Повторный импорт того же CSV — обновит существующие записи (по `name`).

## Авторизация

Два пути:

**1. У вас уже есть session-файлы** (например, выгрузили с warmup-фермы):

Положите их по путям из CSV (`sessions/<name>.session` по умолчанию).
Логин-команда тогда не нужна — пул при запуске сам подхватит.

**2. Логин с нуля по номеру:**

```bash
# По одному — Telegram пришлёт код в чат для каждого
python -m src.accounts.login acc01

# Или массово по всем неавторизованным
make accounts-login
```

## Прокси

Поддерживаемые форматы в поле `proxy`:

```
socks5://user:password@host:port
socks5://host:port                          # без авторизации
socks4://user:password@host:port
http://user:password@host:port              # HTTP CONNECT
```

**Что брать на масштабе 100–200 аккаунтов:**

- **Резидентские**, не datacenter. Datacenter-IP Telegram палит быстро.
- **Статические** (sticky session 30+ дней). Динамические через пул IP убьют сессию.
- **Один прокси — один аккаунт.** Никакого шеринга, даже на «соседние» аккаунты.
- Гео — близко к гео аккаунта. Аккаунт с номером +7, выходящий через США,
  смотрится подозрительно.

Провайдеры, которые работают: `IPRoyal`, `Smartproxy`, `Bright Data`,
`Proxy-Seller` (residential static). Бюджет — $5–15/мес за статический IP.

## Жизненный цикл

```
bulk_import → login/раздача .session → fetch_dialogs → score → generate → send
                                            ↑
                                            └── по каждому аккаунту отдельно
```

- `fetch_dialogs --all` пройдёт по всем enabled и качнёт их историю.
- `score_contacts` работает глобально (скоринг каждого контакта в его аккаунте).
- `generate_drafts --top N` создаст черновики по топ-N контактов **в каждом**
  аккаунте. С `--account <name>` — только для одного.
- `send_queue` рулит пулом: каждый драфт уходит через свой `account_id`,
  диспетчер крутит round-robin между аккаунтами у которых есть capacity.

## Что делает диспетчер

На каждой итерации проверяет аккаунт:

- `enabled` и `status == "active"`
- `flood_until` < сейчас
- `count(send_log за 24ч) < daily_limit`
- `count(send_log за 1ч) < hourly_limit`

Если у назначенного аккаунта нет capacity — драфт пропускается, sender
ждёт минуту и пробует снова. Если у всех нет capacity — спит 20 минут.

## Что происходит при бане

- `FloodWaitError` от Telegram → аккаунт помечается `status=flood`,
  `flood_until = now + (seconds * 1.15)`. Sender его не трогает до истечения,
  работает через остальных. После истечения — автоматически возвращается в `active`.
- `PeerFloodError` → аккаунт `status=banned`, `enabled=false`. Из ротации
  выводится насовсем. Сжёгся.
- Прочие ошибки на отправке — фейлят конкретный draft (`status=failed`),
  аккаунт остаётся в строю.

## Мониторинг состояния

Быстрый запрос к БД:

```sql
SELECT name, status, status_reason, daily_limit,
       (SELECT COUNT(*) FROM send_log
        WHERE account_id = accounts.id
          AND sent_at > datetime('now','-24 hour')
          AND success = 1) as sent_24h
FROM accounts
ORDER BY name;
```

Что бэкапить:

- `sessions/*.session` — без них теряется авторизация всего парка
- `data/tg_assistant.db` — контакты, скоринг, лог отправок, статус аккаунтов
- `.env`, `accounts.csv` — конфиг
