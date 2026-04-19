"""Telegram /stats command — reports UI with role-based branching.

AGENT:  /stats → period picker → own personal summary.
ADMIN:  /stats → employee picker (paginated) or "📊 Общий" → period picker.

Custom period entry is accepted as "DD.MM - DD.MM" or "DD.MM.YYYY - DD.MM.YYYY".
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from reports.application.generate_report import GenerateReport
from reports.domain.models import ReportScope

from .reports_formatter import format_report, split_for_telegram

logger = logging.getLogger(__name__)

_PAGE_SIZE = 8


class ReportsStates(StatesGroup):
    choosing_scope = State()
    choosing_period = State()
    awaiting_custom_period = State()


def _period_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Сегодня", callback_data="rp_period:today"),
                InlineKeyboardButton(text="Неделя", callback_data="rp_period:week"),
            ],
            [
                InlineKeyboardButton(text="Месяц", callback_data="rp_period:month"),
                InlineKeyboardButton(text="Свой период", callback_data="rp_period:custom"),
            ],
        ]
    )


def _scope_keyboard(agents: list[Any], page: int, total: int) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text="📊 Общий", callback_data="rp_scope:overall")]
    ]
    start = page * _PAGE_SIZE
    for a in agents[start:start + _PAGE_SIZE]:
        label = (getattr(a, "full_name", None) or str(getattr(a, "telegram_id", "?")))[:40]
        buttons.append([
            InlineKeyboardButton(
                text=f"👤 {label}", callback_data=f"rp_scope:user:{a.telegram_id}"
            )
        ])
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"rp_page:{page - 1}"))
    if (page + 1) * _PAGE_SIZE < total:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"rp_page:{page + 1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _period_for(code: str) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if code == "today":
        return today, now
    if code == "week":
        return today - timedelta(days=7), now
    if code == "month":
        return today - timedelta(days=30), now
    raise ValueError(f"unknown period code {code!r}")


_DATE_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*-\s*"
                      r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*$")


def parse_custom_period(text: str) -> tuple[datetime, datetime] | None:
    m = _DATE_RE.match(text)
    if not m:
        return None
    d1, mo1, y1, d2, mo2, y2 = m.groups()
    year_now = datetime.now(UTC).year
    y1_i = int(y1) if y1 else year_now
    y2_i = int(y2) if y2 else year_now
    if y1_i < 100:
        y1_i += 2000
    if y2_i < 100:
        y2_i += 2000
    try:
        start = datetime(y1_i, int(mo1), int(d1), tzinfo=UTC)
        end = datetime(y2_i, int(mo2), int(d2), 23, 59, 59, tzinfo=UTC)
    except ValueError:
        return None
    if end < start:
        return None
    return start, end


def create_reports_router(
    generate_report: GenerateReport,
    user_port: Any,
    is_admin_fn,
) -> Router:
    """Create the /stats router. Caller injects dependencies."""
    router = Router(name="reports")

    @router.message(Command("stats"))
    async def cmd_stats(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None:
            await message.answer("🔒 Сначала зарегистрируйтесь в боте.")
            return
        await state.clear()
        if is_admin_fn(profile):
            agents = await user_port.list_active_agents()
            await state.set_state(ReportsStates.choosing_scope)
            await state.update_data(agents=[
                {
                    "telegram_id": a.telegram_id,
                    "full_name": getattr(a, "full_name", None),
                    "twenty_member_id": getattr(a, "twenty_member_id", None),
                }
                for a in agents
            ])
            await message.answer(
                "📊 Выберите, по кому сформировать отчёт:",
                reply_markup=_scope_keyboard(agents, page=0, total=len(agents)),
            )
            return
        # AGENT — straight to period picker, scope = SELF
        await state.set_state(ReportsStates.choosing_period)
        await state.update_data(
            scope="self", target_user_id=profile.twenty_member_id,
        )
        await message.answer("📊 За какой период?", reply_markup=_period_keyboard())

    @router.callback_query(F.data.startswith("rp_page:"))
    async def cb_page(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        agents = [_AgentProxy(**a) for a in data.get("agents", [])]
        page = int(callback.data.split(":")[1])  # type: ignore[union-attr]
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_reply_markup(
                reply_markup=_scope_keyboard(agents, page=page, total=len(agents))
            )

    @router.callback_query(F.data.startswith("rp_scope:"))
    async def cb_scope(callback: CallbackQuery, state: FSMContext) -> None:
        parts = callback.data.split(":")  # type: ignore[union-attr]
        if len(parts) == 2 and parts[1] == "overall":
            await state.update_data(scope="overall", target_user_id=None)
        elif len(parts) == 3 and parts[1] == "user":
            # map telegram_id from the button back to the agent's twenty_member_id
            data = await state.get_data()
            tg_id = int(parts[2])
            wmid: str | None = None
            for a in data.get("agents", []):
                if a.get("telegram_id") == tg_id:
                    wmid = a.get("twenty_member_id")
                    break
            await state.update_data(scope="employee", target_user_id=wmid)
        else:
            await callback.answer("❌")
            return
        await state.set_state(ReportsStates.choosing_period)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "📅 Выберите период:", reply_markup=_period_keyboard()
            )

    @router.callback_query(F.data.startswith("rp_period:"))
    async def cb_period(callback: CallbackQuery, state: FSMContext) -> None:
        code = callback.data.split(":")[1]  # type: ignore[union-attr]
        if code == "custom":
            await state.set_state(ReportsStates.awaiting_custom_period)
            await callback.answer()
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    "Введите период сообщением в формате "
                    "<code>ДД.ММ - ДД.ММ</code> или <code>ДД.ММ.ГГГГ - ДД.ММ.ГГГГ</code>.",
                    parse_mode="HTML",
                )
            return
        try:
            from_ts, to_ts = _period_for(code)
        except ValueError:
            await callback.answer("❌ Неизвестный период.")
            return
        await _render_and_send(callback, state, from_ts, to_ts, generate_report)

    @router.message(ReportsStates.awaiting_custom_period, F.text)
    async def handle_custom(message: Message, state: FSMContext) -> None:
        parsed = parse_custom_period(message.text or "")
        if parsed is None:
            await message.answer(
                "Не удалось прочитать период. Формат: <code>01.04 - 19.04</code> "
                "или <code>01.04.2026 - 19.04.2026</code>.",
                parse_mode="HTML",
            )
            return
        from_ts, to_ts = parsed
        data = await state.get_data()
        scope = ReportScope(data.get("scope", "self"))
        target = data.get("target_user_id")
        dto = await generate_report.execute(
            scope=scope, from_ts=from_ts, to_ts=to_ts, user_id=target
        )
        for chunk in split_for_telegram(format_report(dto)):
            await message.answer(chunk, parse_mode="HTML")
        await state.clear()

    async def _render_and_send(
        callback: CallbackQuery,
        state: FSMContext,
        from_ts: datetime,
        to_ts: datetime,
        generate: GenerateReport,
    ) -> None:
        data = await state.get_data()
        scope = ReportScope(data.get("scope", "self"))
        target = data.get("target_user_id")
        dto = await generate.execute(scope=scope, from_ts=from_ts, to_ts=to_ts, user_id=target)
        html = format_report(dto)
        chunks = split_for_telegram(html)
        await callback.answer()
        if isinstance(callback.message, Message):
            first, *rest = chunks
            await callback.message.edit_text(first, parse_mode="HTML")
            for c in rest:
                await callback.message.answer(c, parse_mode="HTML")
        await state.clear()

    return router


class _AgentProxy:
    """Tiny struct to restore agent shape across FSM serialisation."""

    def __init__(
        self,
        telegram_id: int,
        full_name: str | None = None,
        twenty_member_id: str | None = None,
    ) -> None:
        self.telegram_id = telegram_id
        self.full_name = full_name
        self.twenty_member_id = twenty_member_id
