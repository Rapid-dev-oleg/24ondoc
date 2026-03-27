"""Telegram Ingestion — aiogram 3.x Bot Handler."""

from __future__ import annotations

import io

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..application.use_cases import (
    AddTextContentUseCase,
    AddVoiceContentUseCase,
    CancelSessionUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)


class TelegramFSMStates(StatesGroup):
    collecting = State()
    analyzing = State()
    preview = State()


def _collect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📎 Собрать", callback_data="collect")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel")],
        ]
    )


def _preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать в CRM", callback_data="create_crm")],
            [InlineKeyboardButton(text="✏️ Добавить/Изменить", callback_data="edit_session")],
            [InlineKeyboardButton(text="🔄 Переанализировать", callback_data="reanalyze")],
            [InlineKeyboardButton(text="❌ Удалить черновик", callback_data="cancel")],
        ]
    )


def create_router(
    start_session: StartSessionUseCase,
    add_text: AddTextContentUseCase,
    add_voice: AddVoiceContentUseCase,
    trigger_analysis: TriggerAnalysisUseCase,
    cancel_session: CancelSessionUseCase,
) -> Router:
    """Create and configure the telegram ingestion router with injected use cases."""
    router = Router(name="telegram_ingestion")

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.clear()
        await message.answer(
            "👋 Добро пожаловать в 24ondoc!\nИспользуйте /new_task чтобы создать новую задачу."
        )

    @router.message(Command("new_task"))
    async def cmd_new_task(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        session = await start_session.execute(message.from_user.id)
        if session is None:
            await message.answer("❌ Вы не авторизованы. Обратитесь к администратору.")
            return
        await state.set_state(TelegramFSMStates.collecting)
        await state.update_data(session_id=str(session.session_id))
        await message.answer(
            "📝 Опишите задачу. Отправляйте текст, голосовые сообщения, фото и файлы.\n"
            "Нажмите '📎 Собрать' когда закончите.",
            reply_markup=_collect_keyboard(),
        )

    @router.message(TelegramFSMStates.collecting, F.text)
    async def handle_text(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        await add_text.execute(message.from_user.id, message.text)
        await message.answer("✅ Текст добавлен.", reply_markup=_collect_keyboard())

    @router.message(TelegramFSMStates.collecting, F.voice)
    async def handle_voice(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.voice is None:
            return
        file_id = message.voice.file_id
        tg_file = await bot.get_file(file_id)
        if tg_file.file_path is None:
            await message.answer("❌ Не удалось получить файл.")
            return
        file_io: io.BytesIO | None = await bot.download_file(tg_file.file_path)
        if file_io is None:
            await message.answer("❌ Не удалось скачать файл.")
            return
        await add_voice.execute(message.from_user.id, file_id, file_io.read())
        await message.answer(
            "🎤 Голосовое сообщение транскрибировано и добавлено.",
            reply_markup=_collect_keyboard(),
        )

    @router.message(TelegramFSMStates.collecting, F.photo)
    async def handle_photo(message: Message, state: FSMContext) -> None:
        if message.from_user is None or not message.photo:
            return
        caption = message.caption or ""
        text = "[Фото]"
        if caption:
            text += f" {caption}"
        await add_text.execute(message.from_user.id, text)
        await message.answer("🖼 Фото добавлено.", reply_markup=_collect_keyboard())

    @router.message(TelegramFSMStates.collecting, F.document)
    async def handle_document(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.document is None:
            return
        file_name = message.document.file_name or "файл"
        caption = message.caption or ""
        text = f"[Файл: {file_name}]"
        if caption:
            text += f" {caption}"
        await add_text.execute(message.from_user.id, text)
        await message.answer("📄 Файл добавлен.", reply_markup=_collect_keyboard())

    @router.callback_query(TelegramFSMStates.collecting, F.data == "collect")
    async def callback_collect(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        session = await trigger_analysis.execute(callback.from_user.id)
        if session is None:
            await callback.answer("❌ Сессия не найдена.")
            return
        await state.set_state(TelegramFSMStates.analyzing)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text("⏳ Анализирую... Пожалуйста, подождите.")

    @router.callback_query(F.data == "cancel")
    async def callback_cancel(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        await cancel_session.execute(callback.from_user.id)
        await state.clear()
        await callback.answer("Черновик удалён.")
        if isinstance(callback.message, Message):
            await callback.message.edit_text("❌ Черновик удалён.")

    @router.callback_query(TelegramFSMStates.preview, F.data == "edit_session")
    async def callback_edit(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        await state.set_state(TelegramFSMStates.collecting)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "✏️ Добавьте дополнительный контент.",
                reply_markup=_collect_keyboard(),
            )

    @router.callback_query(TelegramFSMStates.preview, F.data == "reanalyze")
    async def callback_reanalyze(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        session = await trigger_analysis.execute(callback.from_user.id)
        if session is None:
            await callback.answer("❌ Сессия не найдена.")
            return
        await state.set_state(TelegramFSMStates.analyzing)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text("⏳ Переанализирую...")

    return router
