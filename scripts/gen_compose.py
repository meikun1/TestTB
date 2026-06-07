#!/usr/bin/env python3
"""
Генератор docker-compose.yml под нужный масштаб.

  python scripts/gen_compose.py --shards 100 --senders 10 --pgbouncer

Параметры:
  --shards N       — сколько шард всего (== WORKER_COUNT)
  --senders M      — сколько sender-контейнеров поднимать на ЭТОМ сервере
  --pgbouncer      — добавить сервис pgbouncer (нужен от ~2000 акк)
  --shard-offset N — с какого индекса шарды начать (multi-server)
  --shard-range N  — сколько шард на этот сервер
  --output FILE    — куда писать (default: docker-compose.yml)
"""
import argparse
from pathlib import Path


HEADER = """\
# АВТОГЕНЕРИРОВАННЫЙ docker-compose.yml
# Перегенерируй через scripts/gen_compose.py

x-app-base: &app-base
  build: .
  image: tg-broadcaster:latest
  env_file: .env
  volumes:
    - ./attachments:/app/attachments
    - ./logs:/app/logs
    - ./sessions:/app/sessions
    - ./accounts.csv:/app/accounts.csv:ro
  depends_on:
    postgres:
      condition: service_healthy
  restart: unless-stopped

services:
"""


POSTGRES_BLOCK = """\
  postgres:
    image: postgres:16-alpine
    container_name: tg-postgres
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER:-tg}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:-tg}
      POSTGRES_DB: ${POSTGRES_DB:-tg_broadcaster}
    command:
      - postgres
      - -c
      - max_connections=400
      - -c
      - shared_buffers=1GB
      - -c
      - effective_cache_size=3GB
      - -c
      - work_mem=16MB
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER:-tg}"]
      interval: 5s
      timeout: 3s
      retries: 10

"""


PGBOUNCER_BLOCK = """\
  pgbouncer:
    image: edoburu/pgbouncer:latest
    container_name: tg-pgbouncer
    restart: unless-stopped
    environment:
      DB_HOST: postgres
      DB_PORT: 5432
      DB_USER: ${POSTGRES_USER:-tg}
      DB_PASSWORD: ${POSTGRES_PASSWORD:-tg}
      DB_NAME: ${POSTGRES_DB:-tg_broadcaster}
      POOL_MODE: transaction
      MAX_CLIENT_CONN: 2000
      DEFAULT_POOL_SIZE: 50
      MIN_POOL_SIZE: 10
    depends_on:
      postgres:
        condition: service_healthy

"""


OPTOUT_BLOCK = """\
  optout:
    <<: *app-base
    container_name: tg-optout
    command: python -m src.optout --shards all

"""


INTAKE_BLOCK = """\
  intake:
    <<: *app-base
    container_name: tg-intake
    command: python -m src.intake_api
    ports:
      - "${INTAKE_PORT:-8090}:8090"

"""


FOOTER = """
volumes:
  postgres_data:
"""


def _sender_block(name: str, shard_spec: str) -> str:
    return (
        f"  {name}:\n"
        f"    <<: *app-base\n"
        f"    container_name: tg-{name}\n"
        f'    command: python -m src.sender --shards "{shard_spec}"\n'
        f"\n"
    )


def gen(args) -> str:
    total_shards = args.shards
    senders = args.senders
    offset = args.shard_offset
    range_ = args.shard_range or (total_shards - offset)

    my_shards = list(range(offset, offset + range_))
    if not my_shards:
        raise SystemExit("Нет шард для этого сервера — проверь --shard-offset/--shard-range")

    per_sender = len(my_shards) // senders
    rest = len(my_shards) % senders

    out = [HEADER, POSTGRES_BLOCK]
    if args.pgbouncer:
        out.append(PGBOUNCER_BLOCK)
    out.append(OPTOUT_BLOCK)
    out.append(INTAKE_BLOCK)

    idx = 0
    for s in range(senders):
        chunk = per_sender + (1 if s < rest else 0)
        my = my_shards[idx:idx + chunk]
        idx += chunk
        if not my:
            continue
        spec = f"{my[0]}-{my[-1]}" if len(my) > 1 else f"{my[0]}"
        out.append(_sender_block(f"sender-{s}", spec))

    out.append(FOOTER)
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, required=True,
                        help="Сколько всего шард (==WORKER_COUNT в .env)")
    parser.add_argument("--senders", type=int, default=10,
                        help="Sender-контейнеров на этом сервере")
    parser.add_argument("--pgbouncer", action="store_true",
                        help="Добавить pgbouncer")
    parser.add_argument("--shard-offset", type=int, default=0,
                        help="С какой шарды начать (multi-server)")
    parser.add_argument("--shard-range", type=int, default=None,
                        help="Сколько шард брать на этот сервер")
    parser.add_argument("--output", type=str, default="docker-compose.yml")
    args = parser.parse_args()

    content = gen(args)
    Path(args.output).write_text(content)
    rng_end = args.shard_offset + (args.shard_range or args.shards - args.shard_offset) - 1
    print(f"Сгенерирован {args.output}")
    print(f"  Всего шард: {args.shards}")
    print(f"  На этом сервере: {args.shard_offset}..{rng_end}")
    print(f"  Sender-контейнеров: {args.senders}")
    print(f"  PgBouncer: {'да' if args.pgbouncer else 'нет'}")


if __name__ == "__main__":
    main()
