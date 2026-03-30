"""Tests for ATS2 REST Client & AuthManager."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ats_processing.infrastructure.ats2_client import ATS2AuthManager, ATS2RestClient

# ============================================================
# ATS2AuthManager tests
# ============================================================


class TestATS2AuthManager:
    """Tests for token management and auto-refresh."""

    def _make_manager(
        self,
        access_token: str = "access-123",
        refresh_token: str = "refresh-456",
        base_url: str = "https://ats2.t2.ru/crm/openapi",
    ) -> ATS2AuthManager:
        return ATS2AuthManager(
            access_token=access_token,
            refresh_token=refresh_token,
            base_url=base_url,
        )

    @pytest.mark.asyncio
    async def test_auth_manager_uses_cached_token(self) -> None:
        """AuthManager возвращает кешированный access token без HTTP-запросов."""
        manager = self._make_manager(access_token="cached-token")

        token = await manager.get_access_token()

        assert token == "cached-token"

    @pytest.mark.asyncio
    async def test_auth_manager_refreshes_token_on_403(self) -> None:
        """AuthManager обновляет access token при вызове refresh()."""
        manager = self._make_manager()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"access_token": "new-access-token"}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.put = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            new_token = await manager.refresh()

        assert new_token == "new-access-token"
        # Cached token should be updated
        assert await manager.get_access_token() == "new-access-token"

        # Verify the PUT was called with refresh token in Authorization header
        mock_client.put.assert_called_once()
        call_args = mock_client.put.call_args
        assert "authorization/refresh/token" in call_args[0][0]


# ============================================================
# ATS2RestClient tests
# ============================================================


class TestATS2RestClient:
    """Tests for the ATS2 REST client endpoints."""

    def _make_client(self) -> tuple[ATS2RestClient, AsyncMock]:
        auth_manager = AsyncMock(spec=ATS2AuthManager)
        auth_manager.get_access_token = AsyncMock(return_value="test-token")
        auth_manager.refresh = AsyncMock(return_value="refreshed-token")
        client = ATS2RestClient(
            auth_manager=auth_manager,
            base_url="https://ats2.t2.ru/crm/openapi",
        )
        return client, auth_manager

    @pytest.mark.asyncio
    async def test_get_call_records_with_date_filter(self) -> None:
        """GET /call-records/info с фильтрацией по дате возвращает список записей."""
        client, _auth = self._make_client()

        records_data = [
            {
                "id": "rec-1",
                "filename": "call1.mp3",
                "startTime": "2026-03-30T10:00:00",
                "duration": 120,
                "callerNumber": "+79001234567",
                "agentExt": "101",
            }
        ]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = records_data
        mock_response.raise_for_status = MagicMock()

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = records_data
            date_from = datetime(2026, 3, 30, 0, 0, 0)
            date_to = datetime(2026, 3, 30, 23, 59, 59)

            result = await client.get_call_records(date_from=date_from, date_to=date_to)

        assert len(result) == 1
        assert result[0]["id"] == "rec-1"
        mock_req.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_recording_returns_bytes(self) -> None:
        """GET /call-records/file?filename= возвращает bytes MP3-файла."""
        client, _auth = self._make_client()

        audio_bytes = b"\xff\xfb\x90\x00" * 100  # fake MP3

        with patch.object(client, "_request_bytes", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = audio_bytes

            result = await client.download_recording(filename="call1.mp3")

        assert isinstance(result, bytes)
        assert len(result) == 400
        mock_req.assert_called_once_with(
            "GET", "/call-records/file", params={"filename": "call1.mp3"}
        )

    @pytest.mark.asyncio
    async def test_get_transcription_returns_word_list(self) -> None:
        """GET /call-records/file/stt?filename= возвращает транскрипцию."""
        client, _auth = self._make_client()

        stt_data = {
            "words": [
                {"word": "Здравствуйте", "start": 0.0, "end": 0.5},
                {"word": "меня", "start": 0.5, "end": 0.7},
                {"word": "зовут", "start": 0.7, "end": 1.0},
            ]
        }

        with patch.object(client, "_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = stt_data

            result = await client.get_transcription(filename="call1.mp3")

        assert "words" in result
        words = result["words"]
        assert isinstance(words, list)
        assert len(words) == 3
        assert words[0]["word"] == "Здравствуйте"

    @pytest.mark.asyncio
    async def test_client_retries_after_token_refresh(self) -> None:
        """Клиент повторяет запрос после обновления токена при 403."""
        client, auth_manager = self._make_client()

        # First call returns 403, second succeeds
        response_403 = httpx.Response(
            status_code=403,
            request=httpx.Request("GET", "https://ats2.t2.ru/crm/openapi/employees"),
        )
        response_ok = MagicMock()
        response_ok.status_code = 200
        response_ok.json.return_value = [{"id": "emp-1", "name": "Иванов"}]
        response_ok.raise_for_status = MagicMock()

        mock_http_client = AsyncMock()
        mock_http_client.request = AsyncMock(side_effect=[response_403, response_ok])
        mock_http_client.aclose = AsyncMock()

        client._http = mock_http_client

        result = await client._request("GET", "/employees")

        assert result == [{"id": "emp-1", "name": "Иванов"}]
        # Auth manager refresh should have been called
        auth_manager.refresh.assert_called_once()
        # Two HTTP requests: original 403 + retry
        assert mock_http_client.request.call_count == 2
