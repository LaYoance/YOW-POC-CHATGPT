"""
Regression Test Suite for v1.1.0 Review Defects (v1.2.0 Hardening).
Covers:
1. TransportRouter: Anti-duplicate protection on interrupted stream, upstream reconciliation or uncertain state.
2. UiDrivenTransport: Strict rejection of empty response, timeout, or unconfirmed message IDs.
3. SyncService: Official user & assistant ID preservation and upstream DAG reconciliation.
4. Shared Router: Single shared instance across routes and services; strict forced mode enforcement.
5. DOM / Markdown: HTML sanitization with DOMPurify and script tag elimination.
"""

import asyncio
import os
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient

from app.main import app
from app.models.conversation import ActiveBranchResponse, Conversation, Message, SendMessageRequest
from app.models.transport import TransportMode
from app.persistence.conversation_repo import ConversationRepo
from app.persistence.db import DatabaseManager
from app.services.conversation_service import ConversationService
from app.services.sync_service import SyncService
from app.transports.base import ChatGPTTransport, TransportError
from app.transports.router import TransportRouter, get_shared_router, set_shared_router


@pytest.fixture
def clean_shared_router():
    """Ensure shared router is clean before and after each test."""
    original = get_shared_router()
    fresh = TransportRouter()
    set_shared_router(fresh)
    yield fresh
    set_shared_router(original)


class DummyMockTransport(ChatGPTTransport):
    def __init__(self, mode: TransportMode):
        self._mode = mode
        self.send_call_count = 0

    @property
    def mode(self) -> TransportMode:
        return self._mode

    async def check_session(self):
        return None

    async def fetch_projects(self, cursor=None):
        return []

    async def fetch_project_detail(self, project_id: str):
        return None

    async def fetch_conversations(self, **kwargs):
        return {"items": [], "total": 0}

    async def fetch_conversation_tree(self, conversation_id: str):
        return {"current_node": None, "mapping": {}}

    async def send_message_stream(self, request: SendMessageRequest):
        self.send_call_count += 1
        yield {"event": "delta", "text": "Hello"}
        yield {"event": "done", "text": "Hello world", "message_id": "mock-assistant-1"}

    async def get_interpreter_download_url(self, **kwargs):
        return None

    async def download_file_bytes(self, url: str) -> bytes:
        return b""


# ---------------------------------------------------------------------------
# Defect 1: TransportRouter anti-duplicate & uncertain state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_router_anti_duplicate_on_interrupted_stream():
    """
    If a stream begins and yields a chunk (indicating submission was accepted upstream),
    but subsequently fails, the router must NOT failover to another transport.
    Instead, it must attempt upstream tree reconciliation, or yield an 'uncertain' event.
    """
    router = TransportRouter(log_repo=MagicMock())

    # Transport 1: Yields one delta then crashes
    class CrashingAcceptedTransport(DummyMockTransport):
        async def send_message_stream(self, request: SendMessageRequest):
            self.send_call_count += 1
            yield {"event": "delta", "text": "Starting partial generation..."}
            raise ConnectionResetError("Stream interrupted mid-generation")

    # Transport 2: Backup transport that must NEVER be called
    class BackupTransport(DummyMockTransport):
        async def send_message_stream(self, request: SendMessageRequest):
            self.send_call_count += 1
            yield {"event": "done", "text": "Duplicate response", "message_id": "dup-id"}

    t1 = CrashingAcceptedTransport(TransportMode.UI_DRIVEN)
    t2 = BackupTransport(TransportMode.DIRECT_HTTP)
    router.transports = {TransportMode.UI_DRIVEN: t1, TransportMode.DIRECT_HTTP: t2}
    router.fallback_chain = [TransportMode.UI_DRIVEN, TransportMode.DIRECT_HTTP]

    # Case A: Upstream reconciliation fails to find completed assistant response -> returns 'uncertain'
    req = SendMessageRequest(
        message="Test prompt",
        conversation_id="conv-1234",
        parent_message_id="parent-001",
    )

    with patch.object(router, "fetch_conversation_tree", AsyncMock(return_value={"current_node": None})):
        events = []
        async for chunk in router.send_message_stream(req):
            events.append(chunk)

    # Verify backup transport was NOT called (prevent duplicate send)
    assert t1.send_call_count == 1
    assert t2.send_call_count == 0

    # Verify 'uncertain' event was returned
    assert any(e.get("event") == "uncertain" for e in events)
    uncertain_evt = next(e for e in events if e.get("event") == "uncertain")
    assert uncertain_evt["status"] == "uncertain"
    assert "resubmission aborted" in uncertain_evt["error"]


