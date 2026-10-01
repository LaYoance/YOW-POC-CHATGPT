"""
Abstract Base Class and exceptions for ChatGPT Transports (R4).
"""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, List, Optional

from app.models.conversation import Conversation, ConversationSchema, SendMessageRequest
from app.models.project import Project, ProjectSchema
from app.models.session import SessionStatusResponse
from app.models.transport import TransportMode


class TransportError(Exception):
    """Base exception for all transport errors."""
    def __init__(self, message: str, status_code: Optional[int] = None, mode: Optional[TransportMode] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.mode = mode


class SessionExpiredError(TransportError):
    """Raised when upstream returns 401 Unauthorized or token is invalid."""
    pass


class CloudflareChallengeError(TransportError):
    """Raised when request is blocked by Cloudflare (403, Turnstile, or PoW challenge)."""
    pass


class RateLimitError(TransportError):
    """Raised when upstream returns 429 Too Many Requests."""
    pass


class ChatGPTTransport(ABC):
    """
    Abstract interface implemented by all transport mechanisms:
    - Mode 1: DIRECT_HTTP
    - Mode 2: BROWSER_FETCH
    - Mode 3: UI_DRIVEN
    """

    @property
    @abstractmethod
    def mode(self) -> TransportMode:
        """Transport mechanism identifier."""
        pass

    @abstractmethod
    async def check_session(self) -> SessionStatusResponse:
        """Validate current session against upstream."""
        pass

    @abstractmethod
    async def fetch_projects(self, cursor: Optional[str] = None) -> List[Project]:
        """Fetch list of user projects (Gizmos)."""
        pass

    @abstractmethod
    async def fetch_project_detail(self, project_id: str) -> Optional[Project]:
        """Fetch detailed project metadata and attached files."""
        pass

    @abstractmethod
    async def fetch_conversations(
        self,
        project_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 28,
    ) -> Dict[str, Any]:
        """Fetch paginated conversations list."""
        pass

    @abstractmethod
    async def fetch_conversation_tree(self, conversation_id: str) -> Dict[str, Any]:
        """Fetch complete conversation DAG mapping from upstream."""
        pass

    @abstractmethod
    async def send_message_stream(
        self,
        request: SendMessageRequest,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Send message and stream assistant response chunks as SSE delta dicts:
        {"event": "delta"|"done"|"error", "text": "...", "message_id": "...", "parent_id": "..."}
        """
        pass

    @abstractmethod
    async def get_interpreter_download_url(
        self,
        conversation_id: str,
        message_id: str,
        sandbox_path: str,
    ) -> str:
        """Retrieve pre-signed download URL for a file in code interpreter sandbox."""
        pass

    @abstractmethod
    async def download_file_bytes(self, url: str) -> bytes:
        """Download raw binary content using authenticated session credentials."""
        pass
