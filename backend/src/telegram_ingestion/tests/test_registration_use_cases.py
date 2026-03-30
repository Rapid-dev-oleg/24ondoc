"""Tests for registration use cases (TDD: RED → GREEN)."""

from __future__ import annotations

from datetime import UTC, datetime

from telegram_ingestion.application.ports import (
    VoiceEnrollmentPort,
    VoiceSampleStoragePort,
)
from telegram_ingestion.application.registration_use_cases import (
    AutoRegisterUserUseCase,
    SaveVoiceSampleUseCase,
    UpdateProfileFieldUseCase,
)
from telegram_ingestion.domain.models import UserProfile, UserRole
from telegram_ingestion.domain.repository import UserProfileRepository

# ---------------------------------------------------------------------------
# In-memory stubs
# ---------------------------------------------------------------------------


class InMemoryUserProfileRepository(UserProfileRepository):
    def __init__(self) -> None:
        self._store: dict[int, UserProfile] = {}

    async def get_by_telegram_id(self, telegram_id: int) -> UserProfile | None:
        return self._store.get(telegram_id)

    async def save(self, profile: UserProfile) -> None:
        self._store[profile.telegram_id] = profile

    async def list_active(self) -> list[UserProfile]:
        return [p for p in self._store.values() if p.is_active]

    async def list_all(self) -> list[UserProfile]:
        return list(self._store.values())

    async def delete_by_telegram_id(self, telegram_id: int) -> None:
        self._store.pop(telegram_id, None)


class InMemoryVoiceSampleStorage(VoiceSampleStoragePort):
    def __init__(self) -> None:
        self.saved: dict[int, tuple[bytes, str]] = {}

    async def save(self, telegram_id: int, data: bytes, ext: str) -> str:
        self.saved[telegram_id] = (data, ext)
        return f"/tmp/voice/{telegram_id}.{ext}"


