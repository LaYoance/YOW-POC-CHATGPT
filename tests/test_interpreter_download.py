"""
Tests for Code Interpreter Sandbox File Download Feature.
"""

import pytest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from fastapi.testclient import TestClient

from app.main import app
from app.services.conversation_service import ConversationService
from app.config import settings


@pytest.mark.asyncio
async def test_download_interpreter_file_validation():
    service = ConversationService()
    with pytest.raises(ValueError):
        await service.download_interpreter_file("conv-123", "")

    with pytest.raises(ValueError):
        await service.download_interpreter_file("conv-123", "..")


@pytest.mark.asyncio
async def test_download_interpreter_file_cache_hit(tmp_path):
    # Mock settings.full_session_data_dir to tmp_path
    with patch.object(settings, "SESSION_DATA_DIR", tmp_path):
        service = ConversationService()
        conv_id = "test-conv-001"
        download_dir = tmp_path / "downloads" / conv_id
        download_dir.mkdir(parents=True, exist_ok=True)
        cached_file = download_dir / "sample.zip"
        cached_file.write_bytes(b"PK\x03\x04mock_zip_content")

        file_path, filename, media_type = await service.download_interpreter_file(
            conversation_id=conv_id,
            sandbox_path="sandbox:/mnt/data/sample.zip",
        )

        assert file_path == cached_file
        assert filename == "sample.zip"
        assert media_type == "application/zip"
        assert file_path.read_bytes() == b"PK\x03\x04mock_zip_content"


@pytest.mark.asyncio
async def test_download_interpreter_file_upstream_fetch(tmp_path):
    with patch.object(settings, "SESSION_DATA_DIR", tmp_path):
        service = ConversationService()
        conv_id = "test-conv-002"
        msg_id = "test-msg-002"

        # Mock transport router methods
        service.transport_router.get_interpreter_download_url = AsyncMock(
            return_value="https://chatgpt.com/backend-api/estuary/content?test=1"
        )
        service.transport_router.download_file_bytes = AsyncMock(
            return_value=b"PK\x03\x04upstream_downloaded_bytes"
        )

        file_path, filename, media_type = await service.download_interpreter_file(
            conversation_id=conv_id,
            sandbox_path="/mnt/data/upstream_result.zip",
            message_id=msg_id,
        )

        service.transport_router.get_interpreter_download_url.assert_awaited_once_with(
            conversation_id=conv_id,
            message_id=msg_id,
            sandbox_path="/mnt/data/upstream_result.zip",
        )
        service.transport_router.download_file_bytes.assert_awaited_once_with(
            "https://chatgpt.com/backend-api/estuary/content?test=1"
        )

        assert file_path.exists()
        assert filename == "upstream_result.zip"
        assert media_type == "application/zip"
        assert file_path.read_bytes() == b"PK\x03\x04upstream_downloaded_bytes"


def test_api_download_endpoint_success(tmp_path):
    with patch.object(settings, "SESSION_DATA_DIR", tmp_path):
        conv_id = "test-conv-003"
        download_dir = tmp_path / "downloads" / conv_id
        download_dir.mkdir(parents=True, exist_ok=True)
        sample_file = download_dir / "report.pdf"
        sample_file.write_bytes(b"%PDF-1.4 mock pdf content")

        client = TestClient(app)
        res = client.get(
            f"/api/conversations/{conv_id}/interpreter/download",
            params={"sandbox_path": "sandbox:/mnt/data/report.pdf"},
        )

        assert res.status_code == 200
        assert res.headers["content-type"] == "application/pdf"
        assert 'attachment; filename="report.pdf"' in res.headers["content-disposition"]
        assert res.content == b"%PDF-1.4 mock pdf content"


def test_api_download_endpoint_invalid_path():
    client = TestClient(app)
    res = client.get(
        "/api/conversations/test-conv/interpreter/download",
        params={"sandbox_path": ""},
    )
    assert res.status_code in (404, 422)
