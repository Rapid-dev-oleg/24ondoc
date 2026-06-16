"""КП-фича: парсер «своей» позиции, расчёт ИТОГО, заполнение HTML-шаблона."""
from __future__ import annotations

import pytest

from src.telegram_ingestion.infrastructure.kp.builder import (
    KpItem,
    format_price,
    parse_custom_item,
    total_label,
)
from src.telegram_ingestion.infrastructure.kp.catalog import CATALOG, get_catalog_item
from src.telegram_ingestion.infrastructure.kp.render import fill_html

# ---- parse_custom_item ----

@pytest.mark.parametrize("text,name,value", [
    ("Сканер - 5000", "Сканер", 5000),
    ("Установка 1С - 7000 руб", "Установка 1С", 7000),
    ("Касса 12 000", "Касса", 12000),
    ("Настройка — 3500₽", "Настройка", 3500),
    ("Доставка: 1500 р.", "Доставка", 1500),
])
def test_parse_custom_ok(text: str, name: str, value: int) -> None:
    item = parse_custom_item(text)
    assert item is not None
    assert item.name == name
    assert item.price_value == value


@pytest.mark.parametrize("text", ["", "   ", "просто текст без цены", "-", "0 руб"])
def test_parse_custom_rejects_garbage(text: str) -> None:
    assert parse_custom_item(text) is None


def test_format_price() -> None:
    assert format_price(5000) == "5 000"
    assert format_price(32000) == "32 000"
    assert format_price(2000) == "2 000"


# ---- total ----

def test_total_plain_sum() -> None:
    items = [
        KpItem("A", "", "2 000", 2000),
        KpItem("B", "", "27 000", 27000),
    ]
    assert total_label(items) == "29 000 руб."


def test_total_marks_approximate() -> None:
    items = [
        KpItem("A", "", "2 000", 2000),
        KpItem("B", "", "от 32 000", 32000, approximate=True),
    ]
    assert total_label(items) == "от 34 000 руб."


# ---- catalog ----

def test_catalog_lookup() -> None:
    assert get_catalog_item("egais") is not None
    assert get_catalog_item("nope") is None
    # каждый пункт каталога конвертируется в строку КП
    for c in CATALOG:
        it = KpItem.from_catalog(c)
        assert it.name == c.name and it.price_value == c.price_value


# ---- fill_html ----

def _two_items() -> list[KpItem]:
    return [
        KpItem.from_catalog(get_catalog_item("egais")),       # 2000
        KpItem.from_catalog(get_catalog_item("kassa_new")),   # от 32000, approx
    ]


def test_fill_html_renders_rows_and_total() -> None:
    html = fill_html(_two_items(), date="16.06.2026")
    # обе позиции на месте
    assert "Регистрация в ЕГАИС и Честный Знак" in html
    assert "Касса новая + фискальный накопитель" in html
    # ровно 2 строки таблицы (по числу позиций; комментарии-инструкции вырезаны)
    assert html.count('class="num-cell"') == 2
    # итог с «от»
    assert "от 34 000 руб." in html
    # дата подставлена, плейсхолдеров не осталось
    assert "Дата: 16.06.2026" in html
    assert "{{" not in html


def test_fill_html_escapes_custom_html() -> None:
    item = parse_custom_item("Установка <b>1С</b> - 7000")
    assert item is not None
    html = fill_html([item])
    assert "<b>1С</b>" not in html          # сырой тег не просочился
    assert "&lt;b&gt;1С&lt;/b&gt;" in html  # экранирован


def test_fill_html_empty_raises() -> None:
    with pytest.raises(ValueError):
        fill_html([])


# ---- PDF smoke (skip если системные либы WeasyPrint недоступны) ----

def test_render_pdf_smoke() -> None:
    pytest.importorskip("weasyprint")
    from src.telegram_ingestion.infrastructure.kp.render import render_pdf
    try:
        pdf = render_pdf(_two_items(), date="16.06.2026")
    except OSError:
        pytest.skip("WeasyPrint system libraries not installed")
    assert pdf[:4] == b"%PDF"
