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
        self.user_data_dir = user_data_dir or (settings.full_session_data_dir / "chromium")
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
        """Ensure Playwright browser is ready."""
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
            ]

            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                headless=self.headless,
                args=args,
                viewport={"width": 1280, "height": 800},
            )

            pages = self._context.pages
            self._page = pages[0] if pages else await self._context.new_page()
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
            res = await page.evaluate("""
                async () => {
                    const r = await fetch('/backend-api/gizmos/snorlax/sidebar');
                    return r.ok ? await r.json() : null;
                }
            """)
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
        try:
            data = await page.evaluate(f"""
                async () => {{
                    const r = await fetch('/backend-api/gizmos/{project_id}');
                    return r.ok ? await r.json() : null;
                }}
            """)
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

        try:
            data = await page.evaluate(f"""
                async () => {{
                    const r = await fetch('{query}');
                    return r.ok ? await r.json() : null;
                }}
            """)
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
        try:
            data = await page.evaluate(f"""
                async () => {{
                    const r = await fetch('/backend-api/conversation/{conversation_id}');
                    return r.ok ? await r.json() : null;
                }}
            """)
            if data and isinstance(data, dict):
                return data
            raise TransportError("Empty response from conversation tree evaluation", mode=self.mode)
        except Exception as e:
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
        target_url = f"{self.base_url}/c/{request.conversation_id}" if request.conversation_id else f"{self.base_url}/"

        try:
            if page.url != target_url:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=20000)

            # Locate textarea
            textarea = await page.wait_for_selector(
                "#prompt-textarea, textarea, div[contenteditable='true']",
                state="visible",
                timeout=10000,
            )
            if not textarea:
                raise TransportError("Textarea input not found in UI", mode=self.mode)

            # Fill message
            await textarea.fill(request.content)
            await asyncio.sleep(0.3)

            # Click send button
            send_btn = await page.query_selector(
                'button[data-testid="send-button"], button[aria-label="Send prompt"]'
            )
            if send_btn:
                await send_btn.click()
            else:
                await textarea.press("Enter")

            # Poll for assistant response turn
            accumulated_text = ""
            assistant_msg_id = str(uuid.uuid4())
            conv_id = request.conversation_id

            # Wait for assistant response generation to start and complete
            for _ in range(60):
                await asyncio.sleep(0.5)
                turn_text = await page.evaluate("""
                    () => {
                        const turns = document.querySelectorAll('[data-message-author-role="assistant"], .markdown');
                        if (turns.length === 0) return '';
                        const lastTurn = turns[turns.length - 1];
                        return lastTurn.innerText || '';
                    }
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
                stop_btn = await page.query_selector('button[data-testid="stop-button"]')
                if accumulated_text and not stop_btn:
                    break

            yield {
                "event": "done",
                "text": accumulated_text,
                "message_id": assistant_msg_id,
                "parent_id": request.parent_message_id,
                "conversation_id": conv_id,
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
