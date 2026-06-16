"""Сборка позиций КП: модель строки, парсер «своей» позиции, расчёт ИТОГО."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import CatalogItem


@dataclass
class KpItem:
    """Одна строка КП."""
    name: str
    desc: str
    price_label: str   # как показываем: «27 000», «от 32 000», «~5 000»
    price_value: int   # число для суммы
    approximate: bool = False

    @classmethod
    def from_catalog(cls, c: CatalogItem) -> KpItem:
        return cls(
            name=c.name,
            desc=c.desc,
            price_label=c.price_label,
            price_value=c.price_value,
            approximate=c.approximate,
        )


def format_price(n: int) -> str:
    """12345 -> «12 345» (разряды через пробел, как в шаблоне)."""
    return f"{n:,}".replace(",", " ")


def parse_custom_item(text: str) -> KpItem | None:
    """Разобрать «своя позиция» в формате `Услуга - цена`.

    Берём последнее число в строке как цену, всё до него (без хвостовых
    разделителей) — наименование. Терпим «руб/₽/р», пробелы в числе,
    разные тире. Возвращаем None, если нет названия или числа.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    m = re.search(r"(\d[\d\s]*)\s*(?:руб\.?|₽|р\.?)?\s*$", raw, flags=re.IGNORECASE)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    if not digits:
        return None
    price_value = int(digits)
    name = raw[: m.start()].strip().rstrip("-—–:").strip()
    if not name or price_value <= 0:
        return None
    return KpItem(
        name=name,
        desc="",
        price_label=format_price(price_value),
        price_value=price_value,
    )


def total_label(items: list[KpItem]) -> str:
    """ИТОГО: сумма чисел; «от …», если хоть одна позиция приблизительная."""
    total = sum(it.price_value for it in items)
    approx = any(it.approximate for it in items)
    prefix = "от " if approx else ""
    return f"{prefix}{format_price(total)} руб."
