"""Telegram Ingestion — aiogram 3.x Bot Handler."""

from __future__ import annotations

import io
import logging
import uuid
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ai_classification.domain.repository import AIClassificationPort
from chatwoot_integration.application.use_cases import CreateTicketFromSession

from ..application.ports import UserProfilePort
from ..application.registration_use_cases import (
    AutoRegisterUserUseCase,
    SaveVoiceSampleUseCase,
    UpdateProfileFieldUseCase,
)
from ..application.tasks_use_cases import (
    AddTaskCommentUseCase,
    GetMyTasksUseCase,
    ReassignTaskUseCase,
    UpdateTaskStatusUseCase,
)
from ..application.use_cases import (
    AddTextContentUseCase,
    AddVoiceContentUseCase,
    CancelSessionUseCase,
    SetAnalysisResultUseCase,
    StartSessionUseCase,
    TriggerAnalysisUseCase,
)
from ..domain.models import AIResult, DraftSession
from ..domain.repository import DraftSessionRepository

logger = logging.getLogger(__name__)

_CRM_URL = "https://chat.24ondoc.ru"

_TASKS_PAGE_SIZE = 5


class TelegramFSMStates(StatesGroup):
    collecting = State()
    analyzing = State()
    preview = State()
    tasks_list = State()
    task_detail = State()
    adding_comment = State()


class SettingsFSMStates(StatesGroup):
    menu = State()
    edit_name = State()
    edit_email = State()
    voice_sample = State()


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


def _format_preview(session: DraftSession) -> str:
    r = session.ai_result
    if r is None:
        return "❓ Результат анализа недоступен."
    lines = [
        "<b>📋 Preview задачи</b>",
        "",
        f"<b>Заголовок:</b> {r.title}",
        f"<b>Описание:</b> {r.description}",
        f"<b>Категория:</b> {r.category}",
        f"<b>Приоритет:</b> {r.priority}",
    ]
    if r.deadline:
        lines.append(f"<b>Дедлайн:</b> {r.deadline}")
    if r.assignee_hint:
        lines.append(f"<b>Исполнитель:</b> {r.assignee_hint}")
    return "\n".join(lines)


