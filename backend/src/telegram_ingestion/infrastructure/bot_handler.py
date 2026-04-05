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
from twenty_integration.application.use_cases import CreateTwentyTaskFromSession

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

_CRM_URL = "https://24ondoc.ru"

_TASKS_PAGE_SIZE = 5

_MSG_VOICE_ENROLLED = "✅ Голосовой семпл добавлен в систему распознавания."
_MSG_VOICE_SAVED_NOT_ENROLLED = (
    "⚠️ Семпл сохранён, но не удалось добавить в систему распознавания. Попробуйте позже."
)


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


class OperatorLinkStates(StatesGroup):
    choosing_member = State()
    entering_telegram_id = State()


class AddUserStates(StatesGroup):
    choosing_member = State()


class ATS2TokenStates(StatesGroup):
    entering_access_token = State()
    entering_refresh_token = State()
    entering_proxy = State()

_INVITE_TTL = 86400 * 7  # 7 days


def _collect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶ Отправить", callback_data="collect"),
                InlineKeyboardButton(text="✕ Отмена", callback_data="cancel"),
            ],
        ]
    )


def _preview_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶ Создать задачу", callback_data="create_crm")],
            [
                InlineKeyboardButton(text="＋ Дополнить", callback_data="edit_session"),
                InlineKeyboardButton(text="⟳ Заново", callback_data="reanalyze"),
            ],
            [InlineKeyboardButton(text="✕ Удалить", callback_data="cancel")],
        ]
    )


