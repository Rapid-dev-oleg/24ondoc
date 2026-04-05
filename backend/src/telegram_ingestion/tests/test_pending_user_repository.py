"""Unit-тесты для SQLAlchemyPendingUserRepository (mock AsyncSession)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from ..domain.models import PendingUser, UserRole
from ..infrastructure.orm_models import PendingUserORM
from ..infrastructure.pending_user_repository import SQLAlchemyPendingUserRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_pending_orm(
    phone: str = "79001234567",
    role: str = "agent",
) -> PendingUserORM:
    row = PendingUserORM()
    row.phone = phone
    row.role = role
    row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return row


def make_pending(
    phone: str = "79001234567",
    role: UserRole = UserRole.AGENT,
) -> PendingUser:
    return PendingUser(
        phone=phone,
        role=role,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _mock_session() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSQLAlchemyPendingUserRepository:
    async def test_get_by_phone_found(self) -> None:
        session = _mock_session()
        row = make_pending_orm()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = row
        session.execute = AsyncMock(return_value=mock_result)

        repo = SQLAlchemyPendingUserRepository(session)
        pending = await repo.get_by_phone("79001234567")

        assert pending is not None
        assert pending.phone == "79001234567"
        assert pending.role == UserRole.AGENT

    async def test_get_by_phone_not_found(self) -> None:
        session = _mock_session()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=mock_result)

        repo = SQLAlchemyPendingUserRepository(session)
        pending = await repo.get_by_phone("79999999999")

        assert pending is None

    async def test_save_inserts_new(self) -> None:
        session = _mock_session()
        session.get = AsyncMock(return_value=None)

        repo = SQLAlchemyPendingUserRepository(session)
        pending = make_pending()
        await repo.save(pending)

        session.add.assert_called_once()
        added: PendingUserORM = session.add.call_args[0][0]
        assert added.phone == "79001234567"
        assert added.role == "agent"

    async def test_save_updates_existing(self) -> None:
        session = _mock_session()
        row = make_pending_orm()
        session.get = AsyncMock(return_value=row)

        repo = SQLAlchemyPendingUserRepository(session)
        pending = PendingUser(
            phone="79001234567",
            role=UserRole.SUPERVISOR,
        )
        await repo.save(pending)

        session.add.assert_not_called()
        assert row.role == "supervisor"

    async def test_delete_found(self) -> None:
        session = _mock_session()
        row = make_pending_orm()
        session.get = AsyncMock(return_value=row)

        repo = SQLAlchemyPendingUserRepository(session)
        await repo.delete("79001234567")

        session.delete.assert_called_once_with(row)

    async def test_delete_not_found(self) -> None:
        session = _mock_session()
        session.get = AsyncMock(return_value=None)

        repo = SQLAlchemyPendingUserRepository(session)
        await repo.delete("79999999999")

        session.delete.assert_not_called()

    async def test_to_domain_maps_all_fields(self) -> None:
        row = make_pending_orm(
            phone="79001234567",
            role="admin",
        )

        pending = SQLAlchemyPendingUserRepository._to_domain(row)

        assert pending.phone == "79001234567"
        assert pending.role == UserRole.ADMIN
        assert pending.created_at == datetime(2026, 1, 1, tzinfo=UTC)
