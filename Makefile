.PHONY: help build up down logs import generate sender-test optout test-email test-imap psql

CSV ?= accounts.csv
ATTACH ?=
CAPTION ?=
DELAY ?= 15

help:
	@echo "Targets:"
	@echo "  make build              — собрать образ"
	@echo "  make up                 — postgres + sender'ы + optout"
	@echo "  make down               — остановить всё"
	@echo "  make logs               — tail логов всех сервисов"
	@echo "  make import CSV=...     — импорт аккаунтов из CSV"
	@echo "  make generate ATTACH=photo:attachments/x.png CAPTION='вот' DELAY=15"
	@echo "  make optout             — поднять optout-listener отдельно"
	@echo "  make test-imap          — проверить IMAP-конфиг (генерит email, ждёт письмо)"
	@echo "  make psql               — открыть psql shell в БД"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=200

import:
	docker compose run --rm sender-0 python -m src.import_accounts /app/$(CSV)

generate:
	docker compose run --rm sender-0 python -m src.generate \
		$(if $(ATTACH),--attach $(ATTACH),) \
		$(if $(CAPTION),--attach-caption "$(CAPTION)",) \
		--attach-delay $(DELAY)

optout:
	docker compose up -d optout

intake:
	docker compose up -d intake intake-worker

scheduler:
	docker compose up -d scheduler

scheduler-logs:
	docker compose logs -f scheduler

intake-logs:
	docker compose logs -f intake intake-worker

test-imap:
	docker compose run --rm sender-0 python -m src.email_verify

verify-fingerprints:
	docker compose run --rm sender-0 python -m src.verify_fingerprints

verify-fingerprints-bad:
	docker compose run --rm sender-0 python -m src.verify_fingerprints --bad

psql:
	docker compose exec postgres psql -U tg -d tg_broadcaster
