"""
Mode 3: UI_DRIVEN transport using DOM automation via Playwright (R4).
Explicit fallback transport when direct API endpoints are inaccessible.
"""

import asyncio
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
    ConversationNotFoundError,
    RateLimitError,
    SessionExpiredError,
    TransportError,
)

logger = logging.getLogger(__name__)


class UiDrivenTransport(ChatGPTTransport):
    """
    DOM-level automation fallback using Playwright.
    Controls the official web interface elements (#prompt-textarea, send button, response turns).
    """

    def __init__(
        self,
        session_repo: Optional[SessionRepo] = None,
        user_data_dir: Optional[Path] = None,
        base_url: Optional[str] = None,
        headless: Optional[bool] = None,
    ):
        self.session_repo = session_repo or SessionRepo()
        self.user_data_dir = user_data_dir or (settings.full_session_data_dir / "chromium_ui")
        self.base_url = (base_url or settings.UPSTREAM_BASE_URL).rstrip("/")
        self.headless = settings.BROWSER_HEADLESS if headless is None else headless

        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> TransportMode:
        return TransportMode.UI_DRIVEN

    async def _ensure_browser(self) -> Page:
        """Ensure Playwright browser is ready with active cookies and native headless flags."""
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
                headless=self.headless,
                args=args,
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            )

            # Inject cookies from repo with RFC 6265bis compliance for __Host- cookies
            session = self.session_repo.get_default()
            if session and session.credentials:
                for name, val in session.credentials.cookies.items():
                    if name.startswith("__Host-"):
                        c = {
                            "name": name,
                            "value": val,
                            "url": "https://chatgpt.com",
                        }
                    else:
                        c = {
                            "name": name,
                            "value": val,
                            "domain": ".chatgpt.com",
                            "path": "/",
                            "secure": True,
                        }
                    try:
                        await self._context.add_cookies([c])
                    except Exception as e:
                        logger.debug(f"UI transport cookie add error for {name}: {e}")

            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()

            # Ensure page is on base domain before executing DOM fetch calls
            try:
                current_url = getattr(self._page, "url", "")
                if not current_url or not current_url.startswith(self.base_url):
                    await self._page.goto(f"{self.base_url}/", wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                logger.warning(f"UI transport initial navigation warning: {e}")

            return self._page

    async def check_session(self) -> SessionStatusResponse:
        """Check session by verifying login status on chatgpt.com."""
        page = await self._ensure_browser()
        try:
            await page.goto(f"{self.base_url}/", wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1.0)

            # Check if login buttons are present
            login_btn = await page.query_selector('button[data-testid="login-button"], a[href="/auth/login"]')
            if login_btn:
                return SessionStatusResponse(
                    status=SessionStatus.EXPIRED,
                    message="Login button visible in UI: session expired or missing.",
                )

            # Check for user avatar / profile menu
            user_menu = await page.query_selector('button[data-testid="profile-button"], [data-testid="account-menu-button"]')
            is_valid = bool(user_menu)

            return SessionStatusResponse(
                status=SessionStatus.VALID if is_valid else SessionStatus.CHALLENGE_REQUIRED,
                message="UI shows active user profile." if is_valid else "Profile button not found.",
            )
        except Exception as e:
            raise TransportError(f"UI_DRIVEN check_session failed: {e}", mode=self.mode) from e

    async def fetch_projects(self, cursor: Optional[str] = None) -> List[Project]:
        """Scrape projects from the sidebar DOM or fallback to DOM-evaluated API."""
        page = await self._ensure_browser()
        try:
            # First try evaluating in DOM context for fidelity
            target_url = f"{self.base_url}/backend-api/gizmos/snorlax/sidebar"
            res = await page.evaluate("""
                async (url) => {
                    try {
                        const r = await fetch(url, { credentials: 'include' });
                        return r.ok ? await r.json() : null;
                    } catch (e) {
                        return null;
                    }
                }
            """, target_url)
            if res and isinstance(res, dict):
                items = res.get("items", []) or res.get("gizmos", [])
                projects = []
                for item in items:
                    gizmo = item.get("gizmo", item)
                    g_id = gizmo.get("id") or gizmo.get("gizmo_id")
                    if not g_id:
                        continue
                    disp = gizmo.get("display", {})
                    projects.append(
                        Project(
                            id=g_id,
                            title=disp.get("name") or gizmo.get("name") or "Untitled Project",
                            description=disp.get("description"),
                            instructions=gizmo.get("instructions"),
                            gizmo_id=g_id,
                        )
                    )
                return projects

            # Fallback to scraping sidebar links
            links = await page.query_selector_all('a[href*="/g/p-"]')
            projects = []
            for link in links:
                href = await link.get_attribute("href") or ""
                title = await link.inner_text()
                p_id = href.split("/g/")[-1].split("/")[0] if "/g/" in href else str(uuid.uuid4())
                projects.append(Project(id=p_id, title=title.strip() or p_id, gizmo_id=p_id))
            return projects
        except Exception as e:
            raise TransportError(f"UI_DRIVEN fetch_projects failed: {e}", mode=self.mode) from e

    async def fetch_project_detail(self, project_id: str) -> Optional[Project]:
        """Fetch project details via UI page evaluation."""
        page = await self._ensure_browser()
        target_url = f"{self.base_url}/backend-api/gizmos/{project_id}"
        try:
            data = await page.evaluate("""
                async (url) => {
                    try {
                        const r = await fetch(url, { credentials: 'include' });
                        return r.ok ? await r.json() : null;
                    } catch (e) {
                        return null;
                    }
                }
            """, target_url)
            if data:
                gizmo = data.get("gizmo", data)
                disp = gizmo.get("display", {})
                return Project(
                    id=project_id,
                    title=disp.get("name") or gizmo.get("name") or project_id,
                    description=disp.get("description"),
                    instructions=gizmo.get("instructions"),
                    gizmo_id=project_id,
                )
            return Project(id=project_id, title=project_id, gizmo_id=project_id)
        except Exception as e:
            raise TransportError(f"UI_DRIVEN fetch_project_detail failed: {e}", mode=self.mode) from e

    async def fetch_conversations(
        self,
        project_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 28,
    ) -> Dict[str, Any]:
        """Fetch conversations using UI DOM evaluation."""
        page = await self._ensure_browser()
        query = f"/backend-api/conversations?offset={offset}&limit={limit}&order=updated"
        if project_id:
            query += f"&gizmo_id={project_id}"
        target_url = f"{self.base_url}{query}"

        try:
            data = await page.evaluate("""
                async (url) => {
                    try {
                        const r = await fetch(url, { credentials: 'include' });
                        return r.ok ? await r.json() : null;
                    } catch (e) {
                        return null;
                    }
                }
            """, target_url)
            if data and isinstance(data, dict):
                items = data.get("items", [])
                total = data.get("total", len(items))
                return {
                    "items": items,
                    "total": total,
                    "offset": offset,
                    "limit": limit,
                    "has_more": offset + len(items) < total,
                }
            return {"items": [], "total": 0, "offset": offset, "limit": limit, "has_more": False}
        except Exception as e:
            raise TransportError(f"UI_DRIVEN fetch_conversations failed: {e}", mode=self.mode) from e

    async def fetch_conversation_tree(self, conversation_id: str) -> Dict[str, Any]:
        """Fetch conversation DAG tree via UI evaluation."""
        page = await self._ensure_browser()
        target_url = f"{self.base_url}/backend-api/conversation/{conversation_id}"
        script = """
        async (url) => {
            try {
                const r = await fetch(url, { credentials: 'include' });
                const text = await r.text();
                return {
                    status: r.status,
                    ok: r.ok,
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
        try:
            result = await page.evaluate(script, target_url)
            if not isinstance(result, dict):
                raise TransportError("Empty response from conversation tree evaluation", mode=self.mode)
            status = result.get("status", 0)
            raw_text = result.get("text", "")
            if not result.get("ok"):
                if status == 404:
                    raise ConversationNotFoundError(f"404 Not Found from UI evaluation: {raw_text[:200]}", status_code=404, mode=self.mode)
                elif status == 401:
                    raise SessionExpiredError(f"401 Unauthorized from UI evaluation: {raw_text[:200]}", status_code=401, mode=self.mode)
                raise TransportError(f"UI evaluation failed (status {status}): {result.get('error') or raw_text[:200]}", status_code=status, mode=self.mode)

            import json
            try:
                data = json.loads(raw_text)
            except Exception:
                data = raw_text

            if data and isinstance(data, dict):
                return data
            raise TransportError("Empty response from conversation tree evaluation", mode=self.mode)
        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"UI_DRIVEN fetch_conversation_tree failed: {e}", mode=self.mode) from e

    async def send_message_stream(
        self,
        request: SendMessageRequest,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Send message using DOM elements and stream response chunks by observing
        the assistant turn container.
        """
        page = await self._ensure_browser()
        if request.conversation_id:
            target_url = f"{self.base_url}/c/{request.conversation_id}"
        elif request.project_id:
            target_url = f"{self.base_url}/g/{request.project_id}"
        else:
            target_url = f"{self.base_url}/"

        try:
            if not page.url.startswith(target_url):
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)

            # Locate input element (new chat home or existing conversation)
            input_selector = (
                "#pending-conversation-input, #pending-home-input, "
                ".ProseMirror[contenteditable='true'], div[contenteditable='true'], "
                "#prompt-textarea, textarea"
            )
            input_el = await page.wait_for_selector(input_selector, state="visible", timeout=15000)
            if not input_el:
                raise TransportError("Chat input element not found in UI", mode=self.mode)

            # Focus and insert message using fast input simulation to trigger React/ProseMirror state
            await input_el.click()
            await asyncio.sleep(0.2)
            try:
                await page.keyboard.insert_text(request.content)
            except Exception:
                await page.keyboard.type(request.content, delay=0)
            await asyncio.sleep(0.3)

            # Count existing turns to isolate newly streamed response
            initial_turns_count = await page.evaluate("""
                () => document.querySelectorAll('[class*="MarkdownRoot"], .markdown, [data-message-author-role="assistant"]').length
            """)

            # Click send button
            send_btn = await page.query_selector(
                'button[aria-label="Send"], button[aria-label="Send prompt"], button[data-testid="send-button"]'
            )
            if send_btn and not await send_btn.is_disabled():
                await send_btn.click()
            else:
                await page.keyboard.press("Enter")

            # Poll for assistant response turn
            accumulated_text = ""
            assistant_msg_id = str(uuid.uuid4())
            conv_id = request.conversation_id
            generation_completed = False

            # Wait for assistant response generation to start and complete (up to 60s)
            for _ in range(120):
                await asyncio.sleep(0.5)

                # Extract or update conversation ID from URL if was new chat or redirected
                if "/c/" in page.url:
                    raw_id = page.url.split("/c/")[1].split("?")[0].split("#")[0]
                    if raw_id and not raw_id.startswith("local-chatgpt") and len(raw_id) > 10:
                        conv_id = raw_id

                turn_text = await page.evaluate(f"""
                    () => {{
                        const turns = document.querySelectorAll('[class*="MarkdownRoot"], .markdown, [data-message-author-role="assistant"]');
                        if (turns.length <= {initial_turns_count}) return '';
                        const lastTurn = turns[turns.length - 1];
                        return lastTurn.innerText || '';
                    }}
                """)

                if turn_text and len(turn_text) > len(accumulated_text):
                    delta = turn_text[len(accumulated_text):]
                    accumulated_text = turn_text
                    yield {
                        "event": "delta",
                        "delta": delta,
                        "text": accumulated_text,
                        "message_id": assistant_msg_id,
                        "parent_id": request.parent_message_id,
                        "conversation_id": conv_id,
                    }

                # Check if stop generating button disappeared (turn finished)
                stop_btn = await page.query_selector(
                    'button[aria-label*="Stop"], button[data-testid*="stop"]'
                )
                if accumulated_text and not stop_btn:
                    await asyncio.sleep(0.5)
                    generation_completed = True
                    break

            # 1. Condition: generation must be completed (no timeout)
            if not generation_completed:
                raise TransportError(
                    "UI_DRIVEN send_message_stream failed: generation timed out or was interrupted before completion",
                    mode=self.mode,
                )

            # 2. Condition: response text must not be empty
            if not accumulated_text or not accumulated_text.strip():
                raise TransportError(
                    "UI_DRIVEN send_message_stream failed: upstream assistant returned an empty response",
                    mode=self.mode,
                )

            # 3. Condition: conversation ID must be confirmed
            if not conv_id:
                raise TransportError(
                    "UI_DRIVEN send_message_stream failed: unconfirmed conversation ID",
                    mode=self.mode,
                )

            # 4. Condition: official message IDs must be confirmed from upstream tree
            canonical_assistant_id: Optional[str] = None
            canonical_user_id: Optional[str] = None
            canonical_parent_id: Optional[str] = request.parent_message_id

            try:
                session = self.session_repo.get_default()
                token = session.credentials.access_token if session and session.credentials else ""
                tree = await page.evaluate("""
                    async ({url, token}) => {
                        const headers = token ? {'Authorization': 'Bearer ' + token} : {};
                        try {
                            const r = await fetch(url, {headers, credentials: 'include'});
                            return r.ok ? await r.json() : null;
                        } catch (e) {
                            return null;
                        }
                    }
                """, {"url": f"{self.base_url}/backend-api/conversation/{conv_id}", "token": token})

                if tree and isinstance(tree, dict) and tree.get("current_node"):
                    canonical_assistant_id = tree["current_node"]
                    node = tree.get("mapping", {}).get(canonical_assistant_id, {})
                    user_node_id = node.get("parent")
                    if user_node_id:
                        canonical_user_id = user_node_id
                        user_node = tree.get("mapping", {}).get(user_node_id, {})
                        if user_node.get("parent"):
                            canonical_parent_id = user_node["parent"]
            except Exception as e:
                logger.warning(f"Error resolving canonical IDs from conversation tree: {e}")

            if not canonical_assistant_id or not canonical_user_id:
                raise TransportError(
                    f"UI_DRIVEN send_message_stream failed: canonical message IDs unconfirmed for conversation {conv_id}",
                    mode=self.mode,
                )

            yield {
                "event": "done",
                "text": accumulated_text,
                "message_id": canonical_assistant_id,
                "user_message_id": canonical_user_id,
                "parent_id": canonical_parent_id,
                "conversation_id": conv_id,
                "confirmed": True,
            }

        except Exception as e:
            if isinstance(e, TransportError):
                raise
            raise TransportError(f"UI_DRIVEN send_message_stream failed: {e}", mode=self.mode) from e

    async def close(self) -> None:
        """Close browser context."""
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
        page = await self._ensure_browser()
        import urllib.parse
        qs = urllib.parse.urlencode({"message_id": message_id, "sandbox_path": sandbox_path})
        url = f"{self.base_url}/backend-api/conversation/{conversation_id}/interpreter/download?{qs}"
        script = """
        async (targetUrl) => {
            const res = await window.fetch(targetUrl, { credentials: 'include' });
            if (!res.ok) throw new Error("HTTP " + res.status);
            return await res.json();
        }
        """
        data = await page.evaluate(script, url)
        if isinstance(data, dict) and data.get("download_url"):
            return data["download_url"]
        raise TransportError(f"UI_DRIVEN get_interpreter_download_url failed: {data}", mode=self.mode)

    async def download_file_bytes(self, url: str) -> bytes:
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
        import base64
        b64 = await page.evaluate(script, url)
        return base64.b64decode(b64)
