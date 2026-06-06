# Переход на свою модель (LoRA fine-tuning)

Когда MVP с Claude API заработает и накопится достаточно данных, переходим на локальную модель.

## Шаг 1. Сбор датасета

Из БД вытащить пары «контекст переписки → твой ответ»:

```python
# scripts/build_dataset.py (псевдокод)
for contact in contacts:
    msgs = get_messages(contact, ordered=True)
    for i, msg in enumerate(msgs):
        if msg.from_me and i > 0:
            context = msgs[max(0, i-10):i]  # последние 10 сообщений до твоего ответа
            yield {
                "instruction": "Ответь как пользователь, продолжая переписку",
                "input": format_dialog(context),
                "output": msg.text,
            }
```

Целевой объём: 3000–10 000 пар. Меньше — стиль не схватится; больше — отлично.

Формат сохранения: JSONL для совместимости с `axolotl` / `unsloth`.

## Шаг 2. Выбор базовой модели

Рекомендации (от лучшей к самой лёгкой):

| Модель | VRAM (QLoRA) | Качество русского |
|---|---|---|
| Qwen 2.5 14B Instruct | 24 GB | отличное |
| Qwen 2.5 7B Instruct | 12 GB | очень хорошее |
| Llama 3.1 8B Instruct | 12 GB | хорошее |
| Gemma 2 9B | 14 GB | хорошее |
| Mistral 7B v0.3 | 10 GB | среднее на русском |

Для коротких разговорных реплик 7B хватит за глаза.

## Шаг 3. Обучение (unsloth)

`unsloth` даёт 2x скорость и 2x меньше памяти.

```python
from unsloth import FastLanguageModel
from trl import SFTTrainer
from transformers import TrainingArguments

model, tokenizer = FastLanguageModel.from_pretrained(
    "unsloth/Qwen2.5-7B-Instruct-bnb-4bit",
    max_seq_length=2048,
    load_in_4bit=True,
)
model = FastLanguageModel.get_peft_model(
    model,
    r=16, lora_alpha=16,
    target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                    "gate_proj", "up_proj", "down_proj"],
)

trainer = SFTTrainer(
    model=model,
    tokenizer=tokenizer,
    train_dataset=dataset,
    max_seq_length=2048,
    args=TrainingArguments(
        per_device_train_batch_size=2,
        gradient_accumulation_steps=4,
        warmup_steps=10,
        num_train_epochs=3,
        learning_rate=2e-4,
        fp16=True,
        output_dir="./outputs",
    ),
)
trainer.train()
model.save_pretrained("tg_style_lora")
```

На RTX 4090 (24GB) Qwen 2.5 7B на 5000 примерах учится ~2–4 часа.

## Шаг 4. Инференс через vLLM

```bash
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --enable-lora \
  --lora-modules tg_style=./tg_style_lora \
  --max-model-len 4096 \
  --port 8000
```

## Шаг 5. Подмена клиента в generator

В `src/generator/generate_drafts.py` заменить `AsyncAnthropic` на OpenAI-совместимого клиента к локальному vLLM:

```python
from openai import AsyncOpenAI
client = AsyncOpenAI(base_url="http://localhost:8000/v1", api_key="dummy")
```

API почти идентичный, промпт-шаблон остаётся тем же.

## Альтернатива: облачный GPU

Если своей карты нет — vast.ai или runpod.io. RTX 4090 ~$0.4–0.7/час, обучение обойдётся в $2–5 за прогон.