def _make_profile(telegram_id: int = 1) -> UserProfile:
    return UserProfile(
        telegram_id=telegram_id,
        role=UserRole.AGENT,
        is_active=True,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tests: AutoRegisterUserUseCase
# ---------------------------------------------------------------------------


class TestAutoRegisterUserUseCase:
    async def test_creates_new_user_when_not_exists(self) -> None:
        repo = InMemoryUserProfileRepository()
        uc = AutoRegisterUserUseCase(repo)

        profile, is_new = await uc.execute(telegram_id=42, first_name="Алиса")

        assert is_new is True
        assert profile.telegram_id == 42
        assert profile.settings["display_name"] == "Алиса"

    async def test_returns_existing_user_without_creating(self) -> None:
        repo = InMemoryUserProfileRepository()
        existing = _make_profile(telegram_id=10)
        await repo.save(existing)
        uc = AutoRegisterUserUseCase(repo)

        profile, is_new = await uc.execute(telegram_id=10, first_name="Боб")

        assert is_new is False
        assert profile.telegram_id == 10

    async def test_empty_first_name_uses_telegram_id_as_name(self) -> None:
        repo = InMemoryUserProfileRepository()
        uc = AutoRegisterUserUseCase(repo)

        profile, _ = await uc.execute(telegram_id=777, first_name="")

        assert profile.settings["display_name"] == "777"

    async def test_new_user_role_is_agent(self) -> None:
        repo = InMemoryUserProfileRepository()
        uc = AutoRegisterUserUseCase(repo)

        profile, _ = await uc.execute(telegram_id=111, first_name="Агент")

        assert profile.role == UserRole.AGENT

    async def test_profile_saved_to_repository(self) -> None:
        repo = InMemoryUserProfileRepository()
        uc = AutoRegisterUserUseCase(repo)

        await uc.execute(telegram_id=88, first_name="Паша")

        saved = await repo.get_by_telegram_id(88)
        assert saved is not None
        assert saved.telegram_id == 88


# ---------------------------------------------------------------------------
# Tests: UpdateProfileFieldUseCase
# ---------------------------------------------------------------------------


class TestUpdateProfileFieldUseCase:
    async def test_updates_display_name(self) -> None:
        repo = InMemoryUserProfileRepository()
        profile = _make_profile()
        await repo.save(profile)

        uc = UpdateProfileFieldUseCase(repo)
        result = await uc.execute(telegram_id=1, field="display_name", value="Новое имя")

        assert result is not None
        assert result.settings["display_name"] == "Новое имя"

    async def test_updates_email(self) -> None:
        repo = InMemoryUserProfileRepository()
        profile = _make_profile()
        profile = profile.model_copy(update={"settings": {"email": "old@example.com"}})
        await repo.save(profile)

        uc = UpdateProfileFieldUseCase(repo)
        result = await uc.execute(telegram_id=1, field="email", value="new@example.com")

        assert result is not None
        assert result.settings["email"] == "new@example.com"

    async def test_preserves_other_settings_fields(self) -> None:
        repo = InMemoryUserProfileRepository()
        profile = _make_profile()
        profile = profile.model_copy(
            update={"settings": {"display_name": "Имя", "email": "e@e.com"}}
        )
        await repo.save(profile)

        uc = UpdateProfileFieldUseCase(repo)
        await uc.execute(telegram_id=1, field="display_name", value="Новое")

        saved = await repo.get_by_telegram_id(1)
        assert saved is not None
        assert saved.settings["email"] == "e@e.com"

    async def test_returns_none_for_unknown_user(self) -> None:
        uc = UpdateProfileFieldUseCase(InMemoryUserProfileRepository())
        result = await uc.execute(telegram_id=999, field="display_name", value="X")
        assert result is None

    async def test_persists_to_repository(self) -> None:
        repo = InMemoryUserProfileRepository()
        profile = _make_profile(telegram_id=5)
        await repo.save(profile)

        uc = UpdateProfileFieldUseCase(repo)
        await uc.execute(telegram_id=5, field="display_name", value="Сохранённое")

        saved = await repo.get_by_telegram_id(5)
        assert saved is not None
        assert saved.settings["display_name"] == "Сохранённое"


# ---------------------------------------------------------------------------
# Tests: SaveVoiceSampleUseCase
# ---------------------------------------------------------------------------


class InMemoryVoiceEnrollmentPort(VoiceEnrollmentPort):
    def __init__(self, result: bool = True) -> None:
        self._result = result
        self.calls: list[tuple[int, bytes]] = []

    async def enroll(self, agent_id: int, audio_bytes: bytes) -> bool:
        self.calls.append((agent_id, audio_bytes))
        return self._result


class TestSaveVoiceSampleUseCase:
    async def test_saves_voice_and_updates_profile(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        await repo.save(_make_profile(telegram_id=10))

        uc = SaveVoiceSampleUseCase(repo, storage)
        saved, enrolled = await uc.execute(telegram_id=10, data=b"audio_data", ext="ogg")

        assert saved is True
        assert enrolled is False
        assert 10 in storage.saved
        assert storage.saved[10] == (b"audio_data", "ogg")

    async def test_voice_sample_url_stored_in_profile(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        await repo.save(_make_profile(telegram_id=20))

        uc = SaveVoiceSampleUseCase(repo, storage)
        await uc.execute(telegram_id=20, data=b"audio", ext="mp3")

        saved = await repo.get_by_telegram_id(20)
        assert saved is not None
        assert saved.voice_sample_url == "/tmp/voice/20.mp3"

    async def test_returns_false_false_for_unknown_user(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        uc = SaveVoiceSampleUseCase(repo, storage)

        saved, enrolled = await uc.execute(telegram_id=999, data=b"x", ext="ogg")

        assert saved is False
        assert enrolled is False
        assert 999 not in storage.saved

    async def test_supports_wav_extension(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        await repo.save(_make_profile(telegram_id=30))

        uc = SaveVoiceSampleUseCase(repo, storage)
        saved, enrolled = await uc.execute(telegram_id=30, data=b"wav_data", ext="wav")

        assert saved is True
        assert enrolled is False
        repo_saved = await repo.get_by_telegram_id(30)
        assert repo_saved is not None
        assert repo_saved.voice_sample_url == "/tmp/voice/30.wav"

    async def test_overwrites_previous_sample(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        profile = _make_profile(telegram_id=40)
        profile = profile.model_copy(update={"voice_sample_url": "/old/path.ogg"})
        await repo.save(profile)

        uc = SaveVoiceSampleUseCase(repo, storage)
        await uc.execute(telegram_id=40, data=b"new_audio", ext="ogg")

        repo_saved = await repo.get_by_telegram_id(40)
        assert repo_saved is not None
        assert repo_saved.voice_sample_url == "/tmp/voice/40.ogg"
        assert storage.saved[40] == (b"new_audio", "ogg")

    async def test_with_enrollment_port_enrolled_true(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        enrollment = InMemoryVoiceEnrollmentPort(result=True)
        await repo.save(_make_profile(telegram_id=50))

        uc = SaveVoiceSampleUseCase(repo, storage, enrollment=enrollment)
        saved, enrolled = await uc.execute(telegram_id=50, data=b"audio", ext="ogg")

        assert saved is True
        assert enrolled is True
        assert len(enrollment.calls) == 1
        assert enrollment.calls[0] == (50, b"audio")

    async def test_with_enrollment_port_enrolled_false(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        enrollment = InMemoryVoiceEnrollmentPort(result=False)
        await repo.save(_make_profile(telegram_id=60))

        uc = SaveVoiceSampleUseCase(repo, storage, enrollment=enrollment)
        saved, enrolled = await uc.execute(telegram_id=60, data=b"audio", ext="ogg")

        assert saved is True
        assert enrolled is False

    async def test_without_enrollment_port_enrolled_is_false(self) -> None:
        repo = InMemoryUserProfileRepository()
        storage = InMemoryVoiceSampleStorage()
        await repo.save(_make_profile(telegram_id=70))

        uc = SaveVoiceSampleUseCase(repo, storage)
        saved, enrolled = await uc.execute(telegram_id=70, data=b"audio", ext="ogg")

        assert saved is True
        assert enrolled is False
