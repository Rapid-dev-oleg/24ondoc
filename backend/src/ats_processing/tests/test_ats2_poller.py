"""Tests for ATS2PollerService (DEV-128)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from ..application.ats2_poller import ATS2PollerService
from ..application.ats2_transcription_mapper import ATS2TranscriptionMapper
from ..application.ports import ATS2CallSourcePort
from ..domain.models import CallRecord, SourceType
from ..domain.repository import CallRecordRepository

# ---------- Stubs ----------


class StubCallRepo(CallRecordRepository):
    def __init__(self, existing_ids: set[str] | None = None) -> None:
        self._records: dict[str, CallRecord] = {}
        self._existing_ids = existing_ids or set()
        self.saved: list[CallRecord] = []

    async def get_by_id(self, call_id: str) -> CallRecord | None:
        return self._records.get(call_id)

    async def save(self, record: CallRecord) -> None:
        self._records[record.call_id] = record
        self.saved.append(record)

    async def get_pending(
        self, limit: int = 10, source: SourceType | None = None
    ) -> list[CallRecord]:
        return []

    async def find_recent_by_phone(self, phone: str, limit: int = 10) -> list[CallRecord]:
        return []

    def has_call(self, call_id: str) -> bool:
        return call_id in self._existing_ids or call_id in self._records

    def seed(self, record: CallRecord) -> None:
        self._records[record.call_id] = record
        self._existing_ids.add(record.call_id)


class StubATS2Client(ATS2CallSourcePort):
    def __init__(
        self,
        call_records: list[dict[str, object]] | None = None,
        recording_bytes: bytes = b"fake-mp3-audio",
        transcription: dict[str, object] | None = None,
        raise_on_get: Exception | None = None,
    ) -> None:
        self._call_records = call_records or []
        self._recording_bytes = recording_bytes
        self._transcription = transcription or {"words": []}
        self._raise_on_get = raise_on_get
        self.get_call_records_calls: list[tuple[datetime, datetime]] = []
        self.download_calls: list[str] = []
        self.transcription_calls: list[str] = []

    async def get_call_records(
        self, date_from: datetime, date_to: datetime
    ) -> list[dict[str, object]]:
        if self._raise_on_get:
            raise self._raise_on_get
        self.get_call_records_calls.append((date_from, date_to))
        return self._call_records

    async def download_recording(self, filename: str) -> bytes:
        self.download_calls.append(filename)
        return self._recording_bytes

    async def get_transcription(self, filename: str) -> dict[str, object]:
        self.transcription_calls.append(filename)
        return self._transcription

    async def get_active_calls(self) -> list[dict[str, object]]:
        return []

    async def get_employees(self) -> list[dict[str, object]]:
        return []


def _make_ats2_call(
    call_id: str = "ats2_001",
    filename: str = "rec_001.mp3",
    caller_phone: str = "+79991234567",
    agent_ext: str = "101",
    duration: int = 120,
    call_date: str = "2026-03-30T12:00:00",
) -> dict[str, object]:
    return {
        "id": call_id,
        "filename": filename,
        "callerPhone": caller_phone,
        "agentExt": agent_ext,
        "duration": duration,
        "callDate": call_date,
    }


def _build_poller(
    call_repo: StubCallRepo | None = None,
    ats2_client: StubATS2Client | None = None,
    process_call: AsyncMock | None = None,
    poll_interval: float = 60.0,
) -> ATS2PollerService:
    repo = call_repo or StubCallRepo()
    client = ats2_client or StubATS2Client()
    process = process_call or AsyncMock()
    mapper = ATS2TranscriptionMapper()

    return ATS2PollerService(
        ats2_client=client,
        call_repo=repo,
        process_call_webhook=process,
        transcription_mapper=mapper,
        poll_interval_sec=poll_interval,
    )


# ============================================================
# Tests
# ============================================================


class TestATS2PollerService:
    @pytest.mark.asyncio
    async def test_poller_fetches_new_calls_since_last_timestamp(self) -> None:
        """AC: поллер запрашивает записи начиная с last_poll_timestamp."""
        client = StubATS2Client(call_records=[_make_ats2_call()])
        repo = StubCallRepo()
        process = AsyncMock()
        poller = _build_poller(call_repo=repo, ats2_client=client, process_call=process)

        # Set a known last_poll_timestamp
        last_ts = datetime(2026, 3, 30, 10, 0, 0, tzinfo=UTC)
        poller._last_poll_timestamp = last_ts

        await poller.poll_once()

        assert len(client.get_call_records_calls) == 1
        date_from, _date_to = client.get_call_records_calls[0]
        assert date_from == last_ts

    @pytest.mark.asyncio
    async def test_poller_skips_already_processed_calls(self) -> None:
        """AC: звонок с уже существующим call_id пропускается."""
        repo = StubCallRepo()
        existing = CallRecord(
            call_id="ats2_dup",
            audio_url="https://ats2.example.com/rec/dup.mp3",
            source=SourceType.CALL_ATS2_POLLING,
        )
        repo.seed(existing)

        client = StubATS2Client(call_records=[_make_ats2_call(call_id="ats2_dup")])
        process = AsyncMock()
        poller = _build_poller(call_repo=repo, ats2_client=client, process_call=process)

        await poller.poll_once()

        # ProcessCallWebhook should NOT be called for duplicates
        process.execute.assert_not_awaited()
        # No new records saved (only the seed exists)
        assert len(repo.saved) == 0

    @pytest.mark.asyncio
    async def test_poller_creates_call_record_with_correct_source(self) -> None:
        """AC: новая запись создаётся с source=CALL_ATS2_POLLING."""
        client = StubATS2Client(call_records=[_make_ats2_call(call_id="ats2_new")])
        repo = StubCallRepo()
        process = AsyncMock()
        poller = _build_poller(call_repo=repo, ats2_client=client, process_call=process)

        await poller.poll_once()

        assert len(repo.saved) == 1
        saved_record = repo.saved[0]
        assert saved_record.call_id == "ats2_new"
        assert saved_record.source == SourceType.CALL_ATS2_POLLING

    @pytest.mark.asyncio
    async def test_poller_handles_api_error_gracefully(self) -> None:
        """AC: ошибка API не роняет поллер, он продолжает работу."""
        client = StubATS2Client(raise_on_get=ConnectionError("ATS2 unavailable"))
        repo = StubCallRepo()
        process = AsyncMock()
        poller = _build_poller(call_repo=repo, ats2_client=client, process_call=process)

        # Should not raise
        await poller.poll_once()

        # No records saved, no processing triggered
        assert len(repo.saved) == 0
        process.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_poller_updates_last_poll_timestamp_on_success(self) -> None:
        """AC: после успешного опроса last_poll_timestamp обновляется."""
        client = StubATS2Client(call_records=[_make_ats2_call()])
        repo = StubCallRepo()
        process = AsyncMock()
        poller = _build_poller(call_repo=repo, ats2_client=client, process_call=process)

        before = poller._last_poll_timestamp
        await poller.poll_once()
        after = poller._last_poll_timestamp

        assert after > before

    @pytest.mark.asyncio
    async def test_poller_downloads_recording_and_transcription(self) -> None:
        """AC: для нового звонка скачивается запись и транскрипция."""
        transcription_data: dict[str, object] = {
            "words": [
                {"channel": "A", "startTime": 0.0, "endTime": 0.5, "word": "Алло"},
                {"channel": "B", "startTime": 1.0, "endTime": 1.5, "word": "Да"},
            ]
        }
        client = StubATS2Client(
            call_records=[_make_ats2_call(call_id="ats2_rec", filename="rec_rec.mp3")],
            transcription=transcription_data,
        )
        repo = StubCallRepo()
        process = AsyncMock()
        poller = _build_poller(call_repo=repo, ats2_client=client, process_call=process)

        await poller.poll_once()

        # Recording was downloaded
        assert "rec_rec.mp3" in client.download_calls
        # Transcription was fetched
        assert "rec_rec.mp3" in client.transcription_calls
        # Saved record has transcription_t2 set
        assert len(repo.saved) == 1
        assert repo.saved[0].transcription_t2 is not None
        assert "Алло" in repo.saved[0].transcription_t2  # type: ignore[operator]

    @pytest.mark.asyncio
    async def test_poller_graceful_shutdown(self) -> None:
        """Graceful shutdown: поллер останавливается при вызове stop()."""
        poller = _build_poller(poll_interval=0.05)

        task = asyncio.create_task(poller.start())
        await asyncio.sleep(0.1)
        poller.stop()

        # Task should complete without error
        await asyncio.wait_for(task, timeout=2.0)
        assert poller._running is False

    @pytest.mark.asyncio
    async def test_poller_processes_multiple_new_calls(self) -> None:
        """Несколько новых звонков обрабатываются за один poll."""
        client = StubATS2Client(
            call_records=[
                _make_ats2_call(call_id="ats2_a", filename="a.mp3"),
                _make_ats2_call(call_id="ats2_b", filename="b.mp3"),
            ]
        )
        repo = StubCallRepo()
        process = AsyncMock()
        poller = _build_poller(call_repo=repo, ats2_client=client, process_call=process)

        await poller.poll_once()

        assert len(repo.saved) == 2
        assert process.execute.await_count == 2
        saved_ids = {r.call_id for r in repo.saved}
        assert saved_ids == {"ats2_a", "ats2_b"}
