"""Unit-тесты для UserProfileRepository и DraftSessionRepository (mock DB/Redis)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from ..domain.models import (
    ContentBlock,
    DraftSession,
    SessionStatus,
    UserProfile,
    UserRole,
)
from ..infrastructure.draft_session_repository import SQLAlchemyRedisDraftSessionRepository
from ..infrastructure.orm_models import DraftSessionORM, UserORM
from ..infrastructure.user_profile_repository import SQLAlchemyUserProfileRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_user_orm(telegram_id: int = 111) -> UserORM:
    row = UserORM()
    row.telegram_id = telegram_id
    row.role = "agent"
    row.phone_internal = None
    row.voice_sample_url = None
    row.settings = {}
    row.is_active = True
    row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return row


def make_draft_orm(session_id: uuid.UUID | None = None, user_id: int = 111) -> DraftSessionORM:
    row = DraftSessionORM()
    row.session_id = session_id or uuid.uuid4()
    row.user_id = user_id
    row.status = "collecting"
    row.source_type = "manual"
    row.call_record_id = None
    row.content_blocks = []
    row.assembled_text = None
    row.ai_result = None
    row.preview_message_id = None
    row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    row.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    row.expires_at = datetime(2026, 1, 2, tzinfo=UTC)
    return row


def make_profile(telegram_id: int = 111) -> UserProfile:
    return UserProfile(
        telegram_id=telegram_id,
    )


def make_session(user_id: int = 111) -> DraftSession:
    return DraftSession(user_id=user_id)


def _mock_session() -> AsyncMock:
    return AsyncMock()


def _mock_redis() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# UserProfileRepository
# ---------------------------------------------------------------------------


class TestSQLAlchemyUserProfileRepository:
    async def test_get_by_telegram_id_returns_profile(self) -> None:
        session = _mock_session()
        row = make_user_orm()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = row
        session.execute = AsyncMock(return_value=mock_result)

        repo = SQLAlchemyUserProfileRepository(session)
        profile = await repo.get_by_telegram_id(111)

        assert profile is not None
        assert profile.telegram_id == 111
        assert profile.role == UserRole.AGENT

    async def test_get_by_telegram_id_returns_none_when_not_found(self) -> None:
        session = _mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        repo = SQLAlchemyUserProfileRepository(session)
        profile = await repo.get_by_telegram_id(999)

        assert profile is None

    async def test_save_inserts_new_profile(self) -> None:
        session = _mock_session()
        session.get = AsyncMock(return_value=None)

        repo = SQLAlchemyUserProfileRepository(session)
        profile = make_profile()
        await repo.save(profile)

        session.add.assert_called_once()
        added: UserORM = session.add.call_args[0][0]
        assert added.telegram_id == 111

    async def test_save_updates_existing_profile(self) -> None:
        session = _mock_session()
        row = make_user_orm()
        session.get = AsyncMock(return_value=row)

        repo = SQLAlchemyUserProfileRepository(session)
        profile = UserProfile(
            telegram_id=111,
            role=UserRole.SUPERVISOR,
            is_active=False,
        )
        await repo.save(profile)

        session.add.assert_not_called()
        assert row.role == "supervisor"
        assert row.is_active is False

    async def test_list_active_returns_profiles(self) -> None:
        session = _mock_session()
        rows = [make_user_orm(111), make_user_orm(222)]
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=mock_result)

        repo = SQLAlchemyUserProfileRepository(session)
        profiles = await repo.list_active()

        assert len(profiles) == 2
        assert profiles[0].telegram_id == 111
        assert profiles[1].telegram_id == 222

    async def test_list_active_returns_empty_list(self) -> None:
        session = _mock_session()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        session.execute = AsyncMock(return_value=mock_result)

        repo = SQLAlchemyUserProfileRepository(session)
        profiles = await repo.list_active()

        assert profiles == []

    async def test_to_domain_maps_all_fields(self) -> None:
        row = make_user_orm()
        row.role = "admin"
        row.phone_internal = "+7999"
        row.voice_sample_url = "http://example.com/voice.ogg"
        row.settings = {"key": "value"}

        profile = SQLAlchemyUserProfileRepository._to_domain(row)

        assert profile.role == UserRole.ADMIN
        assert profile.phone_internal == "+7999"
        assert profile.voice_sample_url == "http://example.com/voice.ogg"
        assert profile.settings == {"key": "value"}


# ---------------------------------------------------------------------------
# DraftSessionRepository
# ---------------------------------------------------------------------------


class TestSQLAlchemyRedisDraftSessionRepository:
    async def test_get_by_id_returns_session(self) -> None:
        session = _mock_session()
        sid = uuid.uuid4()
        row = make_draft_orm(session_id=sid)
        session.get = AsyncMock(return_value=row)
        redis = _mock_redis()

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        result = await repo.get_by_id(sid)

        assert result is not None
        assert result.session_id == sid
        assert result.status == SessionStatus.COLLECTING

    async def test_get_by_id_returns_none_when_not_found(self) -> None:
        session = _mock_session()
        session.get = AsyncMock(return_value=None)
        redis = _mock_redis()

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        result = await repo.get_by_id(uuid.uuid4())

        assert result is None

    async def test_get_active_by_user_via_redis_hit(self) -> None:
        sid = uuid.uuid4()
        session = _mock_session()
        row = make_draft_orm(session_id=sid)
        session.get = AsyncMock(return_value=row)
        redis = _mock_redis()
        redis.get = AsyncMock(return_value=str(sid).encode())

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        result = await repo.get_active_by_user(111)

        assert result is not None
        assert result.session_id == sid
        redis.get.assert_called_once_with("draft:active:111")

    async def test_get_active_by_user_via_db_when_redis_miss(self) -> None:
        sid = uuid.uuid4()
        session = _mock_session()
        row = make_draft_orm(session_id=sid)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = row
        session.execute = AsyncMock(return_value=mock_result)
        redis = _mock_redis()
        redis.get = AsyncMock(return_value=None)

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        result = await repo.get_active_by_user(111)

        assert result is not None
        assert result.session_id == sid

    async def test_get_active_by_user_returns_none_when_not_found(self) -> None:
        session = _mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)
        redis = _mock_redis()
        redis.get = AsyncMock(return_value=None)

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        result = await repo.get_active_by_user(111)

        assert result is None

    async def test_save_inserts_new_session_and_sets_redis(self) -> None:
        session = _mock_session()
        session.get = AsyncMock(return_value=None)
        redis = _mock_redis()

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        draft = make_session(user_id=111)
        await repo.save(draft)

        session.add.assert_called_once()
        redis.set.assert_called_once_with("draft:active:111", str(draft.session_id), ex=86400)

    async def test_save_updates_existing_session_and_sets_redis(self) -> None:
        sid = uuid.uuid4()
        session = _mock_session()
        row = make_draft_orm(session_id=sid)
        session.get = AsyncMock(return_value=row)
        redis = _mock_redis()

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        draft = DraftSession(session_id=sid, user_id=111)
        draft.add_content_block(ContentBlock(type="text", content="Привет"))
        await repo.save(draft)

        session.add.assert_not_called()
        assert row.content_blocks == [draft.content_blocks[0].model_dump(mode="json")]
        redis.set.assert_called_once()

    async def test_delete_removes_from_db_and_redis(self) -> None:
        sid = uuid.uuid4()
        session = _mock_session()
        row = make_draft_orm(session_id=sid, user_id=111)
        session.get = AsyncMock(return_value=row)
        redis = _mock_redis()

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        await repo.delete(sid)

        redis.delete.assert_called_once_with("draft:active:111")
        session.delete.assert_called_once_with(row)

    async def test_delete_does_nothing_when_not_found(self) -> None:
        session = _mock_session()
        session.get = AsyncMock(return_value=None)
        redis = _mock_redis()

        repo = SQLAlchemyRedisDraftSessionRepository(session, redis)
        await repo.delete(uuid.uuid4())

        redis.delete.assert_not_called()
        session.delete.assert_not_called()

    async def test_to_domain_maps_content_blocks(self) -> None:
        sid = uuid.uuid4()
        row = make_draft_orm(session_id=sid)
        row.content_blocks = [
            {
                "type": "text",
                "content": "Тест",
                "file_id": None,
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        ]

        draft = SQLAlchemyRedisDraftSessionRepository._to_domain(row)

        assert len(draft.content_blocks) == 1
        assert draft.content_blocks[0].content == "Тест"

    async def test_to_domain_maps_ai_result(self) -> None:
        sid = uuid.uuid4()
        row = make_draft_orm(session_id=sid)
        row.status = "preview"
        row.ai_result = {
            "title": "Задача",
            "description": "Описание",
            "category": "bug",
            "priority": "high",
        }

        draft = SQLAlchemyRedisDraftSessionRepository._to_domain(row)

        assert draft.status == SessionStatus.PREVIEW
        assert draft.ai_result is not None
        assert draft.ai_result.title == "Задача"
