#!/usr/bin/env bash
# Установка проекта с нуля на сервере.
#   git clone <repo> && cd <repo> && ./install.sh
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo "Не найден $PYTHON_BIN. Установи Python 3.10+ и повтори." >&2
    exit 1
fi

PY_VERSION=$("$PYTHON_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=${PY_VERSION%.*}
PY_MINOR=${PY_VERSION#*.}
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "Нужен Python 3.10+, найден $PY_VERSION" >&2
    exit 1
fi

echo "==> Создаю venv"
"$PYTHON_BIN" -m venv venv

echo "==> Ставлю зависимости"
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

echo "==> Готовлю папки"
mkdir -p data logs sessions

if [ ! -f .env ]; then
    cp .env.example .env
    echo
    echo "==> Создан .env из шаблона. Заполни его перед запуском:"
    echo "    nano .env"
fi

echo
echo "Готово. Дальше:"
echo "  1. Заполни .env (TG_API_ID, TG_API_HASH, ANTHROPIC_API_KEY)"
echo "  2. source venv/bin/activate"
echo "  3. Подготовь accounts.csv (см. accounts.example.csv) и импортируй:"
echo "       python -m src.accounts.bulk_import accounts.csv"
echo "  4. Положи session-файлы в sessions/ ИЛИ авторизуйся:"
echo "       python -m src.accounts.login --all"
echo "  5. Выкачать историю по каждому аккаунту:"
echo "       python -m src.fetcher.fetch_dialogs --all"
echo "  6. python -m src.scoring.score_contacts"
echo "  7. python -m src.generator.generate_drafts --top 10"
echo "  8. python -m src.sender.send_queue --mode review"
