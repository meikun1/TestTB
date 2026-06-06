# Деплой на сервер

Три способа: bash + venv, Docker, systemd. Выбирай по вкусу.

## 1. Bash + venv (быстрый старт)

```bash
git clone <repo-url> tg-assistant
cd tg-assistant
./install.sh
nano .env            # заполни ключи
source venv/bin/activate

# Первый запуск: введёшь код из Telegram в консоли
python -m src.fetcher.fetch_dialogs
python -m src.scoring.score_contacts
python -m src.generator.generate_drafts --top 10
python -m src.sender.send_queue --mode review
```

## 2. Docker

```bash
git clone <repo-url> tg-assistant
cd tg-assistant
cp .env.example .env
nano .env

docker compose build

# Первый вход в Telegram — интерактивно, введёшь код:
make docker-login
# либо: docker compose run --rm tg-assistant python -m src.fetcher.fetch_dialogs

# Дальше — фоном:
docker compose up -d
docker compose logs -f
```

Сессия Telegram (`data/*.session`) и БД (`data/*.db`) сохраняются в volume — пересборка образа их не убьёт.

## 3. systemd (long-running sender как сервис)

```bash
# 1. Создай пользователя и положи проект
sudo useradd -r -m -d /opt/tg-assistant -s /bin/bash tgbot
sudo -u tgbot git clone <repo-url> /opt/tg-assistant
cd /opt/tg-assistant
sudo -u tgbot ./install.sh
sudo -u tgbot nano .env

# 2. Сделай первый вход вручную (telethon сохранит session-файл)
sudo -u tgbot ./venv/bin/python -m src.fetcher.fetch_dialogs

# 3. Поставь unit
sudo cp deploy/tg-assistant.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tg-assistant
sudo systemctl status tg-assistant
journalctl -u tg-assistant -f
```

## Обновление

```bash
cd /opt/tg-assistant   # или куда клонировал
git pull
./venv/bin/pip install -r requirements.txt
sudo systemctl restart tg-assistant   # если используешь systemd
# либо: docker compose build && docker compose up -d
```

## Что бэкапить

- `.env` — ключи
- `data/*.session` — авторизация Telegram (без неё попросит код заново)
- `data/tg_assistant.db` — все контакты, скоринг, черновики, лог отправок
