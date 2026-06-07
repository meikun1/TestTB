"""Определение узбекского мобильного оператора по префиксу номера."""

# Карта префиксов после кода страны 998
# (см. https://en.wikipedia.org/wiki/Telephone_numbers_in_Uzbekistan)
UZ_CARRIER_PREFIXES = {
    "88": "uzmobile",  # Uzmobile (UMS субсидия)
    "90": "beeline",
    "91": "beeline",
    "93": "ucell",
    "94": "ucell",
    "95": "ums",
    "97": "newop",     # новые операторы (Perfectum и т.п.)
    "99": "ucell",
}


def detect_carrier(phone: str) -> str | None:
    """
    Возвращает 'beeline' | 'ucell' | 'ums' | 'uzmobile' | 'newop'
    либо None если префикс невалидный для UZ.
    """
    if not phone:
        return None
    # нормализация: только цифры
    digits = "".join(c for c in phone if c.isdigit())
    if not digits.startswith("998"):
        return None
    body = digits[3:]  # после кода страны
    if len(body) < 9:
        return None
    prefix = body[:2]
    return UZ_CARRIER_PREFIXES.get(prefix)


def is_valid_uz_number(phone: str) -> bool:
    """Жёсткая валидация: номер UZ с известным оператором."""
    return detect_carrier(phone) is not None
