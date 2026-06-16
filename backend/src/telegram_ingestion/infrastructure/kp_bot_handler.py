"""Telegram-роутер фичи «Создать КП» (коммерческое предложение).

Изолирован: свой Router, свой StatesGroup, все callback_data с префиксом
`kp:`. Ничего из основного bot_handler не трогает. PDF-движок подгружается
лениво в render_pdf — отсутствие системных либ не ломает остальной бот.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from telegram_ingestion.application.ports import UserProfilePort

from .kp.builder import KpItem, parse_custom_item, total_label
from .kp.catalog import CATALOG, get_catalog_item

logger = logging.getLogger(__name__)


class KpStates(StatesGroup):
    building = State()       # экран сборки: каталог + текущие позиции
    custom_input = State()   # ждём «Услуга - цена»


def _items_from_data(data: dict) -> list[KpItem]:
    return [KpItem(**d) for d in data.get("kp_items", [])]


def _items_to_data(items: list[KpItem]) -> list[dict]:
    return [
        {
            "name": it.name,
            "desc": it.desc,
            "price_label": it.price_label,
            "price_value": it.price_value,
            "approximate": it.approximate,
        }
        for it in items
    ]


def _short(text: str, n: int = 30) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _builder_text(items: list[KpItem]) -> str:
    if not items:
        return (
            "🧾 <b>Сборка КП</b>\n\n"
            "Пока пусто. Выберите позиции из прайса или добавьте свою "
            "(кнопка ниже)."
        )
    lines = ["🧾 <b>Сборка КП</b>\n", "<b>Позиции:</b>"]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it.name} — {it.price_label}")
    lines.append(f"\n<b>ИТОГО:</b> {total_label(items)}")
    return "\n".join(lines)


def _builder_kb(items: list[KpItem]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    # Кнопки удаления текущих позиций (по номеру).
    for i, it in enumerate(items):
        rows.append([InlineKeyboardButton(
            text=f"✖ {i + 1}. {_short(it.name)}",
            callback_data=f"kp:rm:{i}",
        )])
    # Прайс-каталог.
    for c in CATALOG:
        rows.append([InlineKeyboardButton(
            text=f"➕ {_short(c.name, 34)} · {c.price_label}",
            callback_data=f"kp:add:{c.id}",
        )])
    rows.append([InlineKeyboardButton(text="✍️ Добавить свою позицию", callback_data="kp:custom")])
    if items:
        rows.append([InlineKeyboardButton(text="👁 Превью / PDF", callback_data="kp:preview")])
    rows.append([InlineKeyboardButton(text="✕ Отмена", callback_data="kp:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _preview_text(items: list[KpItem]) -> str:
    lines = ["📄 <b>Коммерческое предложение</b>\n"]
    for i, it in enumerate(items, 1):
        desc = f"\n   <i>{it.desc}</i>" if it.desc else ""
        lines.append(f"{i}. {it.name} — {it.price_label}{desc}")
    lines.append(f"\n<b>ИТОГО:</b> {total_label(items)}")
    return "\n".join(lines)


def _preview_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="kp:edit")],
        [InlineKeyboardButton(text="📄 Получить PDF", callback_data="kp:pdf")],
        [InlineKeyboardButton(text="✕ Отмена", callback_data="kp:cancel")],
    ])


async def _safe_edit(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    """edit_text с подавлением «message is not modified» и прочих API-ошибок."""
    if callback.message is None:
        return
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramAPIError:
        pass


def create_kp_router(user_port: UserProfilePort) -> Router:
    router = Router(name="kp")

    async def _authorized(telegram_id: int | None) -> bool:
        if telegram_id is None:
            return False
        try:
            return await user_port.is_authorized(telegram_id)
        except Exception:
            logger.exception("KP: is_authorized failed for %s", telegram_id)
            return False

    @router.message(Command("create_kp"))
    async def cmd_create_kp(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        if not await _authorized(message.from_user.id):
            try:
                await message.answer("❌ Вы не авторизованы. Обратитесь к администратору.")
            except TelegramAPIError:
                pass
            return
        await state.set_state(KpStates.building)
        await state.update_data(kp_items=[])
        try:
            await message.answer(_builder_text([]), reply_markup=_builder_kb([]))
        except TelegramAPIError:
            logger.warning("KP: failed to send builder to chat %s", message.chat.id)

    @router.callback_query(KpStates.building, F.data.startswith("kp:add:"))
    async def cb_add(callback: CallbackQuery, state: FSMContext) -> None:
        item_id = (callback.data or "").split(":", 2)[-1]
        c = get_catalog_item(item_id)
        data = await state.get_data()
        items = _items_from_data(data)
        if c is not None:
            items.append(KpItem.from_catalog(c))
            await state.update_data(kp_items=_items_to_data(items))
        await callback.answer("Добавлено")
        await _safe_edit(callback, _builder_text(items), _builder_kb(items))

    @router.callback_query(KpStates.building, F.data.startswith("kp:rm:"))
    async def cb_remove(callback: CallbackQuery, state: FSMContext) -> None:
        try:
            idx = int((callback.data or "").split(":", 2)[-1])
        except ValueError:
            await callback.answer()
            return
        data = await state.get_data()
        items = _items_from_data(data)
        if 0 <= idx < len(items):
            items.pop(idx)
            await state.update_data(kp_items=_items_to_data(items))
        await callback.answer("Удалено")
        await _safe_edit(callback, _builder_text(items), _builder_kb(items))

    @router.callback_query(KpStates.building, F.data == "kp:custom")
    async def cb_custom(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(KpStates.custom_input)
        await callback.answer()
        if callback.message is not None:
            try:
                await callback.message.answer(
                    "✍️ Напишите позицию в формате <b>Услуга - цена</b>\n"
                    "Например: <code>Установка 1С - 7000</code>"
                )
            except TelegramAPIError:
                pass

    @router.message(KpStates.custom_input, F.text)
    async def handle_custom(message: Message, state: FSMContext) -> None:
        item = parse_custom_item(message.text or "")
        if item is None:
            try:
                await message.answer(
                    "Не понял цену. Формат: <b>Услуга - цена</b>, "
                    "например <code>Сканер - 5000</code>."
                )
            except TelegramAPIError:
                pass
            return
        data = await state.get_data()
        items = _items_from_data(data)
        items.append(item)
        await state.update_data(kp_items=_items_to_data(items))
        await state.set_state(KpStates.building)
        try:
            await message.answer(_builder_text(items), reply_markup=_builder_kb(items))
        except TelegramAPIError:
            pass

    @router.callback_query(KpStates.building, F.data == "kp:preview")
    async def cb_preview(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        items = _items_from_data(data)
        await callback.answer()
        if not items:
            return
        await _safe_edit(callback, _preview_text(items), _preview_kb())

    @router.callback_query(F.data == "kp:edit")
    async def cb_edit(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(KpStates.building)
        data = await state.get_data()
        items = _items_from_data(data)
        await callback.answer()
        await _safe_edit(callback, _builder_text(items), _builder_kb(items))

    @router.callback_query(F.data == "kp:pdf")
    async def cb_pdf(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        items = _items_from_data(data)
        if not items:
            await callback.answer("Нет позиций", show_alert=True)
            return
        await callback.answer("Генерирую PDF…")
        try:
            from .kp.render import render_pdf
            pdf_bytes = render_pdf(items)
        except Exception:
            logger.exception("KP: PDF render failed")
            if callback.message is not None:
                try:
                    await callback.message.answer(
                        "⚠️ Не удалось сгенерировать PDF. Попробуйте позже."
                    )
                except TelegramAPIError:
                    pass
            return
        if callback.message is not None:
            try:
                await callback.message.answer_document(
                    BufferedInputFile(pdf_bytes, filename="Коммерческое_предложение.pdf"),
                    caption="📄 Коммерческое предложение",
                )
            except TelegramAPIError:
                logger.warning("KP: failed to send PDF document")
        await state.clear()

    @router.callback_query(F.data == "kp:cancel")
    async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer("Отменено")
        await _safe_edit(
            callback, "✕ Создание КП отменено.",
            InlineKeyboardMarkup(inline_keyboard=[]),
        )

    return router