@pytest.mark.asyncio
async def test_router_anti_duplicate_with_successful_upstream_reconciliation():
    """
    If stream is interrupted after acceptance, but upstream reconciliation recovers
    the canonical assistant message from tree, router yields a reconciled 'done' event.
    """
    router = TransportRouter(log_repo=MagicMock())

    class CrashingAcceptedTransport(DummyMockTransport):
        async def send_message_stream(self, request: SendMessageRequest):
            self.send_call_count += 1
            yield {"event": "delta", "text": "Starting..."}
            raise ConnectionResetError("Broken pipe")

    t1 = CrashingAcceptedTransport(TransportMode.UI_DRIVEN)
    t2 = DummyMockTransport(TransportMode.DIRECT_HTTP)
    router.transports = {TransportMode.UI_DRIVEN: t1, TransportMode.DIRECT_HTTP: t2}
    router.fallback_chain = [TransportMode.UI_DRIVEN, TransportMode.DIRECT_HTTP]

    canonical_tree = {
        "current_node": "official-assistant-id-999",
        "mapping": {
            "official-assistant-id-999": {
                "message": {
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Reconciled response from chatgpt.com"]},
                },
                "parent": "official-user-id-888",
            }
        },
    }

    req = SendMessageRequest(
        message="Test prompt",
        conversation_id="conv-5678",
        parent_message_id="parent-002",
    )

    with patch.object(router, "fetch_conversation_tree", AsyncMock(return_value=canonical_tree)):
        events = []
        async for chunk in router.send_message_stream(req):
            events.append(chunk)

    assert t1.send_call_count == 1
    assert t2.send_call_count == 0

    # Verify done event reconciled from tree
    done_evt = next((e for e in events if e.get("event") == "done"), None)
    assert done_evt is not None
    assert done_evt["message_id"] == "official-assistant-id-999"
    assert done_evt["parent_id"] == "official-user-id-888"
    assert done_evt["reconciled"] is True
    assert "Reconciled response" in done_evt["text"]


# ---------------------------------------------------------------------------
# Defect 2: UiDrivenTransport rejects empty, timeout, or unconfirmed IDs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ui_driven_rejects_empty_or_timeout_response():
    """UiDrivenTransport must never yield success on empty response or generation timeout."""
    from app.transports.ui_driven import UiDrivenTransport

    transport = UiDrivenTransport(session_repo=MagicMock())

    # Case A: Generation times out (no assistant text, generation_completed is False)
    mock_page = AsyncMock()
    mock_page.url = "https://chatgpt.com/c/test-conv-id"
    mock_input = AsyncMock()
    mock_page.wait_for_selector = AsyncMock(return_value=mock_input)
    mock_page.query_selector = AsyncMock(return_value=None)
    mock_page.evaluate = AsyncMock(return_value="")  # always empty turn text
    transport._ensure_browser = AsyncMock(return_value=mock_page)

    req = SendMessageRequest(message="Test", conversation_id="test-conv-id")

    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(TransportError) as exc_info:
            async for _ in transport.send_message_stream(req):
                pass
    assert "generation timed out" in str(exc_info.value).lower()

    # Case B: Response is whitespace only (accumulated_text.strip() is empty)
    # 1st evaluate is initial_turns_count (0), 2nd is turn_text ("   ")
    mock_page.evaluate = AsyncMock(side_effect=[0, "   ", "   "])
    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(TransportError) as exc_info:
            async for _ in transport.send_message_stream(req):
                pass
    assert "empty response" in str(exc_info.value).lower()

    # Case C: Canonical message IDs cannot be confirmed from tree
    # Evaluates: 0 (initial count), "Valid text", "Valid text", then tree evaluate returns None
    mock_page.evaluate = AsyncMock(side_effect=[0, "Valid text", "Valid text", None])
    with patch("asyncio.sleep", AsyncMock()):
        with pytest.raises(TransportError) as exc_info:
            async for _ in transport.send_message_stream(req):
                pass
    assert "canonical message ids unconfirmed" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Defect 3: SyncService preserves official IDs and reconciles upstream DAG
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_service_preserves_official_ids_and_cleans_temporary_user_msg(isolated_db):
    """
    SyncService must reconcile post-send upstream DAG, delete temporary pre-send
    user message ID, and preserve official upstream IDs and parent links.
    """
    conv_repo = ConversationRepo(db=isolated_db)
    conv_id = "test-conv-official"
    official_user_id = "official-user-id-123"
    official_assistant_id = "official-assistant-id-456"

    # Upstream DAG tree returned after sending
    upstream_tree = {
        "conversation_id": conv_id,
        "current_node": official_assistant_id,
        "mapping": {
            "root": {"message": None, "parent": None, "children": [official_user_id]},
            official_user_id: {
                "message": {
                    "id": official_user_id,
                    "author": {"role": "user"},
                    "content": {"parts": ["Hello official"]},
                },
                "parent": "root",
                "children": [official_assistant_id],
            },
            official_assistant_id: {
                "message": {
                    "id": official_assistant_id,
                    "author": {"role": "assistant"},
                    "content": {"parts": ["Hello response"]},
                },
                "parent": official_user_id,
                "children": [],
            },
        },
    }

    mock_router = MagicMock(spec=TransportRouter)

    async def mock_stream(_):
        yield {"event": "delta", "text": "Hello response"}
        yield {
            "event": "done",
            "text": "Hello response",
            "conversation_id": conv_id,
            "message_id": official_assistant_id,
            "user_message_id": official_user_id,
            "parent_id": "root",
        }

    mock_router.send_message_stream = mock_stream
    mock_router.fetch_conversation_tree = AsyncMock(return_value=upstream_tree)

    sync_service = SyncService(
        conversation_repo=conv_repo,
        transport_router=mock_router,
    )

    events = []
    async for chunk in sync_service.send_and_stream(
        conversation_id=conv_id,
        project_id=None,
        prompt="Hello official",
    ):
        events.append(chunk)

    done_evt = next(e for e in events if e.get("event") == "done")
    assert done_evt["message_id"] == official_assistant_id
    assert done_evt["user_message_id"] == official_user_id

    # Verify database persistence:
    # 1. No temporary user UUIDs remain; only official_user_id exists for user
    with isolated_db.get_connection() as conn:
        user_msgs = conn.execute("SELECT id, role, parent_id FROM messages WHERE conversation_id = ? AND role = 'user'", (conv_id,)).fetchall()
        assert len(user_msgs) == 1
        assert user_msgs[0][0] == official_user_id
        assert user_msgs[0][2] == "root"

        asst_msgs = conn.execute("SELECT id, role, parent_id FROM messages WHERE conversation_id = ? AND role = 'assistant'", (conv_id,)).fetchall()
        assert len(asst_msgs) == 1
        assert asst_msgs[0][0] == official_assistant_id
        assert asst_msgs[0][2] == official_user_id


