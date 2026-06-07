"""
Простой детектор языка для определения language_hint у контакта.

Не лингвистический NLP — простая эвристика:
  - >70% кириллицы → ru или uz_cyrl (отличаем по маркер-словам)
  - >70% латиницы → uz_latn или en (отличаем по узбек-маркерам)
  - смесь / мало текста → null (используем дефолт)
"""
import re


UZBEK_CYRL_MARKERS = [
    "ҳ", "ў", "қ", "ғ",       # буквы которых нет в русском
    "салом", "раҳмат", "қалай",
    "сиз", "бўлади", "керак",
]
UZBEK_LATN_MARKERS = [
    "salom", "rahmat", "qalay",
    "ko'r", "bo'l", "qil",
    "siz", "shu", "yoki", "uchun",
]


def detect_language(text: str) -> str | None:
    """
    text — конкатенация последних N сообщений контакта.
    Возвращает 'ru' | 'uz_cyrl' | 'uz_latn' | None.
    """
    if not text or len(text) < 50:
        return None

    lower = text.lower()
    cyrl_count = len(re.findall(r"[а-яёҳўқғ]", lower))
    latn_count = len(re.findall(r"[a-zo']", lower))
    total_letters = cyrl_count + latn_count
    if total_letters == 0:
        return None

    cyrl_ratio = cyrl_count / total_letters

    if cyrl_ratio > 0.7:
        # кириллица — это либо ru либо uz_cyrl
        if any(mark in lower for mark in UZBEK_CYRL_MARKERS):
            return "uz_cyrl"
        return "ru"

    if cyrl_ratio < 0.3:
        # латиница — это либо uz_latn либо en
        if any(mark in lower for mark in UZBEK_LATN_MARKERS):
            return "uz_latn"
        # en мы шаблонами не покрываем → null чтобы упасть в дефолт ru
        return None

    return None  # смешанное — дефолт
