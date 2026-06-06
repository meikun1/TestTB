# TG Assistant

Помощник для написания персональных сообщений контактам в Telegram.
Анализирует историю переписок, скорит контакты, генерирует тексты в твоём стиле, отправляет с антиспам-задержками.

## Архитектура

```
src/
├── fetcher/      # Telethon — выгрузка диалогов и сообщений
├── scoring/      # Скоринг контактов: тёплый/остывающий/холодный/мёртвый
├── generator/    # Генерация сообщений (Claude API сейчас, своя модель потом)
├── sender/       # Очередь отправки с задержками и лимитами
└── utils/        # Логирование, хранилище, БД
```

## Установка

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Настройка

1. Получи `API_ID` и `API_HASH` на https://my.telegram.org/apps
2. Получи `ANTHROPIC_API_KEY` на https://console.anthropic.com (на первое время)
3. Скопируй `.env.example` в `.env` и заполни ключи

## Запуск

```bash
# Шаг 1: выгрузить диалоги (первый раз попросит код из Telegram)
python -m src.fetcher.fetch_dialogs

# Шаг 2: посчитать скоринг контактов
python -m src.scoring.score_contacts

# Шаг 3: сгенерировать черновики для топ-N контактов
python -m src.generator.generate_drafts --top 10

# Шаг 4: запустить sender в полу-ручном режиме (тебя спросят перед каждой отправкой)
python -m src.sender.send_queue --mode review
```

## Безопасность аккаунта

— Лимит по умолчанию: 20 сообщений в день
— Паузы между отправками: 5–40 минут (рандом)
— Только в рабочие часы по таймзоне контакта
— Human-in-the-loop на первое время: ты подтверждаешь каждый текст

Эти ограничения снижают риск бана аккаунта Telegram за подозрительную активность.

## Roadmap

- [x] MVP с Claude API
- [ ] Сбор датасета для файнтюна (твои реплики из истории)
- [ ] LoRA fine-tuning Qwen 2.5 7B на твоём стиле
- [ ] Замена API на локальную модель через vLLM
