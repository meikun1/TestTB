"""
Банк шаблонов для рассылки.

Сегментация по language_hint контакта (если есть):
  - 'ru'      → русские шаблоны (~60% корпуса)
  - 'uz_latn' → узбекские латиницей (~30%)
  - 'uz_cyrl' → узбекские кириллицей (~10%)
  - null      → дефолт ru

ВАЖНО: узбекские шаблоны нужно проверить с носителем перед production.
Тут placeholder-варианты, машинно-плавные но не идеальные.
"""
import random


TEMPLATES_RU = [
    "Привет, {name}! Глянь, кинул тебе тут.",
    "{name}, привет, давно не общались. Вот посмотри.",
    "О, {name}, привет. Кидаю тебе.",
    "{name}, привет. Глянь когда будет минута.",
    "Йо, {name}. Скидываю тебе тут.",
    "{name}, добрый день. Думаю тебе зайдёт, посмотри.",
    "Слушай, {name}, привет. Скинул тебе тут.",
    "{name}, привет! Кидаю.",
    "{name}, хей. Посмотри в свободную минуту.",
    "{name}, привет, держи, должно тебе зайти.",
    "Йо {name}, как ты? Кидаю тебе.",
    "Привет, {name}, у меня для тебя кое-что.",
    "{name}, хей, глянь.",
    "{name}, я тут вот, держи.",
    "Привет {name}! Подумал про тебя — кидаю.",
]


TEMPLATES_UZ_LATN = [
    "Salom, {name}! Buni ko'r-chi.",
    "{name}, salom, qaranglar.",
    "Hey, {name}, men buni yubordim.",
    "{name}, salom, vaqting bo'lganda ko'r.",
    "Salom {name}! Senga foydali bo'lar.",
    "{name}, salom, buni ham ko'r.",
    "Yo, {name}, men buni yubordim.",
    "Salom, {name}, qaragin.",
]


TEMPLATES_UZ_CYRL = [
    "Салом, {name}! Буни кўр-чи.",
    "{name}, салом, қаранг.",
    "Ҳей, {name}, мен буни юбордим.",
    "{name}, салом, вақтинг бўлганда кўр.",
    "Салом, {name}, шуни кўрсанг.",
]


LOCAL_NAMES_M = [
    "Otabek", "Aziz", "Botir", "Sherzod", "Sardor",
    "Sanjar", "Nodir", "Bekzod", "Akmal", "Jasur",
]
LOCAL_NAMES_F = [
    "Aziza", "Madina", "Nodira", "Dilnoza", "Malika",
    "Zarina", "Shahnoza", "Gulnoza", "Sevara", "Kamola",
]


def pick_template(language_hint: str | None) -> str:
    if language_hint == "uz_latn":
        return random.choice(TEMPLATES_UZ_LATN)
    if language_hint == "uz_cyrl":
        return random.choice(TEMPLATES_UZ_CYRL)
    # ru или null — русские (доминирующий для UZ-бизнеса)
    return random.choice(TEMPLATES_RU)


def pick_local_name(seed: int | None = None) -> str:
    """Если у контакта нет first_name — подставить что-то локально-знакомое.
    Парность М/Ж не определяем — берём из общего пула."""
    rnd = random.Random(seed) if seed is not None else random
    return rnd.choice(LOCAL_NAMES_M + LOCAL_NAMES_F)


def render(template: str, name: str | None) -> str:
    """Подставляет имя в шаблон. Если имени нет — убирает запятую и пробелы корректно."""
    if name:
        return template.format(name=name)
    # без имени: «Привет, {name}!» → «Привет!»
    text = template.replace(", {name}", "").replace(" {name}", "").replace("{name}", "")
    # подчистка двойных пробелов / висящих знаков
    text = " ".join(text.split())
    return text
