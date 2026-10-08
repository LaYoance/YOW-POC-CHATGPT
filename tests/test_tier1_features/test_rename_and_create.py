import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from app.main import app
from app.models.conversation import ActiveBranchResponse, Message
from app.transports.base import TransportMode


@pytest.fixture
def client():
    return TestClient(app)


def test_rename_endpoint_validates_empty_title(client):
    res = client.patch("/api/conversations/conv-123", json={"title": "   "})
    assert res.status_code == 400


def test_rename_endpoint_calls_service(client):
    with patch("app.api.routes_conversations._conversation_service.rename_conversation", new_callable=AsyncMock) as mock_rename:
        mock_rename.return_value = "Nouveau Titre"
        res = client.patch("/api/conversations/conv-123", json={"title": "Nouveau Titre"})
        assert res.status_code == 200
        assert res.json() == {"ok": True, "conversation_id": "conv-123", "title": "Nouveau Titre"}
        mock_rename.assert_called_once_with("conv-123", "Nouveau Titre")


def test_create_conversation_endpoint_validates(client):
    res = client.post("/api/chat/conversations", json={"title": "", "prompt": "Hello"})
    assert res.status_code == 400

    res2 = client.post("/api/chat/conversations", json={"title": "Test", "prompt": ""})
    assert res2.status_code == 400


def test_create_conversation_endpoint_calls_service(client):
    with patch("app.api.routes_chat._sync_service.create_titled_conversation", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = {
            "conversation_id": "conv-456",
            "title": "Mon Titre",
            "title_verified": True,
            "project_id": "proj-1",
            "url": "https://chatgpt.com/g/proj-1/c/conv-456",
            "reply_preview": "Bonjour!",
        }
        res = client.post("/api/chat/conversations", json={
            "title": "Mon Titre",
            "prompt": "Bonjour",
            "project_id": "proj-1",
        })
        assert res.status_code == 200
        data = res.json()
        assert data["conversation_id"] == "conv-456"
        assert data["title"] == "Mon Titre"
        assert data["title_verified"] is True
        mock_create.assert_called_once_with(
            project_id="proj-1",
            title="Mon Titre",
            prompt="Bonjour",
            model="auto",
        )
