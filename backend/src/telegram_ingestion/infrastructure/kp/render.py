"""Заполнение HTML-шаблона КП и рендер в PDF.

PDF-движок (WeasyPrint) импортируется ЛЕНИВО внутри `render_pdf`, чтобы
отсутствие системных библиотек не ломало импорт модуля/бота и тесты —
сборка HTML и парсинг работают без него.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime
from pathlib import Path

from .builder import KpItem, total_label

ASSETS_DIR = Path(__file__).parent / "assets"
TEMPLATE_PATH = ASSETS_DIR / "template_kp.html"

# Реальная строка таблицы в шаблоне (после удаления HTML-комментариев она
# единственная, что содержит {{N}}).
_ROW_RE = re.compile(
    r"<tr>\s*<td class=\"num-cell\">\{\{N\}\}.*?</tr>",
    re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DATE_RE = re.compile(r"Дата:\s*_+")


def _load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def fill_html(items: list[KpItem], *, date: str | None = None) -> str:
    """Подставить позиции в шаблон. Возвращает готовый HTML."""
    if not items:
        raise ValueError("КП без позиций")

    tpl = _load_template()
    # Убираем HTML-комментарии (в т.ч. блок-инструкцию) — иначе плейсхолдеры
    # дублируются и в комментарии.
    tpl = _COMMENT_RE.sub("", tpl)

    row_match = _ROW_RE.search(tpl)
    if row_match is None:
        raise RuntimeError("Не найдена шаблонная строка таблицы в template_kp.html")
    row_tpl = row_match.group(0)

    rows: list[str] = []
    for i, it in enumerate(items, start=1):
        row = (
            row_tpl
            .replace("{{N}}", str(i))
            .replace("{{NAME}}", _html.escape(it.name))
            .replace("{{DESC}}", _html.escape(it.desc))
            .replace("{{PRICE}}", _html.escape(it.price_label))
        )
        rows.append(row)

    tpl = tpl.replace(row_tpl, "\n".join(rows))
    tpl = tpl.replace("{{TOTAL}}", _html.escape(total_label(items)))

    date_str = date or datetime.now().strftime("%d.%m.%Y")
    tpl = _DATE_RE.sub(f"Дата: {date_str}", tpl)
    return tpl


def render_pdf(items: list[KpItem], *, date: str | None = None) -> bytes:
    """Сгенерировать PDF из заполненного шаблона (WeasyPrint, ленивый импорт)."""
    filled = fill_html(items, date=date)
    from weasyprint import HTML  # lazy: системные либы нужны только тут

    # base_url = папка assets, чтобы <img src="ONdoc_logo.png"> резолвился.
    return HTML(string=filled, base_url=str(ASSETS_DIR)).write_pdf()
