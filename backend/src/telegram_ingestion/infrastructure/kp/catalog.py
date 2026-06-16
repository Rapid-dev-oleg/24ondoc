"""Прайс-каталог для КП (v1 — зашит в код; правится здесь).

Каждая позиция = одна строка в КП. `price_label` показывается как есть
(«от 32 000», «~5 000»), `price_value` — число для ИТОГО, `approximate`
помечает прайс с «от/~», чтобы итог тоже шёл как «от …».
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CatalogItem:
    id: str
    name: str
    desc: str
    price_label: str
    price_value: int
    approximate: bool = False


CATALOG: tuple[CatalogItem, ...] = (
    CatalogItem(
        "egais",
        "Регистрация в ЕГАИС и Честный Знак",
        "работа ~1.5 часа",
        "2 000",
        2000,
    ),
    CatalogItem(
        "kassa_used",
        "Касса б/у + фискальный накопитель",
        "установим, зарегистрируем в налоговой",
        "27 000",
        27000,
    ),
    CatalogItem(
        "kassa_new",
        "Касса новая + фискальный накопитель",
        "установим, зарегистрируем",
        "от 32 000",
        32000,
        approximate=True,
    ),
    CatalogItem(
        "scanner",
        "Сканер новый",
        "",
        "~5 000",
        5000,
        approximate=True,
    ),
    CatalogItem(
        "1c",
        "Программа 1С для продажи",
        "платформа, база магазина, ЕГАИС, маркировка (пиво), "
        "оборудование: касса/сканер/банковский терминал",
        "7 000",
        7000,
    ),
)


def get_catalog_item(item_id: str) -> CatalogItem | None:
    for it in CATALOG:
        if it.id == item_id:
            return it
    return None