@pytest.mark.asyncio
async def test_sync_service_handles_uncertain_event_safely(isolated_db):
    """SyncService yields 'uncertain' event safely without corrupting conversation."""
    conv_repo = ConversationRepo(db=isolated_db)
    mock_router = MagicMock(spec=TransportRouter)

    async def mock_stream(_):
        yield {
            "event": "uncertain",
            "status": "uncertain",
            "error": "Upstream timeout: delivery unconfirmed",
        }

    mock_router.send_message_stream = mock_stream

    sync_service = SyncService(
        conversation_repo=conv_repo,
        transport_router=mock_router,
    )

    events = []
    async for chunk in sync_service.send_and_stream(conversation_id="c1", project_id=None, prompt="Prompt"):
        events.append(chunk)

    assert len(events) == 1
    assert events[0]["event"] == "uncertain"
    assert "delivery unconfirmed" in events[0]["error"]


# ---------------------------------------------------------------------------
# Defect 4: Shared Router across routes & services and forced mode compliance
# ---------------------------------------------------------------------------

def test_shared_router_instance_and_forced_mode_compliance(clean_shared_router):
    """
    Verify get_shared_router returns the exact instance used across routes_transport,
    ConversationService, ProjectService, and SyncService, and forced mode strictly binds them.
    """
    shared = get_shared_router()

    # Verify services default to shared router
    conv_service = ConversationService()
    assert conv_service.transport_router is shared

    sync_service = SyncService()
    assert sync_service.transport_router is shared

    # Test forced mode setting via API
    client = TestClient(app)
    res_set = client.post("/api/transport/mode", json={"mode": "UI_DRIVEN"})
    assert res_set.status_code == 200
    assert res_set.json()["forced_mode"] == "UI_DRIVEN"

    # Verify shared router is strictly locked to UI_DRIVEN
    assert shared.forced_mode == TransportMode.UI_DRIVEN
    assert conv_service.transport_router.forced_mode == TransportMode.UI_DRIVEN

    # Status route reflects the exact shared router
    res_status = client.get("/api/transport/status")
    assert res_status.status_code == 200
    assert res_status.json()["forced_mode"] == "UI_DRIVEN"

    # Reset forced mode
    res_reset = client.post("/api/transport/mode", json={"mode": None})
    assert res_reset.status_code == 200
    assert res_reset.json()["forced_mode"] is None
    assert shared.forced_mode is None


# ---------------------------------------------------------------------------
# Defect 5: DOMPurify and Markdown HTML sanitization
# ---------------------------------------------------------------------------

def test_dompurify_script_served_and_referenced():
    """Verify dompurify.min.js is served by static files and included in index.html."""
    client = TestClient(app)

    # 1. Verify index.html includes dompurify.min.js
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert '<script src="/static/js/dompurify.min.js"></script>' in res_index.text

    # 2. Verify dompurify.min.js is accessible and contains valid library header
    res_dompurify = client.get("/static/js/dompurify.min.js")
    assert res_dompurify.status_code == 200
    assert "DOMPurify" in res_dompurify.text

    # 3. Verify app.js references DOMPurify.sanitize
    res_app = client.get("/static/js/app.js")
    assert res_app.status_code == 200
    assert "window.DOMPurify.sanitize" in res_app.text
