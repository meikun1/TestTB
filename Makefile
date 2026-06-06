PY := ./venv/bin/python
TOP ?= 10
GOAL ?= personal
HOOK ?=
CSV ?= accounts.csv
NAME ?=

.PHONY: install accounts-import accounts-login fetch-all fetch score generate send-review send-auto clean docker-build docker-up docker-down docker-login

install:
	./install.sh

# --- Multi-account ---
accounts-import:
	$(PY) -m src.accounts.bulk_import $(CSV)

accounts-login:
	$(PY) -m src.accounts.login --all

# --- Workflow ---
fetch-all:
	$(PY) -m src.fetcher.fetch_dialogs --all

fetch:
	$(PY) -m src.fetcher.fetch_dialogs --account $(NAME)

score:
	$(PY) -m src.scoring.score_contacts

generate:
	$(PY) -m src.generator.generate_drafts --top $(TOP) --goal $(GOAL) --hook "$(HOOK)"

send-review:
	$(PY) -m src.sender.send_queue --mode review

send-auto:
	$(PY) -m src.sender.send_queue --mode auto

clean:
	rm -rf venv __pycache__ */__pycache__ */*/__pycache__

# --- Docker ---
docker-build:
	docker compose build

docker-login:
	# Интерактивный первый вход в Telegram (введёшь код из SMS).
	docker compose run --rm tg-assistant python -m src.fetcher.fetch_dialogs

docker-up:
	docker compose up -d

docker-down:
	docker compose down