def create_router(
    start_session: StartSessionUseCase,
    add_text: AddTextContentUseCase,
    add_voice: AddVoiceContentUseCase,
    trigger_analysis: TriggerAnalysisUseCase,
    cancel_session: CancelSessionUseCase,
    user_port: UserProfilePort,
    auto_register: AutoRegisterUserUseCase | None = None,
    *,
    ai_port: AIClassificationPort | None = None,
    set_analysis_result: SetAnalysisResultUseCase | None = None,
    create_ticket: CreateTicketFromSession | None = None,
    draft_repo: DraftSessionRepository | None = None,
) -> Router:
    """Create and configure the telegram ingestion router with injected use cases."""
    router = Router(name="telegram_ingestion")

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.clear()

        if auto_register is not None:
            first_name = message.from_user.first_name or ""
            try:
                profile, password, is_new = await auto_register.execute(
                    message.from_user.id, first_name
                )
            except Exception:
                logger.exception("Auto-registration failed for user %s", message.from_user.id)
                await message.answer(
                    "❌ Ошибка регистрации. Попробуйте позже или обратитесь к администратору."
                )
                return
            try:
                if is_new:
                    email = profile.settings.get("email", f"{profile.telegram_id}@24ondoc.ru")
                    await message.answer(
                        "✅ Вы успешно зарегистрированы в системе 24ondoc!\n\n"
                        f"📧 Email: <code>{email}</code>\n"
                        f"🔑 Пароль: <code>{password}</code>\n"
                        f"🔗 CRM: {_CRM_URL}\n\n"
                        "Для смены пароля: войдите в CRM → Настройки профиля → Пароль.\n\n"
                        "Используйте /settings для настройки профиля."
                    )
                else:
                    await message.answer(
                        "👋 Добро пожаловать в 24ondoc!\n"
                        "Используйте /new_task чтобы создать новую задачу."
                    )
            except TelegramAPIError:
                logger.warning("Failed to send /start reply to chat %s", message.chat.id)
        elif await user_port.is_authorized(message.from_user.id):
            await message.answer(
                "👋 Добро пожаловать в 24ondoc!\nИспользуйте /new_task чтобы создать новую задачу."
            )
        else:
            await message.answer(
                "🔒 У вас нет доступа. Обратитесь к администратору для регистрации."
            )

    @router.message(Command("new_task"))
    async def cmd_new_task(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        session = await start_session.execute(message.from_user.id)
        if session is None:
            try:
                await message.answer("❌ Вы не авторизованы. Обратитесь к администратору.")
            except TelegramAPIError:
                logger.warning("Failed to send /new_task reply to chat %s", message.chat.id)
            return
        await state.set_state(TelegramFSMStates.collecting)
        await state.update_data(session_id=str(session.session_id))
        try:
            await message.answer(
                "📝 Опишите задачу. Отправляйте текст, голосовые сообщения, фото и файлы.\n"
                "Нажмите '📎 Собрать' когда закончите.",
                reply_markup=_collect_keyboard(),
            )
        except TelegramAPIError:
            logger.warning("Failed to send /new_task reply to chat %s", message.chat.id)

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
        raw_file_io = await bot.download_file(tg_file.file_path)
        if not isinstance(raw_file_io, io.BytesIO):
            await message.answer("❌ Не удалось скачать файл.")
            return
        try:
            await add_voice.execute(message.from_user.id, file_id, raw_file_io.read())
        except Exception:
            logger.exception("Voice transcription failed for user %s", message.from_user.id)
            await message.answer(
                "❌ Не удалось транскрибировать голосовое сообщение. Попробуйте ещё раз.",
                reply_markup=_collect_keyboard(),
            )
            return
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

        if ai_port is None or set_analysis_result is None:
            await state.set_state(TelegramFSMStates.collecting)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    "❌ AI-анализ недоступен. Попробуйте позже.",
                    reply_markup=_collect_keyboard(),
                )
            return

        try:
            text = session.assembled_text or ""
            classification = await ai_port.classify(text)
            ai_result = AIResult(
                title=classification.title,
                description=classification.description,
                category=str(classification.category),
                priority=str(classification.priority),
                deadline=classification.deadline,
                entities={
                    "emails": classification.entities.emails,
                    "phones": classification.entities.phones,
                    "prices": classification.entities.prices,
                    "dates": classification.entities.dates,
                },
                assignee_hint=classification.assignee_hint,
            )
            updated = await set_analysis_result.execute(session.session_id, ai_result)
            if updated is None:
                raise ValueError("Session not found after analysis")
            await state.set_state(TelegramFSMStates.preview)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _format_preview(updated),
                    reply_markup=_preview_keyboard(),
                )
        except Exception:
            await state.set_state(TelegramFSMStates.collecting)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    "❌ Ошибка анализа. Попробуйте снова.",
                    reply_markup=_collect_keyboard(),
                )

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
        if draft_repo is not None:
            session = await draft_repo.get_active_by_user(callback.from_user.id)
            if session is None:
                await callback.answer("❌ Сессия не найдена.")
                return
            session.start_editing()
            await draft_repo.save(session)
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

        if ai_port is None or set_analysis_result is None:
            await state.set_state(TelegramFSMStates.preview)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    "❌ AI-анализ недоступен. Попробуйте позже.",
                    reply_markup=_preview_keyboard(),
                )
            return

        try:
            text = session.assembled_text or ""
            classification = await ai_port.classify(text)
            ai_result = AIResult(
                title=classification.title,
                description=classification.description,
                category=str(classification.category),
                priority=str(classification.priority),
                deadline=classification.deadline,
                entities={
                    "emails": classification.entities.emails,
                    "phones": classification.entities.phones,
                    "prices": classification.entities.prices,
                    "dates": classification.entities.dates,
                },
                assignee_hint=classification.assignee_hint,
            )
            updated = await set_analysis_result.execute(session.session_id, ai_result)
            if updated is None:
                raise ValueError("Session not found after analysis")
            await state.set_state(TelegramFSMStates.preview)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _format_preview(updated),
                    reply_markup=_preview_keyboard(),
                )
        except Exception:
            await state.set_state(TelegramFSMStates.preview)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    "❌ Ошибка анализа. Попробуйте снова.",
                    reply_markup=_preview_keyboard(),
                )

    @router.callback_query(TelegramFSMStates.preview, F.data == "create_crm")
    async def callback_create_crm(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return

        if create_ticket is None or draft_repo is None:
            await callback.answer("❌ CRM-интеграция недоступна.", show_alert=True)
            return

        data = await state.get_data()
        session_id_str = data.get("session_id")
        if session_id_str is None:
            await callback.answer("❌ Сессия не найдена.", show_alert=True)
            return

        try:
            session_id = uuid.UUID(session_id_str)
            fetched = await draft_repo.get_by_id(session_id)
            if fetched is None:
                await callback.answer("❌ Сессия не найдена.", show_alert=True)
                return

            ticket = await create_ticket.execute(fetched)
            if ticket is None:
                await callback.answer("❌ Ошибка создания задачи.", show_alert=True)
                return

            await cancel_session.execute(callback.from_user.id)
            await state.clear()
            await callback.answer(f"✅ Задача #{ticket.task_id} создана в CRM!")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    f"✅ Задача <b>#{ticket.task_id}</b> создана в CRM.\n{ticket.title}"
                )
        except Exception:
            await callback.answer("❌ Ошибка создания задачи.", show_alert=True)

    return router


