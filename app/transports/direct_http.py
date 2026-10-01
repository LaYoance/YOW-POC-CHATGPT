"""
Mode 1: DIRECT_HTTP transport using curl_cffi with TLS/JA4 browser impersonation (R4).
"""

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from curl_cffi.requests import AsyncSession

from app.config import settings
from app.models.conversation import Conversation, ConversationSchema, SendMessageRequest
from app.models.project import Project, ProjectFileSchema, ProjectSchema
from app.models.session import SessionStatus, SessionStatusResponse
from app.models.transport import TransportMode
from app.persistence.session_repo import SessionRepo
from app.transports.base import (
    ChatGPTTransport,
    CloudflareChallengeError,
    RateLimitError,
    SessionExpiredError,
    TransportError,
)

logger = logging.getLogger(__name__)


class DirectHttpTransport(ChatGPTTransport):
    """
    Direct HTTP client impersonating Chrome TLS fingerprint via curl_cffi.
    Evaluates Mode 1 capabilities and failure modes (e.g. Cloudflare Turnstile).
    """

    def __init__(
        self,
        session_repo: Optional[SessionRepo] = None,
        base_url: Optional[str] = None,
        impersonate: str = "chrome124",
    ):
        self.session_repo = session_repo or SessionRepo()
        self.base_url = (base_url or settings.UPSTREAM_BASE_URL).rstrip("/")
        self.impersonate = impersonate

    @property
    def _effective_impersonate(self) -> Optional[str]:
        """Only apply TLS impersonation for HTTPS upstream; avoid h2c upgrade issues on plain HTTP."""
        if self.base_url.startswith("https://"):
            return self.impersonate
        return None

    @property
    def mode(self) -> TransportMode:
        return TransportMode.DIRECT_HTTP

    async def _get_auth_headers(self) -> Dict[str, str]:
        """Load active session credentials and format headers."""
        session = self.session_repo.get_default()
        if not session or session.status == SessionStatus.EXPIRED or not session.credentials.is_usable:
            raise SessionExpiredError("Session is expired or invalid in local repository.", mode=self.mode)

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
            "oai-device-id": session.credentials.device_id or str(uuid.uuid4()),
        }

        if session.credentials.access_token:
            headers["Authorization"] = f"Bearer {session.credentials.access_token}"

        raw_cookie = session.credentials.raw_cookie_header
        if raw_cookie:
            headers["Cookie"] = raw_cookie

        return headers

    def _check_response_status(self, status_code: int, text: str) -> None:
        """Inspect status code and response body for anti-bot / auth errors."""
        if status_code == 401:
            raise SessionExpiredError(f"Upstream returned 401 Unauthorized: {text[:200]}", status_code=401, mode=self.mode)
        elif status_code == 403:
            raise CloudflareChallengeError(f"Cloudflare Turnstile/403 challenge encountered: {text[:200]}", status_code=403, mode=self.mode)
        elif status_code == 429:
            raise RateLimitError(f"Upstream rate limit exceeded: {text[:200]}", status_code=429, mode=self.mode)
        elif status_code >= 400:
            raise TransportError(f"HTTP error {status_code}: {text[:200]}", status_code=status_code, mode=self.mode)

        # Detect Cloudflare challenge page inside 200 response if any
        if "Just a moment..." in text or "cf-turnstile" in text:
            raise CloudflareChallengeError("Cloudflare interstitial challenge page detected in body.", status_code=403, mode=self.mode)

    async def check_session(self) -> SessionStatusResponse:
        """Validate session using GET /api/auth/session."""
        headers = await self._get_auth_headers()
        url = f"{self.base_url}/api/auth/session"

        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.get(url, headers=headers, timeout=15)
                self._check_response_status(res.status_code, res.text)
                data = res.json()
                has_user = bool(data.get("user"))
                if not has_user:
                    raise SessionExpiredError("Session invalid or expired according to upstream.", mode=self.mode)
                return SessionStatusResponse(
                    status=SessionStatus.VALID,
                    message="Session active and verified.",
                    user_email=data.get("user", {}).get("email"),
                    expires_at=data.get("expires"),
                )
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP check_session failed: {e}", mode=self.mode) from e

    async def fetch_projects(self, cursor: Optional[str] = None) -> List[Project]:
        """Fetch user projects via /backend-api/gizmos/snorlax/sidebar."""
        headers = await self._get_auth_headers()
        url = f"{self.base_url}/backend-api/gizmos/snorlax/sidebar"

        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.get(url, headers=headers, timeout=20)
                self._check_response_status(res.status_code, res.text)
                data = res.json()

                projects: List[Project] = []
                items = data.get("items", []) or data.get("gizmos", [])
                for item in items:
                    gizmo = item.get("gizmo", item)
                    g_id = gizmo.get("id") or gizmo.get("gizmo_id")
                    if not g_id:
                        continue
                    display = gizmo.get("display", {})
                    p = Project(
                        id=g_id,
                        title=display.get("name") or gizmo.get("name") or "Untitled Project",
                        description=display.get("description") or gizmo.get("description"),
                        instructions=gizmo.get("instructions"),
                        gizmo_id=g_id,
                        workspace_id=gizmo.get("workspace_id"),
                    )
                    projects.append(p)
                return projects
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP fetch_projects failed: {e}", mode=self.mode) from e

    async def fetch_project_detail(self, project_id: str) -> Optional[Project]:
        """Fetch project details and files via /backend-api/gizmos/{project_id}."""
        headers = await self._get_auth_headers()
        url = f"{self.base_url}/backend-api/gizmos/{project_id}"

        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.get(url, headers=headers, timeout=20)
                self._check_response_status(res.status_code, res.text)
                data = res.json()
                gizmo = data.get("gizmo", data)
                display = gizmo.get("display", {})

                # Parse attached files
                files: List[ProjectFileSchema] = []
                for f in gizmo.get("files", []):
                    files.append(
                        ProjectFileSchema(
                            id=f.get("id") or str(uuid.uuid4()),
                            name=f.get("name") or f.get("filename") or "unnamed",
                            size_bytes=f.get("size") or f.get("size_bytes"),
                            mime_type=f.get("mime_type") or f.get("type"),
                        )
                    )

                return Project(
                    id=project_id,
                    title=display.get("name") or gizmo.get("name") or project_id,
                    description=display.get("description") or gizmo.get("description"),
                    instructions=gizmo.get("instructions"),
                    gizmo_id=project_id,
                    files=files,
                    workspace_id=gizmo.get("workspace_id"),
                )
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP fetch_project_detail failed: {e}", mode=self.mode) from e

    async def fetch_conversations(
        self,
        project_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 28,
    ) -> Dict[str, Any]:
        """Fetch conversations with offset/limit pagination."""
        headers = await self._get_auth_headers()
        params = {
            "offset": str(offset),
            "limit": str(limit),
            "order": "updated",
        }
        if project_id:
            params["gizmo_id"] = project_id

        url = f"{self.base_url}/backend-api/conversations"

        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.get(url, headers=headers, params=params, timeout=20)
                self._check_response_status(res.status_code, res.text)
                data = res.json()

                items = data.get("items", [])
                total = data.get("total", len(items))
                has_missing = data.get("has_missing", False)
                return {
                    "items": items,
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                    "has_more": has_missing or (offset + len(items) < total),
                }
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP fetch_conversations failed: {e}", mode=self.mode) from e

    async def fetch_conversation_tree(self, conversation_id: str) -> Dict[str, Any]:
        """Fetch conversation DAG mapping from /backend-api/conversation/{id}."""
        headers = await self._get_auth_headers()
        url = f"{self.base_url}/backend-api/conversation/{conversation_id}"

        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.get(url, headers=headers, timeout=25)
                self._check_response_status(res.status_code, res.text)
                return res.json()
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP fetch_conversation_tree failed: {e}", mode=self.mode) from e

    async def send_message_stream(
        self,
        request: SendMessageRequest,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Send message via POST /backend-api/conversation and stream response chunks."""
        headers = await self._get_auth_headers()
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "text/event-stream"

        user_message_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "action": "next",
            "messages": [
                {
                    "id": user_message_id,
                    "author": {"role": "user"},
                    "content": {
                        "content_type": "text",
                        "parts": [request.content],
                    },
                    "metadata": {},
                }
            ],
            "model": request.model or "auto",
            "history_and_training_disabled": False,
        }

        if request.conversation_id:
            payload["conversation_id"] = request.conversation_id
        if request.parent_message_id:
            payload["parent_message_id"] = request.parent_message_id

        if request.project_id:
            payload["conversation_mode"] = {
                "kind": "gizmo_interaction",
                "gizmo_id": request.project_id,
            }
        else:
            payload["conversation_mode"] = {"kind": "primary_assistant"}

        url = f"{self.base_url}/backend-api/conversation"

        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.post(url, headers=headers, json=payload, stream=True, timeout=60)
                self._check_response_status(res.status_code, res.text if hasattr(res, "text") else "")

                accumulated_text = ""
                assistant_message_id: Optional[str] = None
                parent_id = user_message_id
                conv_id = request.conversation_id

                async for line in res.aiter_lines():
                    if not line:
                        continue
                    line_str = line.decode("utf-8") if isinstance(line, bytes) else line
                    line_str = line_str.strip()

                    if line_str == "data: [DONE]":
                        yield {
                            "event": "done",
                            "text": accumulated_text,
                            "message_id": assistant_message_id,
                            "parent_id": parent_id,
                            "conversation_id": conv_id,
                        }
                        break

                    if line_str.startswith("data: "):
                        data_json = line_str[6:]
                        try:
                            chunk = json.loads(data_json)
                        except json.JSONDecodeError:
                            continue

                        if "conversation_id" in chunk and not conv_id:
                            conv_id = chunk["conversation_id"]

                        msg = chunk.get("message")
                        if msg:
                            assistant_message_id = msg.get("id") or assistant_message_id
                            role = msg.get("author", {}).get("role")
                            if role == "assistant":
                                parts = msg.get("content", {}).get("parts", [])
                                if parts:
                                    current_full_text = "".join(parts)
                                    # Delta calculation
                                    delta = current_full_text[len(accumulated_text):]
                                    accumulated_text = current_full_text
                                    if delta:
                                        yield {
                                            "event": "delta",
                                            "delta": delta,
                                            "text": accumulated_text,
                                            "message_id": assistant_message_id,
                                            "parent_id": user_message_id,
                                            "conversation_id": conv_id,
                                        }

                if not accumulated_text:
                    yield {
                        "event": "done",
                        "text": accumulated_text,
                        "message_id": assistant_message_id,
                        "parent_id": user_message_id,
                        "conversation_id": conv_id,
                    }

        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP send_message_stream failed: {e}", mode=self.mode) from e

    async def get_interpreter_download_url(
        self,
        conversation_id: str,
        message_id: str,
        sandbox_path: str,
    ) -> str:
        headers = await self._get_auth_headers()
        url = f"{self.base_url}/backend-api/conversation/{conversation_id}/interpreter/download"
        params = {
            "message_id": message_id,
            "sandbox_path": sandbox_path,
        }
        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.get(url, headers=headers, params=params, timeout=20)
                self._check_response_status(res.status_code, res.text)
                data = res.json()
                download_url = data.get("download_url") if isinstance(data, dict) else None
                if not download_url:
                    raise TransportError(f"No download_url in response: {res.text[:200]}", mode=self.mode)
                return download_url
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP get_interpreter_download_url failed: {e}", mode=self.mode) from e

    async def download_file_bytes(self, url: str) -> bytes:
        headers = await self._get_auth_headers()
        try:
            async with AsyncSession(impersonate=self._effective_impersonate) as client:
                res = await client.get(url, headers=headers, timeout=60)
                self._check_response_status(res.status_code, res.text[:200] if res.status_code != 200 else "")
                return res.content
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"DIRECT_HTTP download_file_bytes failed: {e}", mode=self.mode) from e
