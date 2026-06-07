# ❄️ FROZEN — Cold Outreach Project (v1)

Эта ветка содержит **первую версию проекта** — cold outreach система на 500
аккаунтов с LLM-генерацией, прокси, ramp-up и полной cold-safety обвязкой.

**Статус: FROZEN.** Развитие остановлено по решению владельца.

## Что было сделано

- Postgres + Redis + 10 sender-воркеров с шардингом
- LLM-генерация уникальных текстов (Claude API)
- Антиспам обвязка: typing simulation, ramp-up, working hours,
  reply-rate monitoring, opt-out
- Sticky proxy + sticky device profiles
- Email-верификация при логине через catch-all IMAP
- Dashboard + intake API + healthcheck с Telegram-алертами
- Поддержка вложений (photo / video / audio / voice / document / link)
- Live-reload пула через Redis pub/sub

## Где новый проект

Активная разработка переехала в ветку **`main`** — там лежит
другой по идеологии проект **Telegram Contacts Broadcaster**:
рассылка существующим контактам (не cold outreach), без прокси,
с реальными device-fingerprint, оптимизированный под UZ-сегмент.

```bash
git checkout main
```

## Можно ли вернуться

Эта ветка не удаляется. Если когда-то понадобится cold-outreach
функциональность — `git checkout claude/adoring-planck-HWyji`,
всё на месте и работает. Архитектурная документация в `docs/SCALE_500.md`,
`docs/MULTI_ACCOUNT.md`, `docs/DASHBOARD.md`, `docs/SAFETY.md`.