def _format_preview(
    session: DraftSession,
    kategoriya_label: str | None = None,
    vazhnost_label: str | None = None,
) -> str:
    r = session.ai_result
    if r is None:
        return "❓ Результат анализа недоступен."
    lines = [
        "<b>📋 Preview задачи</b>",
        "",
        f"<b>Заголовок:</b> {r.title}",
        f"<b>Описание:</b> {r.description}",
        f"<b>Категория:</b> {kategoriya_label or '—'}",
        f"<b>Важность:</b> {vazhnost_label or '—'}",
    ]
    if r.deadline:
        lines.append(f"<b>Дедлайн:</b> {r.deadline}")
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
    create_twenty_task: CreateTwentyTaskFromSession | None = None,
    draft_repo: DraftSessionRepository | None = None,
    twenty_crm_port: Any = None,
    redis: Any = None,
    bot_username: str = "",
    call_repo: Any = None,
    settings: Any = None,
    ats2_auth_manager: Any = None,
    ats2_poller: Any = None,
    ats2_client: Any = None,
) -> Router:
    """Create and configure the telegram ingestion router with injected use cases."""
    from ..domain.models import UserRole

    router = Router(name="telegram_ingestion")

    @router.message(Command("start"))
    async def cmd_start(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        await state.clear()

        # Check for invite deep link: /start inv_<token>
        args = message.text.split(maxsplit=1)[1] if message.text and " " in message.text else ""
        if args.startswith("inv_") and redis is not None:
            token = args
            raw = await redis.get(f"invite:{token}")
            if raw is not None:
                import json as _json
                invite = _json.loads(raw.decode())
                first_name = message.from_user.first_name or ""
                last_name = message.from_user.last_name or ""
                display_name = f"{first_name} {last_name}".strip() or str(message.from_user.id)
                try:
                    await user_port.upsert_user(
                        telegram_id=message.from_user.id,
                        twenty_member_id=invite["twenty_member_id"],
                        role=invite["role"],
                        display_name=display_name,
                    )
                    role_label = "администратор" if invite["role"] == "admin" else "участник"
                    await message.answer(
                        f"✅ Вы добавлены в систему 24ondoc как <b>{role_label}</b>!\n\n"
                        "Используйте /new_task чтобы создать задачу."
                    )
                    await redis.delete(f"invite:{token}")
                    return
                except Exception:
                    logger.exception("Invite registration failed for user %s", message.from_user.id)
                    await message.answer("❌ Ошибка регистрации. Попробуйте позже.")
                    return
            else:
                await message.answer("❌ Ссылка-приглашение недействительна или устарела.")
                return

        if auto_register is not None:
            first_name = message.from_user.first_name or ""
            try:
                profile, is_new = await auto_register.execute(message.from_user.id, first_name)
            except Exception:
                logger.exception("Auto-registration failed for user %s", message.from_user.id)
                await message.answer(
                    "❌ Ошибка регистрации. Попробуйте позже или обратитесь к администратору."
                )
                return
            try:
                if is_new:
                    await message.answer(
                        "✅ Вы успешно зарегистрированы в системе 24ondoc!\n\n"
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
                "📝 Опишите задачу — отправьте текст, голосовое, фото или файл."
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
        photo = message.photo[-1]
        caption = message.caption or ""
        text = "[Фото]"
        if caption:
            text += f" {caption}"
        await add_text.execute(
            message.from_user.id, text, block_type="photo", file_id=photo.file_id
        )
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
        await add_text.execute(
            message.from_user.id, text, block_type="file", file_id=message.document.file_id
        )
        await message.answer("📄 Файл добавлен.", reply_markup=_collect_keyboard())

    async def _run_ai_analysis(
        session: DraftSession,
        callback: CallbackQuery,
        state: FSMContext,
        fallback_state: State,
        fallback_keyboard: InlineKeyboardMarkup,
    ) -> None:
        """Run AI classification on session and show preview. Shared by collect and reanalyze."""
        if ai_port is None or set_analysis_result is None:
            await state.set_state(fallback_state)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    "❌ AI-анализ недоступен. Попробуйте позже.",
                    reply_markup=fallback_keyboard,
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

            # Select kategoriya/vazhnost from Twenty CRM options
            kategoriya_value: str | None = None
            vazhnost_value: str | None = None
            kategoriya_label: str | None = None
            vazhnost_label: str | None = None
            if twenty_crm_port is not None:
                try:
                    options = await twenty_crm_port.fetch_task_field_options()
                    task_text = f"{classification.title}\n{classification.description}"
                    selection = await ai_port.select_task_fields(
                        task_text,
                        options.get("kategoriya", []),
                        options.get("vazhnost", []),
                    )
                    kategoriya_value = selection.kategoriya
                    vazhnost_value = selection.vazhnost
                    # Resolve labels for preview
                    kat_map = {o["value"]: o["label"] for o in options.get("kategoriya", [])}
                    vazh_map = {o["value"]: o["label"] for o in options.get("vazhnost", [])}
                    kategoriya_label = kat_map.get(kategoriya_value or "")
                    vazhnost_label = vazh_map.get(vazhnost_value or "")
                except Exception:
                    logger.exception("Failed to select task fields during preview")

            await state.update_data(
                twenty_kategoriya=kategoriya_value,
                twenty_vazhnost=vazhnost_value,
                twenty_kategoriya_label=kategoriya_label,
                twenty_vazhnost_label=vazhnost_label,
            )

            await state.set_state(TelegramFSMStates.preview)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    _format_preview(updated, kategoriya_label, vazhnost_label),
                    reply_markup=_preview_keyboard(),
                )
        except Exception:
            logger.exception("AI analysis failed")
            await state.set_state(fallback_state)
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    "❌ Ошибка анализа. Попробуйте снова.",
                    reply_markup=fallback_keyboard,
                )

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

        await _run_ai_analysis(
            session, callback, state,
            fallback_state=TelegramFSMStates.collecting,
            fallback_keyboard=_collect_keyboard(),
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

        await _run_ai_analysis(
            session, callback, state,
            fallback_state=TelegramFSMStates.preview,
            fallback_keyboard=_preview_keyboard(),
        )

    @router.callback_query(TelegramFSMStates.preview, F.data == "create_crm")
    async def callback_create_crm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        if callback.from_user is None:
            await callback.answer()
            return

        if create_twenty_task is None or draft_repo is None:
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

            # Determine assignee from user profile
            profile = await user_port.get_profile(callback.from_user.id)
            assignee_id = profile.twenty_member_id if profile else None

            # File downloader: downloads from Telegram, returns (bytes, filename, content_type)
            async def _download_tg_file(file_id: str) -> tuple[bytes, str, str] | None:
                import mimetypes

                tg_file = await bot.get_file(file_id)
                if tg_file.file_path is None:
                    return None
                raw = await bot.download_file(tg_file.file_path)
                if not isinstance(raw, io.BytesIO):
                    return None
                filename = tg_file.file_path.split("/")[-1] if "/" in tg_file.file_path else tg_file.file_path
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                return raw.read(), filename, content_type

            task = await create_twenty_task.execute(
                fetched,
                telegram_id=callback.from_user.id,
                user_name=callback.from_user.first_name or "",
                assignee_id=assignee_id,
                file_downloader=_download_tg_file,
                kategoriya=data.get("twenty_kategoriya"),
                vazhnost=data.get("twenty_vazhnost"),
            )

            await cancel_session.execute(callback.from_user.id)
            await state.clear()
            await callback.answer("✅ Задача создана в CRM!")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    f"✅ Задача <b>{task.title}</b> создана в Twenty CRM."
                )
        except Exception:
            logger.exception("Failed to create CRM task for user %s", callback.from_user.id)
            await callback.answer("❌ Ошибка создания задачи.", show_alert=True)

    # ---------- /operators command (DEV-122) ----------

    @router.message(Command("operators"))
    async def cmd_operators(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None or profile.role not in (UserRole.ADMIN, UserRole.SUPERVISOR):
            await message.answer("🔒 Нет доступа.")
            return
        if twenty_crm_port is None:
            await message.answer("❌ Интеграция с Twenty недоступна.")
            return
        try:
            members = await twenty_crm_port.list_workspace_members()
            if not members:
                await message.answer("❌ Нет членов workspace.")
                return
            await state.set_state(OperatorLinkStates.choosing_member)
            await message.answer(
                "👥 Выберите члена workspace для привязки к Telegram ID:",
                reply_markup=_operators_keyboard(members),
            )
        except Exception as e:
            logger.error("Error listing workspace members: %s", e)
            await message.answer("❌ Ошибка получения членов workspace.")

    @router.callback_query(OperatorLinkStates.choosing_member, F.data.startswith("select_member:"))
    async def cb_select_member(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        if callback.data is None:
            return
        twenty_member_id = callback.data.split(":", 1)[1]
        await state.update_data(twenty_member_id=twenty_member_id)
        await state.set_state(OperatorLinkStates.entering_telegram_id)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                "📱 Введите Telegram ID оператора (или telegram_id в формате числа):"
            )

    @router.message(OperatorLinkStates.entering_telegram_id, F.text)
    async def handle_telegram_id(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        try:
            telegram_id = int(message.text.strip())
        except ValueError:
            await message.answer("❌ Некорректный Telegram ID. Введите число.")
            return
        state_data = await state.get_data()
        twenty_member_id = state_data.get("twenty_member_id")
        if not twenty_member_id:
            await message.answer("❌ Ошибка: member_id не найден.")
            await state.clear()
            return
        updated = await user_port.update_twenty_member_id(telegram_id, twenty_member_id)
        if updated is None:
            await message.answer(f"❌ Оператор с Telegram ID {telegram_id} не найден в системе.")
        else:
            name = updated.settings.get("display_name", telegram_id)
            await message.answer(
                f"✅ Оператор {name} привязан к члену workspace (ID: {twenty_member_id})"
            )
        await state.clear()

    # ---------- /add_member & /add_admin commands ----------

    async def _cmd_add_user(message: Message, state: FSMContext, target_role: str) -> None:
        """Общий обработчик для /add_member и /add_admin."""
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None or profile.role != UserRole.ADMIN:
            await message.answer("🔒 Нет доступа. Только для администраторов.")
            return
        if twenty_crm_port is None:
            await message.answer("❌ Интеграция с Twenty недоступна.")
            return
        try:
            members = await twenty_crm_port.list_workspace_members()
            if not members:
                await message.answer("❌ Нет членов workspace.")
                return
            role_label = "администратора" if target_role == "admin" else "участника"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text=f"{m.first_name} {m.last_name}",
                        callback_data=f"adduser:{target_role}:{m.twenty_id}",
                    )]
                    for m in members
                ]
            )
            await state.set_state(AddUserStates.choosing_member)
            await message.answer(
                f"👥 Выберите члена workspace для добавления {role_label}:",
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Error in add_user command")
            await message.answer("❌ Ошибка получения членов workspace.")

    @router.message(Command("add_member"))
    async def cmd_add_member(message: Message, state: FSMContext) -> None:
        await _cmd_add_user(message, state, "agent")

    @router.message(Command("add_admin"))
    async def cmd_add_admin(message: Message, state: FSMContext) -> None:
        await _cmd_add_user(message, state, "admin")

    @router.callback_query(AddUserStates.choosing_member, F.data.startswith("adduser:"))
    async def cb_add_user_select(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None or callback.data is None:
            await callback.answer()
            return
        parts = callback.data.split(":", 2)
        if len(parts) != 3:
            await callback.answer("❌ Ошибка данных.")
            return
        _, target_role, twenty_member_id = parts

        if redis is None:
            await callback.answer("❌ Redis недоступен.", show_alert=True)
            await state.clear()
            return

        # Generate invite token and save to Redis
        import json as _json

        token = f"inv_{uuid.uuid4().hex[:12]}"
        invite_data = _json.dumps({
            "twenty_member_id": twenty_member_id,
            "role": target_role,
        })
        await redis.set(f"invite:{token}", invite_data, ex=_INVITE_TTL)

        username = bot_username or "aidevl_bot"
        link = f"https://t.me/{username}?start={token}"
        role_label = "администратора" if target_role == "admin" else "участника"

        await callback.answer()
        await state.clear()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"🔗 Ссылка для добавления <b>{role_label}</b>:\n\n"
                f"<a href=\"{link}\">Открыть бот и зарегистрироваться</a>\n\n"
                f"Отправьте эту ссылку пользователю. "
                f"При переходе он автоматически зарегистрируется в системе.\n"
                f"Ссылка действительна 7 дней."
            )

    # ---------- /health command (admin) ----------

    @router.message(Command("health"))
    async def cmd_health(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None or profile.role != UserRole.ADMIN:
            await message.answer("🔒 Нет доступа. Только для адм��нистраторов.")
            return

        await message.answer("⏳ П��оверяю сервисы...")

        lines: list[str] = ["<b>Здоровье системы:</b>\n"]

        # 1. Redis
        try:
            if redis is not None:
                await redis.ping()
                lines.append("✅ Redis — OK")
            else:
                lines.append("⚠️ Redis — не настроен")
        except Exception as e:
            lines.append(f"❌ Redis — {e}")

        # 2. Twenty CRM
        try:
            if twenty_crm_port is not None:
                members = await twenty_crm_port.list_workspace_members()
                lines.append(f"✅ Twenty CRM — OK ({len(members)} участников)")
            else:
                lines.append("⚠️ Twenty CRM — не настроен")
        except Exception as e:
            lines.append(f"❌ Twenty CRM — {e}")

        # 3. Proxy + ATS2
        ats2_connected = False
        if settings is not None and settings.ats2_enabled and ats2_auth_manager is not None:
            import httpx as _httpx

            # Use proxy from auth_manager (updated at runtime via /ats2_proxy)
            proxy_url = getattr(ats2_auth_manager, "_proxy_url", None) or settings.ats2_proxy_url
            try:
                current_token = await ats2_auth_manager.get_access_token()
                async with _httpx.AsyncClient(
                    proxy=proxy_url or None, timeout=10.0
                ) as _client:
                    resp = await _client.get("https://ats2.t2.ru/crm/openapi/call-records/active", headers={
                        "Authorization": current_token,
                        "Accept": "application/json",
                    })
                    if resp.status_code == 403:
                        lines.append("✅ Прокси ATS2 — OK")
                        lines.append("⚠️ ATS2 (Теле2) — токен истёк (403)")
                    elif resp.status_code < 400:
                        lines.append("✅ Прокси ATS2 — OK")
                        lines.append("✅ ATS2 (Теле2) — OK")
                        ats2_connected = True
                    else:
                        lines.append("✅ Прокси ATS2 — OK")
                        lines.append(f"⚠️ ATS2 (Теле2) — HTTP {resp.status_code}")
            except Exception as e:
                lines.append(f"❌ Прокси ATS2 — {e}")
                lines.append("❌ ATS2 (Теле2) — недоступен")
        elif settings is not None and settings.ats2_enabled:
            lines.append("⚠️ Прокси ATS2 — не настроен")
        else:
            lines.append("⚪ ATS2 — отключён")

        # 4. Groq STT
        if settings is not None and settings.groq_api_key:
            import httpx as _httpx

            try:
                async with _httpx.AsyncClient(timeout=10.0) as _client:
                    resp = await _client.get(
                        "https://api.groq.com/openai/v1/models",
                        headers={"Authorization": f"Bearer {settings.groq_api_key}"},
                    )
                    if resp.status_code < 400:
                        lines.append("✅ Groq STT — OK")
                    else:
                        lines.append(f"❌ Groq STT — HTTP {resp.status_code}")
            except Exception as e:
                lines.append(f"❌ Groq STT — {e}")
        else:
            lines.append("⚪ Groq STT — ключ не задан")

        # 5. OpenRouter
        if settings is not None and (settings.openrouter_api_key or settings.openai_api_key):
            import httpx as _httpx

            api_key = settings.openrouter_api_key or settings.openai_api_key
            try:
                async with _httpx.AsyncClient(timeout=10.0) as _client:
                    resp = await _client.get(
                        "https://openrouter.ai/api/v1/models",
                        headers={"Authorization": f"Bearer {api_key}"},
                    )
                    if resp.status_code < 400:
                        lines.append("✅ OpenRouter — OK")
                    else:
                        lines.append(f"❌ OpenRouter — HTTP {resp.status_code}")
            except Exception as e:
                lines.append(f"❌ OpenRouter — {e}")
        else:
            lines.append("⚪ OpenRouter — ключ не задан")

        await message.answer("\n".join(lines))

        # 6. Sync ATS2 calls if connected
        if ats2_connected and ats2_poller is not None:
            try:
                await message.answer("🔄 Синхронизация звонков ATS2...")
                await ats2_poller.poll_once()
                await message.answer("✅ Синхронизация звонков завершена.")
            except Exception as e:
                logger.exception("ATS2 sync from /health failed")
                await message.answer(f"❌ Ошибка синхронизации: {e}")

    # ---------- /logs command (admin) ----------

    @router.message(Command("logs"))
    async def cmd_logs(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None or profile.role != UserRole.ADMIN:
            await message.answer("🔒 Нет доступа. Только для администраторов.")
            return

        if call_repo is None:
            await message.answer("❌ Репозиторий звонков недоступен.")
            return

        try:
            records = await call_repo.get_recent(limit=10)
        except Exception:
            await message.answer("❌ Ошибка загрузки записей.")
            return

        if not records:
            await message.answer("�� Нет записей.")
            return

        status_icons = {
            "new": "🆕",
            "processing": "⏳",
            "preview": "👁",
            "created": "✅",
            "error": "❌",
        }
        source_labels = {
            "call_t2_webhook": "T2 Webhook",
            "call_ats2_polling": "ATS2 Поллер",
        }

        lines = [f"<b>Последние 10 заявок:</b>\n"]
        for i, r in enumerate(records, 1):
            icon = status_icons.get(r.status.value, "❓")
            source = source_labels.get(r.source.value, r.source.value)
            phone = r.caller_phone or "��"
            duration_str = ""
            if r.duration is not None:
                m, s = divmod(r.duration, 60)
                duration_str = f" ({m}м{s}с)"
            dt = r.created_at.strftime("%d.%m %H:%M") if r.created_at else ""
            has_text = "📝" if r.transcription_t2 or r.transcription_whisper else ""

            lines.append(
                f"{i}. {icon} {dt} | {source}\n"
                f"   📞 {phone}{duration_str} {has_text}\n"
                f"   ID: <code>{r.call_id[:16]}</code> [{r.status.value}]"
            )

        await message.answer("\n".join(lines))

    # ---------- ATS2 token management (admin) ----------

    @router.message(Command("ats2_access_token"))
    async def cmd_ats2_access_token(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None or profile.role != UserRole.ADMIN:
            await message.answer("🔒 Нет доступа.")
            return
        if ats2_auth_manager is None:
            await message.answer("⚠️ ATS2 отключён.")
            return
        await state.set_state(ATS2TokenStates.entering_access_token)
        await message.answer("🔑 Отправьте новый <b>Access Token</b> для ATS2:")

    @router.message(ATS2TokenStates.entering_access_token, F.text)
    async def handle_ats2_access_token(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        token = message.text.strip()
        if not token or len(token) < 10:
            await message.answer("❌ Токен слишком короткий. Попробуйте ещё раз.")
            return

        try:
            old_refresh = ats2_auth_manager._refresh_token
            ats2_auth_manager.update_tokens(access_token=token, refresh_token=old_refresh)

            # Update .env file
            if settings is not None:
                _update_env_var(settings.env_file_path, "ATS2_ACCESS_TOKEN", token)

            await state.clear()
            await message.answer("✅ Access Token обновлён.")
            # Delete the message with token for security
            try:
                await message.delete()
            except Exception:
                pass
        except Exception:
            logger.exception("Failed to update ATS2 access token")
            await state.clear()
            await message.answer("❌ Ошибка обновления токена.")

    @router.message(Command("ats2_refresh_token"))
    async def cmd_ats2_refresh_token(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None or profile.role != UserRole.ADMIN:
            await message.answer("🔒 Нет доступа.")
            return
        if ats2_auth_manager is None:
            await message.answer("⚠️ ATS2 отключён.")
            return
        await state.set_state(ATS2TokenStates.entering_refresh_token)
        await message.answer("🔑 Отправьте новый <b>Refresh Token</b> для ATS2:")

    @router.message(ATS2TokenStates.entering_refresh_token, F.text)
    async def handle_ats2_refresh_token(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        token = message.text.strip()
        if not token or len(token) < 10:
            await message.answer("❌ Токен слишком короткий. Попробуйте ещё раз.")
            return

        try:
            old_access = ats2_auth_manager._access_token
            ats2_auth_manager.update_tokens(access_token=old_access, refresh_token=token)

            # Update .env file
            if settings is not None:
                _update_env_var(settings.env_file_path, "ATS2_REFRESH_TOKEN", token)

            await state.clear()
            await message.answer("✅ Refresh Token обновлён.")
            try:
                await message.delete()
            except Exception:
                pass
        except Exception:
            logger.exception("Failed to update ATS2 refresh token")
            await state.clear()
            await message.answer("❌ Ошибка обновления токена.")

    @router.message(Command("ats2_proxy"))
    async def cmd_ats2_proxy(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        profile = await user_port.get_profile(message.from_user.id)
        if profile is None or profile.role != UserRole.ADMIN:
            await message.answer("🔒 Нет доступа.")
            return
        if ats2_client is None:
            await message.answer("⚠️ ATS2 отключён.")
            return
        await state.set_state(ATS2TokenStates.entering_proxy)
        await message.answer(
            "🌐 Отправьте новый прокси для ATS2:\n"
            "<code>host:port:login:password</code>"
        )

    @router.message(ATS2TokenStates.entering_proxy, F.text)
    async def handle_ats2_proxy(message: Message, state: FSMContext) -> None:
        if message.from_user is None or message.text is None:
            return
        raw = message.text.strip()

        # Parse host:port:login:pass format
        if not raw.startswith("http"):
            parts = raw.split(":")
            if len(parts) == 4:
                host, port, login, pwd = parts
                proxy = f"http://{login}:{pwd}@{host}:{port}"
            elif len(parts) == 2:
                proxy = f"http://{raw}"
            else:
                await message.answer("❌ Формат: <code>host:port:login:pass</code>")
                return
        else:
            proxy = raw

        try:
            await ats2_client.update_proxy(proxy)

            if settings is not None:
                _update_env_var(settings.env_file_path, "ATS2_PROXY_URL", proxy)

            await state.clear()
            await message.answer("✅ Прокси ATS2 обновлён.")
            try:
                await message.delete()
            except Exception:
                pass
        except Exception:
            logger.exception("Failed to update ATS2 proxy")
            await state.clear()
            await message.answer("❌ Ошибка обновления прокси.")

    return router


def _update_env_var(env_path: str, key: str, value: str) -> None:
    """Update a variable in .env file."""
    import os

    path = env_path if os.path.isabs(env_path) else f"/app/{env_path}"
    if not os.path.exists(path):
        logger.warning("_update_env_var: file %s not found", path)
        return

    with open(path) as f:
        lines = f.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}\n"
            found = True
            break
    if not found:
        lines.append(f"{key}={value}\n")

    with open(path, "w") as f:
        f.writelines(lines)


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
    for idx, ticket in enumerate(tickets[start:end], start=start):
        title_short = ticket['title'][:45]
        label = f"📋 {title_short}"
        cb_data = f"task_detail:{idx}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=cb_data)])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"tasks_page:{page - 1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton(text="▶ Далее", callback_data=f"tasks_page:{page + 1}"))
    if nav_row:
        buttons.append(nav_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _task_detail_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для детального просмотра задачи."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀ К списку", callback_data="tasks_back")],
        ]
    )


def _reassign_keyboard(task_id: int, agents: Sequence[Any]) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    for agent in agents:
        label = f"👤 {agent.telegram_id}"
        cb = f"reassign_to:{task_id}:{agent.telegram_id}"
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
                "assignee_crm_id": t.assignee_chatwoot_id,
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
        idx = int(callback.data.split(":")[1])  # type: ignore[union-attr]

        data = await state.get_data()
        tasks = data.get("tasks", [])
        if idx >= len(tasks):
            await callback.answer("❌ Задача не найдена.")
            return
        ticket = tasks[idx]
        task_id = ticket["task_id"]
        assignee_crm_id = ticket.get("assignee_crm_id") or ""
        status = ticket["status"]
        title = ticket["title"]

        profile = await user_port.get_profile(callback.from_user.id)
        from ..domain.models import UserRole

        is_supervisor = profile is not None and profile.role in (
            UserRole.SUPERVISOR,
            UserRole.ADMIN,
        )

        await state.set_state(TelegramFSMStates.task_detail)
        await state.update_data(
            current_task_id=task_id,
            current_task_idx=idx,
            current_assignee_crm_id=assignee_crm_id,
            current_task_status=status,
        )

        keyboard = _task_detail_keyboard(task_id, assignee_crm_id, status, is_supervisor)
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"📋 Задача #{task_id}: {title}\nСтатус: {status}",
                reply_markup=keyboard,
            )

    @router.callback_query(F.data == "task_resolve")
    async def callback_task_resolve(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        data = await state.get_data()
        task_id = data.get("current_task_id")
        title = ""
        tasks = data.get("tasks", [])
        idx = data.get("current_task_idx", 0)
        if idx < len(tasks):
            title = tasks[idx].get("title", "")
        if task_id is None:
            await callback.answer("❌ Задача не найдена.")
            return
        try:
            await twenty_crm_port.update_task_status(task_id, "VYPOLNENO")
            await callback.answer("✅ Задача решена!")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"✅ Задача «{title}» решена.")
        except Exception:
            await callback.answer("❌ Ошибка.", show_alert=True)

    @router.callback_query(F.data == "task_reopen")
    async def callback_task_reopen(callback: CallbackQuery, state: FSMContext) -> None:
        if callback.from_user is None:
            await callback.answer()
            return
        data = await state.get_data()
        task_id = data.get("current_task_id")
        title = ""
        tasks = data.get("tasks", [])
        idx = data.get("current_task_idx", 0)
        if idx < len(tasks):
            title = tasks[idx].get("title", "")
        if task_id is None:
            await callback.answer("❌ Задача не найдена.")
            return
        try:
            await twenty_crm_port.update_task_status(task_id, "TODO")
            await callback.answer("🔓 Задача открыта!")
            if isinstance(callback.message, Message):
                await callback.message.edit_text(f"🔓 Задача «{title}» открыта.")
        except Exception:
            await callback.answer("❌ Ошибка.", show_alert=True)

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
            target_user_id=target_chatwoot_id,
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


def _operators_keyboard(members: Sequence[Any]) -> InlineKeyboardMarkup:
    """Создаёт inline клавиатуру со списком членов workspace."""
    keyboard = [
        [
            InlineKeyboardButton(
                text=f"{member.first_name} {member.last_name} ({member.email})",
                callback_data=f"select_member:{member.twenty_id}",
            )
        ]
        for member in members
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


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
        await message.answer("⏳ Обрабатываю голосовой семпл...")
        saved, enrolled = await save_voice.execute(message.from_user.id, raw.read(), "ogg")
        if not saved:
            await message.answer("❌ Профиль не найден.")
            return
        await state.set_state(SettingsFSMStates.menu)
        if enrolled:
            await message.answer(
                _MSG_VOICE_ENROLLED,
                reply_markup=_settings_keyboard(),
            )
        else:
            await message.answer(
                _MSG_VOICE_SAVED_NOT_ENROLLED,
                reply_markup=_settings_keyboard(),
            )

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
        await message.answer("⏳ Обрабатываю голосовой семпл...")
        saved, enrolled = await save_voice.execute(message.from_user.id, raw.read(), ext)
        if not saved:
            await message.answer("❌ Профиль не найден.")
            return
        await state.set_state(SettingsFSMStates.menu)
        if enrolled:
            await message.answer(
                _MSG_VOICE_ENROLLED,
                reply_markup=_settings_keyboard(),
            )
        else:
            await message.answer(
                _MSG_VOICE_SAVED_NOT_ENROLLED,
                reply_markup=_settings_keyboard(),
            )

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
                        f"✅ Тикет для звонка {call_id} создан в CRM."
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
