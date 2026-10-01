"""
Mode 2: BROWSER_FETCH transport using Playwright CDP context evaluation (R4).
Bypasses Cloudflare Turnstile and TLS fingerprint checks by executing within real browser context.
"""

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from playwright.async_api import BrowserContext, Page, async_playwright

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


class BrowserFetchTransport(ChatGPTTransport):
    """
    Executes fetch() calls inside an authenticated Playwright browser context.
    Natively inherits CF cookies, HTTP/2 session, and Turnstile tokens.
    """

    def __init__(
        self,
        session_repo: Optional[SessionRepo] = None,
        user_data_dir: Optional[Path] = None,
        base_url: Optional[str] = None,
        headless: Optional[bool] = None,
    ):
        self.session_repo = session_repo or SessionRepo()
        self.user_data_dir = user_data_dir or (settings.full_session_data_dir / "chromium_fetch")
        self.base_url = (base_url or settings.UPSTREAM_BASE_URL).rstrip("/")
        self.headless = settings.BROWSER_HEADLESS if headless is None else headless

        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()
        self._stream_queues: Dict[str, asyncio.Queue] = {}

    @property
    def mode(self) -> TransportMode:
        return TransportMode.BROWSER_FETCH

    async def _ensure_browser(self) -> Page:
        """Initialize Playwright persistent context if not already active."""
        async with self._lock:
            if self._page and not self._page.is_closed():
                return self._page

            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            if not self._playwright:
                self._playwright = await async_playwright().start()

            args = [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-infobars",
                "--window-size=1280,800",
            ]
            if self.headless:
                args.append("--headless=new")

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=False,
                args=args,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )

            # Expose stream chunk handler binding
            try:
                await self._context.expose_binding(
                    "__antigravity_stream_chunk",
                    self._on_browser_stream_chunk,
                )
            except Exception:
                pass  # Already exposed

            # Inject cookies from repo with RFC 6265bis compliance for __Host- cookies
            session = self.session_repo.get_default()
            if session and session.credentials:
                for name, val in session.credentials.cookies.items():
                    if name.startswith("__Host-"):
                        cookie_obj = {
                            "name": name,
                            "value": val,
                            "url": "https://chatgpt.com",
                        }
                    else:
                        cookie_obj = {
                            "name": name,
                            "value": val,
                            "domain": ".chatgpt.com",
                            "path": "/",
                            "secure": True,
                        }
                    try:
                        await self._context.add_cookies([cookie_obj])
                    except Exception as e:
                        logger.debug(f"Warning adding cookie {name}: {e}")

            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()

            # Ensure page is on domain
            try:
                current_url = self._page.url
                if not current_url.startswith(self.base_url):
                    await self._page.goto(f"{self.base_url}/", wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                logger.warning(f"Initial navigation warning: {e}")

            return self._page

    async def _on_browser_stream_chunk(self, source: Any, stream_id: str, event_type: str, data: Any) -> None:
        """Callback receiving streaming events from browser JavaScript."""
        if stream_id in self._stream_queues:
            await self._stream_queues[stream_id].put({"event_type": event_type, "data": data})

    async def _execute_fetch_json(
        self,
        endpoint: str,
        method: str = "GET",
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Execute window.fetch within the browser context and return parsed JSON."""
        page = await self._ensure_browser()
        url = endpoint if endpoint.startswith("http") else f"{self.base_url}{endpoint}"

        session = self.session_repo.get_default()
        custom_headers = dict(headers or {})
        if session and session.credentials:
            if "Authorization" not in custom_headers and session.credentials.access_token:
                custom_headers["Authorization"] = f"Bearer {session.credentials.access_token}"
            if "oai-device-id" not in custom_headers and session.credentials.device_id:
                custom_headers["oai-device-id"] = session.credentials.device_id

        script = """
        async ({url, method, body, customHeaders}) => {
            const opts = {
                method: method,
                headers: {
                    'Accept': 'application/json',
                    'Content-Type': 'application/json',
                    ...customHeaders
                },
                credentials: 'include'
            };
            if (body && (method === 'POST' || method === 'PUT' || method === 'PATCH')) {
                opts.body = JSON.stringify(body);
            }
            try {
                const res = await window.fetch(url, opts);
                const text = await res.text();
                return {
                    status: res.status,
                    ok: res.ok,
                    text: text
                };
            } catch (err) {
                return {
                    status: 0,
                    ok: false,
                    error: err.toString()
                };
            }
        }
        """

        args = {
            "url": url,
            "method": method,
            "body": body,
            "customHeaders": custom_headers,
        }

        result = await page.evaluate(script, args)

        status = result.get("status", 0)
        raw_text = result.get("text", "")
        if not result.get("ok"):
            if status == 401:
                raise SessionExpiredError(f"401 Unauthorized from browser fetch: {raw_text[:200]}", status_code=401, mode=self.mode)
            elif status == 403:
                raise CloudflareChallengeError(f"403 Forbidden / Cloudflare challenge in browser: {raw_text[:200]}", status_code=403, mode=self.mode)
            elif status == 429:
                raise RateLimitError(f"429 Rate limit in browser: {raw_text[:200]}", status_code=429, mode=self.mode)
            raise TransportError(f"Browser fetch failed (status {status}): {result.get('error') or raw_text[:200]}", status_code=status, mode=self.mode)

        try:
            return json.loads(raw_text)
        except json.JSONDecodeError:
            return raw_text

    async def check_session(self) -> SessionStatusResponse:
        """Check session via browser fetch to /api/auth/session."""
        try:
            data = await self._execute_fetch_json("/api/auth/session")
            if isinstance(data, dict) and data.get("user"):
                return SessionStatusResponse(
                    status=SessionStatus.VALID,
                    message="Session valid via BROWSER_FETCH.",
                    user_email=data.get("user", {}).get("email"),
                    expires_at=data.get("expires"),
                )
            return SessionStatusResponse(
                status=SessionStatus.EXPIRED,
                message="No authenticated user in session.",
            )
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"BROWSER_FETCH check_session failed: {e}", mode=self.mode) from e

    async def fetch_projects(self, cursor: Optional[str] = None) -> List[Project]:
        """Fetch projects via browser fetch to /backend-api/gizmos/snorlax/sidebar."""
        try:
            data = await self._execute_fetch_json("/backend-api/gizmos/snorlax/sidebar")
            projects: List[Project] = []
            items = data.get("items", []) or data.get("gizmos", [])
            for item in items:
                gizmo = item.get("gizmo", item)
                g_id = gizmo.get("id") or gizmo.get("gizmo_id")
                if not g_id:
                    continue
                display = gizmo.get("display", {})
                projects.append(
                    Project(
                        id=g_id,
                        title=display.get("name") or gizmo.get("name") or "Untitled Project",
                        description=display.get("description") or gizmo.get("description"),
                        instructions=gizmo.get("instructions"),
                        gizmo_id=g_id,
                        workspace_id=gizmo.get("workspace_id"),
                    )
                )
            return projects
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"BROWSER_FETCH fetch_projects failed: {e}", mode=self.mode) from e

    async def fetch_project_detail(self, project_id: str) -> Optional[Project]:
        """Fetch project details via browser fetch to /backend-api/gizmos/{id}."""
        try:
            data = await self._execute_fetch_json(f"/backend-api/gizmos/{project_id}")
            gizmo = data.get("gizmo", data)
            display = gizmo.get("display", {})

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
            raise TransportError(f"BROWSER_FETCH fetch_project_detail failed: {e}", mode=self.mode) from e

    async def fetch_conversations(
        self,
        project_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 28,
    ) -> Dict[str, Any]:
        """Fetch paginated conversations via browser fetch."""
        endpoint = f"/backend-api/conversations?offset={offset}&limit={limit}&order=updated"
        if project_id:
            endpoint += f"&gizmo_id={project_id}"

        try:
            data = await self._execute_fetch_json(endpoint)
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
            raise TransportError(f"BROWSER_FETCH fetch_conversations failed: {e}", mode=self.mode) from e

    async def fetch_conversation_tree(self, conversation_id: str) -> Dict[str, Any]:
        """Fetch conversation DAG tree via browser fetch."""
        try:
            return await self._execute_fetch_json(f"/backend-api/conversation/{conversation_id}")
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"BROWSER_FETCH fetch_conversation_tree failed: {e}", mode=self.mode) from e

    async def send_message_stream(
        self,
        request: SendMessageRequest,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Stream assistant response chunks via browser fetch ReadableStream reader."""
        page = await self._ensure_browser()
        stream_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        self._stream_queues[stream_id] = queue

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
        payload["parent_message_id"] = request.parent_message_id or str(uuid.uuid4())

        if request.project_id:
            payload["conversation_mode"] = {
                "kind": "gizmo_interaction",
                "gizmo_id": request.project_id,
            }
        else:
            payload["conversation_mode"] = {"kind": "primary_assistant"}

        session = self.session_repo.get_default()
        custom_headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        if session and session.credentials:
            if session.credentials.access_token:
                custom_headers["Authorization"] = f"Bearer {session.credentials.access_token}"
            if session.credentials.device_id:
                custom_headers["oai-device-id"] = session.credentials.device_id

        # Fetch sentinel chat-requirements token if possible
        try:
            sentinel_data = await self._execute_fetch_json(
                "/backend-api/sentinel/chat-requirements",
                method="POST",
                body={},
                headers={"Content-Type": "application/json"},
            )
            if isinstance(sentinel_data, dict) and sentinel_data.get("token"):
                custom_headers["openai-sentinel-chat-requirements-token"] = sentinel_data["token"]
        except Exception as e:
            logger.debug(f"Could not retrieve sentinel token for stream: {e}")

        stream_script = """
        async ({url, payload, streamId, customHeaders}) => {
            try {
                const res = await window.fetch(url, {
                    method: 'POST',
                    headers: {
                        'Accept': 'text/event-stream',
                        'Content-Type': 'application/json',
                        ...customHeaders
                    },
                    body: JSON.stringify(payload),
                    credentials: 'include'
                });

                if (!res.ok) {
                    const text = await res.text();
                    await window.__antigravity_stream_chunk(streamId, 'error', {status: res.status, text: text});
                    return;
                }

                const reader = res.body.getReader();
                const decoder = new TextDecoder();
                let buffer = '';

                while (true) {
                    const {done, value} = await reader.read();
                    if (done) break;
                    buffer += decoder.decode(value, {stream: true});
                    const lines = buffer.split('\\n');
                    buffer = lines.pop(); // keep partial trailing line

                    for (const line of lines) {
                        const trimmed = line.trim();
                        if (!trimmed) continue;
                        if (trimmed === 'data: [DONE]') {
                            await window.__antigravity_stream_chunk(streamId, 'done', {});
                            return;
                        }
                        if (trimmed.startsWith('data: ')) {
                            try {
                                const json = JSON.parse(trimmed.slice(6));
                                await window.__antigravity_stream_chunk(streamId, 'chunk', json);
                            } catch (e) {
                                // ignore parse error on partial SSE
                            }
                        }
                    }
                }
                await window.__antigravity_stream_chunk(streamId, 'done', {});
            } catch (err) {
                await window.__antigravity_stream_chunk(streamId, 'error', {error: err.toString()});
            }
        }
        """

        # Launch the stream asynchronously in browser page
        asyncio.create_task(
            page.evaluate(
                stream_script,
                {
                    "url": f"{self.base_url}/backend-api/conversation",
                    "payload": payload,
                    "streamId": stream_id,
                    "customHeaders": custom_headers,
                },
            )
        )

        accumulated_text = ""
        assistant_message_id: Optional[str] = None
        conv_id = request.conversation_id

        try:
            while True:
                item = await asyncio.wait_for(queue.get(), timeout=60.0)
                event_type = item.get("event_type")
                data = item.get("data", {})

                if event_type == "error":
                    status = data.get("status", 0)
                    text = data.get("text", "") or data.get("error", "")
                    if status == 401:
                        raise SessionExpiredError(f"401 Unauthorized on stream: {text}", status_code=401, mode=self.mode)
                    elif status == 403:
                        raise CloudflareChallengeError(f"403 Forbidden on stream: {text}", status_code=403, mode=self.mode)
                    raise TransportError(f"Browser stream error: {text}", status_code=status, mode=self.mode)

                elif event_type == "chunk":
                    if "conversation_id" in data and not conv_id:
                        conv_id = data["conversation_id"]

                    msg = data.get("message")
                    if msg:
                        assistant_message_id = msg.get("id") or assistant_message_id
                        role = msg.get("author", {}).get("role")
                        if role == "assistant":
                            parts = msg.get("content", {}).get("parts", [])
                            if parts:
                                current_full_text = "".join(parts)
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

                elif event_type == "done":
                    yield {
                        "event": "done",
                        "text": accumulated_text,
                        "message_id": assistant_message_id,
                        "parent_id": user_message_id,
                        "conversation_id": conv_id,
                    }
                    break

        finally:
            self._stream_queues.pop(stream_id, None)

    async def close(self) -> None:
        """Close browser context and playwright cleanly."""
        async with self._lock:
            if self._context:
                try:
                    await self._context.close()
                except Exception:
                    pass
                self._context = None
                self._page = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

    async def get_interpreter_download_url(
        self,
        conversation_id: str,
        message_id: str,
        sandbox_path: str,
    ) -> str:
        import urllib.parse
        qs = urllib.parse.urlencode({"message_id": message_id, "sandbox_path": sandbox_path})
        endpoint = f"/backend-api/conversation/{conversation_id}/interpreter/download?{qs}"
        data = await self._execute_fetch_json(endpoint, method="GET")
        if isinstance(data, dict) and data.get("download_url"):
            return data["download_url"]
        raise TransportError(f"No download_url in browser fetch response: {data}", mode=self.mode)

    async def download_file_bytes(self, url: str) -> bytes:
        import base64
        page = await self._ensure_browser()
        script = """
        async (targetUrl) => {
            const res = await window.fetch(targetUrl, { credentials: 'include' });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const blob = await res.blob();
            return new Promise((resolve, reject) => {
                const reader = new FileReader();
                reader.onloadend = () => resolve(reader.result.split(',')[1]);
                reader.onerror = reject;
                reader.readAsDataURL(blob);
            });
        }
        """
        b64 = await page.evaluate(script, url)
        return base64.b64decode(b64)
