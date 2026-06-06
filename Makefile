PY := ./venv/bin/python
TOP ?= 10
GOAL ?= personal
HOOK ?=
CSV ?= accounts.csv
NAME ?=
SHARD ?= 0

.PHONY: install accounts-import accounts-login fetch-all fetch score generate \
        send-review send-auto sender-shard dashboard fetcher healthcheck \
        docker-build docker-up docker-down docker-logs

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

# Sender: один шард локально (для отладки). В проде воркеры крутит docker-compose.
sender-shard:
	$(PY) -m src.sender.send_queue --mode auto --shard $(SHARD)

send-review:
	$(PY) -m src.sender.send_queue --mode review

send-auto:
	$(PY) -m src.sender.send_queue --mode auto

# --- Сервисы ---
dashboard:
	$(PY) -m src.dashboard.app

fetcher:
	$(PY) -m src.fetcher.service

healthcheck:
	$(PY) -m src.monitoring.healthcheck

# --- Docker ---
docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f --tail=200

docker-restart-senders:
	docker compose restart sender-0 sender-1 sender-2 sender-3 sender-4 \
	                       sender-5 sender-6 sender-7 sender-8 sender-9
