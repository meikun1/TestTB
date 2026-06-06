# Dashboard и Intake API

FastAPI-приложение в `src/dashboard/app.py`. Поднимает HTML-морду со статусами пула и JSON-API для интеграции с warmup-пайплайном.

## Запуск

```bash
make dashboard
# или напрямую
python -m src.dashboard.app
# или продакшен
uvicorn src.dashboard.app:app --host 0.0.0.0 --port 8080 --workers 2
```

В Docker — отдельный сервис в `docker-compose.yml`:

```bash
docker compose up -d
# открыть http://<server>:8080
```

## Аутентификация

В `.env`:

```bash
DASHBOARD_TOKEN=длинная-случайная-строка
```

- **Для браузера:** HTTP Basic. Логин `admin`, пароль — `DASHBOARD_TOKEN`.
- **Для API:** заголовок `Authorization: Bearer <DASHBOARD_TOKEN>`.

`/api/healthz` — без авторизации, для liveness-проб оркестратора.

## HTML-страницы

| URL | Что показывает |
|---|---|
| `/` | Обзор: счётчики по статусам, активность за 24ч, контактов в БД |
| `/accounts` | Таблица всех аккаунтов: статус, лимиты, факт по часу и суткам, прокси |
| `/accounts/<name>` | Детали аккаунта + последние 30 отправок |
| `/queue` | Очередь черновиков по аккаунтам в разрезе статусов |
| `/send-log` | Последние 200 отправок по всему пулу |

## Intake API — приём сессий из внешнего пайплайна

Когда warmup-сервер закончил прогрев аккаунта, он пушит сессию сюда. После запроса аккаунт сразу появляется в пуле и при следующем рестарте sender'а подхватится.

**`POST /api/accounts/intake`**

Headers:
```
Authorization: Bearer <DASHBOARD_TOKEN>
Content-Type: application/json
```

Body:
```json
{
  "name": "acc01",
  "phone": "+79001234567",
  "session_string": "1BVtsOK4Bu...огромная-строка-Telethon-StringSession...",
  "proxy": "socks5://user:pass@1.2.3.4:1080",
  "daily_limit": 80,
  "hourly_limit": 8,
  "enabled": true
}
```

- `name`, `phone`, `session_string` — обязательны.
- `proxy` — опционально (если не задано или `PROXIES_ENABLED=false`, аккаунт пойдёт напрямую).
- `daily_limit`/`hourly_limit` — опционально; по умолчанию из `DEFAULT_DAILY_LIMIT`/`DEFAULT_HOURLY_LIMIT` в `.env`.
- `enabled` — true по умолчанию.

Response 200:
```json
{ "account_id": 42, "name": "acc01", "created": true }
```

`created: false` — аккаунт уже был, обновили его поля. Если был в статусе `flood`/`banned` — сбрасываем в `active` (предположение: новая сессия = новый старт).

### Откуда взять `session_string` в warmup-пайплайне

В Telethon, после `await client.start(phone=...)`:

```python
from telethon.sessions import StringSession
session_string = StringSession.save(client.session)
```

Эту строку шлёшь в intake API. Она содержит auth_key и dc_id — всё что нужно
для подключения с другого сервера.

### Пример push'а из bash:

```bash
curl -X POST https://dash.example.com/api/accounts/intake \
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name":"acc01",
    "phone":"+79001234567",
    "session_string":"1BVtsOK4Bu....",
    "proxy":"socks5://u:p@1.2.3.4:1080",
    "daily_limit":80
  }'
```

### Пример из Python:

```python
import httpx, os
from telethon.sessions import StringSession

# ...после прогрева...
session_string = StringSession.save(client.session)

httpx.post(
    "https://dash.example.com/api/accounts/intake",
    headers={"Authorization": f"Bearer {os.environ['DASHBOARD_TOKEN']}"},
    json={
        "name": account_name,
        "phone": phone,
        "session_string": session_string,
        "proxy": proxy_url,
        "daily_limit": 80,
    },
    timeout=15,
)
```

## Прочие API

| Метод | URL | Что делает |
|---|---|---|
| GET | `/api/accounts` | Список всех аккаунтов со статусами и счётчиками |
| GET | `/api/accounts/{name}` | Состояние конкретного |
| GET | `/api/healthz` | Liveness (без auth) |

## Подхват новых аккаунтов sender'ом

`AccountPool` загружается при старте процесса sender'а. Чтобы новые intake-аккаунты попали в работу — sender'у нужен рестарт:

```bash
make send-auto              # foreground
# или
docker compose restart sender
# или
systemctl restart tg-assistant
```

Live-reload пула без рестарта — можно добавить отдельным фоновым тиком в sender (раз в N минут перечитывает enabled-аккаунты). Если нужно — скажи.
