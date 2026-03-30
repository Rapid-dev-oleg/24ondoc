"""Telegram Ingestion — Registration and profile use cases."""

from __future__ import annotations

from ..domain.models import UserProfile, UserRole
from ..domain.repository import UserProfileRepository
from .ports import AgentRegistrationPort, VoiceEnrollmentPort, VoiceSampleStoragePort

_CRM_EMAIL_DOMAIN = "24ondoc.ru"
_TEMP_PASSWORD = "Temp_Password1"


class AutoRegisterUserUseCase:
    """Auto-create a Chatwoot agent + UserProfile on /start if user does not exist yet.

    Returns (profile, temp_password, is_new).  When is_new=False, temp_password is "".
    """

    def __init__(
        self,
        user_repo: UserProfileRepository,
        agent_registration: AgentRegistrationPort,
        account_id: int,
    ) -> None:
        self._user_repo = user_repo
        self._agent_registration = agent_registration
        self._account_id = account_id

    async def execute(self, telegram_id: int, first_name: str) -> tuple[UserProfile, str, bool]:
        existing = await self._user_repo.get_by_telegram_id(telegram_id)
        if existing is not None:
            return existing, "", False

        name = first_name.strip() or str(telegram_id)
        email = f"{telegram_id}@{_CRM_EMAIL_DOMAIN}"
        password = _TEMP_PASSWORD

        chatwoot_user_id = await self._agent_registration.create_chatwoot_agent(
            name, email, password
        )
        chatwoot_contact_id = await self._agent_registration.create_contact(name, email)

        profile = UserProfile(
            telegram_id=telegram_id,
            chatwoot_user_id=chatwoot_user_id,
            chatwoot_account_id=self._account_id,
            chatwoot_contact_id=chatwoot_contact_id,
            role=UserRole.AGENT,
            settings={"display_name": name, "email": email},
        )
        await self._user_repo.save(profile)

        return profile, password, True


class UpdateProfileFieldUseCase:
    """Update a named field inside the user profile settings JSONB dict."""

    def __init__(self, user_repo: UserProfileRepository) -> None:
        self._user_repo = user_repo

    async def execute(self, telegram_id: int, field: str, value: str) -> UserProfile | None:
        profile = await self._user_repo.get_by_telegram_id(telegram_id)
        if profile is None:
            return None
        new_settings = dict(profile.settings)
        new_settings[field] = value
        updated = profile.model_copy(update={"settings": new_settings})
        await self._user_repo.save(updated)
        return updated


class SaveVoiceSampleUseCase:
    """Persist a voice sample, update the user's voice_sample_url, and optionally enroll it."""

    def __init__(
        self,
        user_repo: UserProfileRepository,
        storage: VoiceSampleStoragePort,
        enrollment: VoiceEnrollmentPort | None = None,
    ) -> None:
        self._user_repo = user_repo
        self._storage = storage
        self._enrollment = enrollment

    async def execute(self, telegram_id: int, data: bytes, ext: str) -> tuple[bool, bool]:
        """Save bytes with given extension (ogg/mp3/wav) and update profile.

        Returns (saved, enrolled):
            saved=True if the sample was stored and the profile was updated,
            enrolled=True if the sample was also enrolled in the recognition system.
        """
        profile = await self._user_repo.get_by_telegram_id(telegram_id)
        if profile is None:
            return False, False
        path = await self._storage.save(telegram_id, data, ext)
        updated = profile.model_copy(update={"voice_sample_url": path})
        await self._user_repo.save(updated)

        enrolled = False
        if self._enrollment is not None:
            enrolled = await self._enrollment.enroll(profile.chatwoot_user_id, data)

        return True, enrolled