# ---------- Tasks Router ----------


def _tasks_list_keyboard(
    tickets: list[dict[str, Any]],
    page: int,
    total: int,
) -> InlineKeyboardMarkup:
    """Клавиатура со списком задач (5 шт.) и навигацией по страницам."""
    buttons: list[list[InlineKeyboardButton]] = []
    start = page * _TASKS_PAGE_SIZE
    end = start + _TASKS_PAGE_SIZE
    for ticket in tickets[start:end]:
        label = f"📋 #{ticket['task_id']} {ticket['title'][:40]}"
        cb_data = f"task_detail:{ticket['task_id']}:{ticket.get('assignee_chatwoot_id') or 0}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb_data)])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"tasks_page:{page - 1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton(text="▶ Далее", callback_data=f"tasks_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _task_detail_keyboard(
    task_id: int, assignee_chatwoot_id: int, status: str, is_supervisor: bool
) -> InlineKeyboardMarkup:
    """Клавиатура для детального просмотра задачи."""
    buttons: list[list[InlineKeyboardButton]] = []

    if status == "open" or status == "pending":
        buttons.append(
            [
                InlineKeyboardButton(
                    text="✅ Решить", callback_data=f"task_resolve:{task_id}:{assignee_chatwoot_id}"
                )
            ]
        )
    else:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="🔓 Открыть", callback_data=f"task_reopen:{task_id}:{assignee_chatwoot_id}"
                )
            ]
        )

    buttons.append(
        [InlineKeyboardButton(text="💬 Комментарий", callback_data=f"task_comment:{task_id}")]
    )

    if is_supervisor:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="👤 Переназначить", callback_data=f"task_reassign_list:{task_id}"
                )
            ]
        )

    buttons.append([InlineKeyboardButton(text="◀ К списку", callback_data="tasks_back")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _reassign_keyboard(task_id: int, agents: Sequence[Any]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for agent in agents:
        label = f"👤 {agent.telegram_id} (cwt:{agent.chatwoot_user_id})"
        cb = f"reassign_to:{task_id}:{agent.chatwoot_user_id}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb)])
    buttons.append([InlineKeyboardButton(text="◀ Отмена", callback_data="tasks_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def create_tasks_router(
    get_my_tasks: GetMyTasksUseCase,
    update_task_status: UpdateTaskStatusUseCase,
    reassign_task: ReassignTaskUseCase,
    add_task_comment: AddTaskCommentUseCase,
    user_port: UserProfilePort,
) -> Router:
    """Создаёт роутер для /my_tasks flow."""
    router = Router(name="tasks")

    @router.message(Command("my_tasks"))
    async def cmd_my_tasks(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        tickets = await get_my_tasks.execute(telegram_id=message.from_user.id)
        if not tickets:
            await message.answer("📭 У вас нет открытых задач.")
            return

        serialized = [
            {
                "task_id": t.task_id,
                "title": t.title,
                "status": t.status.value,
                "assignee_chatwoot_id": t.assignee_chatwoot_id,
            }
            for t in tickets
        ]
        await state.set_state(TelegramFSMStates.tasks_list)
        await state.update_data(tasks=serialized, tasks_page=0)

        keyboard = _tasks_list_keyboard(serialized, page=0, total=len(serialized))
        await message.answer(
            f"📋 Ваши задачи ({len(tickets)} шт.):",
            reply_markup=keyboard,
        )

    @router.callback_query(F.data.startswith("tasks_page:"))
    async def callback_tasks_page(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        page = int(callback.data.split(":")[1])  # type: ignore[union-attr]
        data = await state.get_data()
        tasks = data.get("tasks", [])
        await state.update_data(tasks_page=page)
        keyboard = _tasks_list_keyboard(tasks, page=page, total=len(tasks))
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"📋 Ваши задачи ({len(tasks)} шт.):",
                reply_markup=keyboard,
            )

    @router.callback_query(F.data.startswith("task_detail:"))
    async def callback_task_detail(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        parts = callback.data.split(":")  # type: ignore[union-attr]
        task_id = int(parts[1])
        assignee_chatwoot_id = int(parts[2])

        profile = await user_port.get_profile(callback.from_user.id)
        from ..domain.models import UserRole

        is_supervisor = profile is not None and profile.role in (
            UserRole.SUPERVISOR,
            UserRole.ADMIN,
        )

        data = await state.get_data()
        tasks = data.get("tasks", [])
        ticket = next((t for t in tasks if t["task_id"] == task_id), None)
        status = ticket["status"] if ticket else "open"
        title = ticket["title"] if ticket else f"Задача #{task_id}"

        await state.set_state(TelegramFSMStates.task_detail)
        await state.update_data(
            current_task_id=task_id,
            current_assignee_chatwoot_id=assignee_chatwoot_id,
            current_task_status=status,
        )

        keyboard = _task_detail_keyboard(task_id, assignee_chatwoot_id, status, is_supervisor)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"📋 Задача #{task_id}: {title}\nСтатус: {status}",
                reply_markup=keyboard,
            )

    @router.callback_query(F.data.startswith("task_resolve:"))
    async def callback_task_resolve(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        parts = callback.data.split(":")  # type: ignore[union-attr]
        task_id = int(parts[1])
        assignee_chatwoot_id = int(parts[2]) if parts[2] != "0" else None

        ok = await update_task_status.execute(
            requester_telegram_id=callback.from_user.id,
            task_id=task_id,
            assignee_chatwoot_id=assignee_chatwoot_id,
            new_status="resolved",
        )
        if ok:
            await callback.answer("✅ Задача решена!")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"✅ Задача #{task_id} решена.")
        else:
            await callback.answer("❌ Нет прав для изменения статуса.", show_alert=True)

    @router.callback_query(F.data.startswith("task_reopen:"))
    async def callback_task_reopen(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        parts = callback.data.split(":")  # type: ignore[union-attr]
        task_id = int(parts[1])
        assignee_chatwoot_id = int(parts[2]) if parts[2] != "0" else None

        ok = await update_task_status.execute(
            requester_telegram_id=callback.from_user.id,
            task_id=task_id,
            assignee_chatwoot_id=assignee_chatwoot_id,
            new_status="open",
        )
        if ok:
            await callback.answer("🔓 Задача открыта!")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"🔓 Задача #{task_id} открыта.")
        else:
            await callback.answer("❌ Нет прав для изменения статуса.", show_alert=True)

    @router.callback_query(F.data.startswith("task_reassign_list:"))
    async def callback_reassign_list(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        task_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
        agents = await user_port.list_active_agents()
        keyboard = _reassign_keyboard(task_id, agents)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"👤 Выберите агента для задачи #{task_id}:",
                reply_markup=keyboard,
            )

    @router.callback_query(F.data.startswith("reassign_to:"))
    async def callback_reassign_to(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        parts = callback.data.split(":")  # type: ignore[union-attr]
        task_id = int(parts[1])
        target_chatwoot_id = int(parts[2])

        ok = await reassign_task.execute(
            requester_telegram_id=callback.from_user.id,
            task_id=task_id,
            target_chatwoot_user_id=target_chatwoot_id,
        )
        if ok:
            await callback.answer("✅ Задача переназначена!")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"✅ Задача #{task_id} переназначена.")
        else:
            await callback.answer("❌ Нет прав для переназначения.", show_alert=True)

    @router.callback_query(F.data.startswith("task_comment:"))
    async def callback_task_comment(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        task_id = int(callback.data.split(":")[1])  # type: ignore[union-attr]
        await state.set_state(TelegramFSMStates.adding_comment)
        await state.update_data(comment_task_id=task_id)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(f"💬 Введите комментарий к задаче #{task_id}:")

    @router.message(TelegramFSMStates.adding_comment, F.text)
    async def handle_comment_text(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        data = await state.get_data()
        task_id = data.get("comment_task_id")
        if task_id is None:
            await message.answer("❌ Сессия устарела. Начните заново с /my_tasks.")
            await state.clear()
            return
        await add_task_comment.execute(task_id=task_id, content=message.text)
        await state.set_state(TelegramFSMStates.task_detail)
        await message.answer(f"✅ Комментарий к задаче #{task_id} добавлен.")

    @router.callback_query(F.data == "tasks_back")
    async def callback_tasks_back(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        data = await state.get_data()
        tasks = data.get("tasks", [])
        page = data.get("tasks_page", 0)
        await state.set_state(TelegramFSMStates.tasks_list)
        keyboard = _tasks_list_keyboard(tasks, page=page, total=len(tasks))
        await callback.answer()
        if isinstance(callback.message, Message):
            text = f"📋 Ваши задачи ({len(tasks)} шт.):" if tasks else "📭 У вас нет задач."
            await callback.message.edit_text(text, reply_markup=keyboard if tasks else None)

    return router


# ---------- Settings Router ----------


def _settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить имя", callback_data="settings_name")],
            [InlineKeyboardButton(text="📧 Изменить email", callback_data="settings_email")],
            [InlineKeyboardButton(text="🎤 Голосовой семпл", callback_data="settings_voice")],
            [
                InlineKeyboardButton(
                    text="🔑 Данные для входа в CRM",
                    callback_data="settings_credentials",
                )
            ],
        ]
    )


def create_settings_router(
    update_profile: UpdateProfileFieldUseCase,
    save_voice: SaveVoiceSampleUseCase,
    user_port: UserProfilePort,
) -> Router:
    """Создаёт роутер для /settings flow."""
    router = Router(name="settings")

    @router.message(Command("settings"))
    async def cmd_settings(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        if not await user_port.is_authorized(message.from_user.id):
            await message.answer("🔒 Вы не авторизованы.")
            return
        await state.set_state(SettingsFSMStates.menu)
        await message.answer("⚙️ Настройки профиля:", reply_markup=_settings_keyboard())

    @router.callback_query(SettingsFSMStates.menu, F.data == "settings_name")
    async def cb_settings_name(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsFSMStates.edit_name)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text("✏️ Введите новое имя:")

    @router.callback_query(SettingsFSMStates.menu, F.data == "settings_email")
    async def cb_settings_email(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsFSMStates.edit_email)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text("📧 Введите новый email:")

    @router.callback_query(SettingsFSMStates.menu, F.data == "settings_voice")
    async def cb_settings_voice(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsFSMStates.voice_sample)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "🎤 Отправьте голосовое сообщение или аудиофайл (ogg, mp3, wav):"
            )

    @router.callback_query(SettingsFSMStates.menu, F.data == "settings_credentials")
    async def cb_settings_credentials(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        profile = await user_port.get_profile(callback.from_user.id)
        await callback.answer()
        if isinstance(callback.message, Message):
            if profile is None:
                await callback.message.edit_text("❌ Профиль не найден.")
                return
            email = profile.settings.get("email", f"{profile.telegram_id}@24ondoc.ru")
            await callback.message.edit_text(
                f"🔑 Данные для входа в CRM:\n\n"
                f"📧 Email: <code>{email}</code>\n"
                f"🔗 CRM: {_CRM_URL}\n\n"
                "Для смены пароля войдите в CRM → Настройки профиля → Пароль.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="◀ Назад", callback_data="settings_back")]
                    ]
                ),
            )

    @router.callback_query(F.data == "settings_back")
    async def cb_settings_back(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SettingsFSMStates.menu)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "⚙️ Настройки профиля:", reply_markup=_settings_keyboard()
            )

    @router.message(SettingsFSMStates.edit_name, F.text)
    async def handle_new_name(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        updated = await update_profile.execute(message.from_user.id, "display_name", message.text)
        if updated is None:
            await message.answer("❌ Профиль не найден.")
            return
        await state.set_state(SettingsFSMStates.menu)
        await message.answer(f"✅ Имя обновлено: {message.text}", reply_markup=_settings_keyboard())

    @router.message(SettingsFSMStates.edit_email, F.text)
    async def handle_new_email(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        updated = await update_profile.execute(message.from_user.id, "email", message.text)
        if updated is None:
            await message.answer("❌ Профиль не найден.")
            return
        await state.set_state(SettingsFSMStates.menu)
        await message.answer(
            f"✅ Email обновлён: {message.text}", reply_markup=_settings_keyboard()
        )

    @router.message(SettingsFSMStates.voice_sample, F.voice)
    async def handle_voice_sample(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.voice is None:
            return
        tg_file = await bot.get_file(message.voice.file_id)
        if tg_file.file_path is None:
            await message.answer("❌ Не удалось получить файл.")
            return
        raw = await bot.download_file(tg_file.file_path)
        if not isinstance(raw, io.BytesIO):
            await message.answer("❌ Не удалось скачать файл.")
            return
        ok = await save_voice.execute(message.from_user.id, raw.read(), "ogg")
        if ok:
            await state.set_state(SettingsFSMStates.menu)
            await message.answer("✅ Голосовой семпл сохранён.", reply_markup=_settings_keyboard())
        else:
            await message.answer("❌ Профиль не найден.")

    _ALLOWED_AUDIO_EXTENSIONS = {"ogg", "mp3", "wav"}

    @router.message(SettingsFSMStates.voice_sample, F.document)
    async def handle_audio_document(message: Message, state: FSMContext, bot: Bot) -> None:
        if message.from_user is None or message.document is None:
            return
        file_name = message.document.file_name or ""
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
        if ext not in _ALLOWED_AUDIO_EXTENSIONS:
            await message.answer("❌ Поддерживаются только ogg, mp3, wav файлы.")
            return
        tg_file = await bot.get_file(message.document.file_id)
        if tg_file.file_path is None:
            await message.answer("❌ Не удалось получить файл.")
            return
        raw = await bot.download_file(tg_file.file_path)
        if not isinstance(raw, io.BytesIO):
            await message.answer("❌ Не удалось скачать файл.")
            return
        ok = await save_voice.execute(message.from_user.id, raw.read(), ext)
        if ok:
            await state.set_state(SettingsFSMStates.menu)
            await message.answer("✅ Голосовой семпл сохранён.", reply_markup=_settings_keyboard())
        else:
            await message.answer("❌ Профиль не найден.")

    return router


# ---------- Call Notification Router (DEV-53) ----------


def _call_notification_keyboard(call_id: str) -> InlineKeyboardMarkup:
    """Inline кнопки для уведомления о звонке."""
    cid = call_id
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Создать тикет",
                    callback_data=f"call_action:{cid}:create",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Изменить",
                    callback_data=f"call_action:{cid}:edit",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Игнорировать",
                    callback_data=f"call_action:{cid}:ignore",
                )
            ],
        ]
    )


def create_call_notification_router(
    call_repo: CallRecordRepositoryLike,
    chatwoot_port: ChatwootPortLike | None = None,
) -> Router:
    """Роутер для обработки callback-кнопок уведомления о звонке."""
    router = Router(name="call_notification")

    @router.callback_query(F.data.startswith("call_action:"))
    async def handle_call_action(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        parts = callback.data.split(":")  # type: ignore[union-attr]
        if len(parts) < 3:
            await callback.answer("❌ Некорректный callback.")
            return

        call_id = parts[1]
        action = parts[2]
        await callback.answer()

        if action == "create":
            if chatwoot_port is not None and isinstance(callback.message, Message):
                await callback.message.edit_text(f"⏳ Создаём тикет для звонка {call_id}...")
                try:
                    await chatwoot_port.create_ticket_from_call(call_id)
                    await callback.message.edit_text(
                        f"✅ Тикет для звонка {call_id} создан в Chatwoot."
                    )
                except Exception:
                    await callback.message.edit_text("❌ Ошибка создания тикета.")
            elif isinstance(callback.message, Message):
                await callback.message.edit_text(
                    f"✅ Создание тикета для звонка {call_id} запланировано."
                )

        elif action == "edit":
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    f"✏️ Редактирование звонка {call_id}. Используйте /new_task."
                )

        elif action == "ignore":
            record = await call_repo.get_by_id(call_id)
            if record is not None:
                record.mark_error()
                await call_repo.save(record)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"🚫 Звонок {call_id} проигнорирован.")

        else:
            if isinstance(callback.message, Message):
                await callback.message.edit_text("❓ Неизвестное действие.")

    return router


# Type aliases to avoid circular imports
@runtime_checkable
class CallRecordRepositoryLike(Protocol):
    async def get_by_id(self, call_id: str) -> Any: ...
    async def save(self, record: Any) -> None: ...


@runtime_checkable
class ChatwootPortLike(Protocol):
    async def create_ticket_from_call(self, call_id: str) -> None: ...
