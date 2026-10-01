"""
TransportRouter: coordinates execution, fallback, forced modes, and telemetry logging (R4).
Prioritizes DIRECT_HTTP -> BROWSER_FETCH -> UI_DRIVEN.
"""

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Callable, Coroutine, Dict, List, Optional

from app.config import settings
from app.models.conversation import SendMessageRequest
from app.models.project import Project
from app.models.session import SessionStatusResponse
from app.models.transport import TransportAuditRecord, TransportMode, TransportStatusReport
from app.persistence.session_repo import SessionRepo
from app.persistence.transport_log_repo import TransportLogRepo
from app.transports.base import (
    ChatGPTTransport,
    CloudflareChallengeError,
    SessionExpiredError,
    TransportError,
)
from app.transports.browser_fetch import BrowserFetchTransport
from app.transports.direct_http import DirectHttpTransport
from app.transports.ui_driven import UiDrivenTransport

logger = logging.getLogger(__name__)


class TransportRouter:
    """
    Intelligent router dispatching requests across transport modes with telemetry audit.
    Order of preference: DIRECT_HTTP -> BROWSER_FETCH -> UI_DRIVEN (explicit fallback).
    """

    def __init__(
        self,
        session_repo: Optional[SessionRepo] = None,
        log_repo: Optional[TransportLogRepo] = None,
        forced_mode: Optional[TransportMode] = None,
        default_mode: Optional[TransportMode] = None,
    ):
        self.session_repo = session_repo or SessionRepo()
        self.log_repo = log_repo or TransportLogRepo()
        self.forced_mode = forced_mode

        # Initialize transport adapters
        self.transports: Dict[TransportMode, ChatGPTTransport] = {
            TransportMode.DIRECT_HTTP: DirectHttpTransport(session_repo=self.session_repo),
            TransportMode.BROWSER_FETCH: BrowserFetchTransport(session_repo=self.session_repo),
            TransportMode.UI_DRIVEN: UiDrivenTransport(session_repo=self.session_repo),
        }

        # Sequence of execution
        self.fallback_chain = [
            TransportMode.DIRECT_HTTP,
            TransportMode.BROWSER_FETCH,
            TransportMode.UI_DRIVEN,
        ]

        self.last_successful_mode: Optional[TransportMode] = None
        self.consecutive_failures: Dict[str, int] = {m.value: 0 for m in TransportMode}

    def set_forced_mode(self, mode: Optional[TransportMode]) -> None:
        """Lock router to a specific transport or None for auto-failover."""
        self.forced_mode = mode
        logger.info(f"Transport router forced_mode set to: {mode}")

    def get_status_report(self) -> TransportStatusReport:
        """Return diagnostic health report for UI and telemetry."""
        return TransportStatusReport(
            current_mode=self.forced_mode or self.last_successful_mode or TransportMode.DIRECT_HTTP,
            forced_mode=self.forced_mode,
            available_modes=list(self.transports.keys()),
            last_successful_mode=self.last_successful_mode,
            consecutive_failures=self.consecutive_failures,
            circuit_breaker_open=any(cnt >= 5 for cnt in self.consecutive_failures.values()),
        )

    async def _execute_with_failover(
        self,
        operation_name: str,
        target_id: Optional[str],
        fn: Callable[[ChatGPTTransport], Coroutine[Any, Any, Any]],
    ) -> Any:
        """Executes operation with automatic fallback through the transport chain."""
        modes_to_try = [self.forced_mode] if self.forced_mode else self.fallback_chain
        last_exception: Optional[Exception] = None
        fallback_triggered = False

        for idx, mode in enumerate(modes_to_try):
            transport = self.transports[mode]
            start_time = time.perf_counter()
            attempted_mode = mode
            if idx > 0:
                fallback_triggered = True

            try:
                result = await fn(transport)
                latency_ms = (time.perf_counter() - start_time) * 1000

                self.last_successful_mode = mode
                self.consecutive_failures[mode.value] = 0

                # Audit log
                self.log_repo.log_operation(
                    TransportAuditRecord(
                        operation=operation_name,
                        attempted_mode=modes_to_try[0],
                        resolved_mode=mode,
                        target_id=target_id,
                        status_code=200,
                        latency_ms=latency_ms,
                        success=True,
                        fallback_triggered=fallback_triggered,
                    )
                )
                return result

            except SessionExpiredError as e:
                latency_ms = (time.perf_counter() - start_time) * 1000
                self.consecutive_failures[mode.value] += 1
                self.log_repo.log_operation(
                    TransportAuditRecord(
                        operation=operation_name,
                        attempted_mode=attempted_mode,
                        resolved_mode=mode,
                        target_id=target_id,
                        status_code=401,
                        latency_ms=latency_ms,
                        success=False,
                        error_message=str(e),
                        fallback_triggered=fallback_triggered,
                    )
                )
                # Session expiration affects all transports; do not cycle fruitlessly
                raise

            except Exception as e:
                latency_ms = (time.perf_counter() - start_time) * 1000
                self.consecutive_failures[mode.value] += 1
                last_exception = e
                logger.warning(
                    f"Transport {mode.value} failed for {operation_name}: {e}. "
                    f"Proceeding to next fallback if available."
                )

                self.log_repo.log_operation(
                    TransportAuditRecord(
                        operation=operation_name,
                        attempted_mode=attempted_mode,
                        resolved_mode=mode,
                        target_id=target_id,
                        status_code=getattr(e, "status_code", None),
                        latency_ms=latency_ms,
                        success=False,
                        error_message=str(e),
                        fallback_triggered=fallback_triggered,
                    )
                )

        if last_exception:
            raise last_exception
        raise TransportError(f"All transport modes failed for {operation_name}")

    async def check_session(self) -> SessionStatusResponse:
        """Check session status across transports."""
        return await self._execute_with_failover(
            "check_session",
            None,
            lambda t: t.check_session(),
        )

    async def fetch_projects(self, cursor: Optional[str] = None) -> List[Project]:
        """Fetch projects with fallback."""
        return await self._execute_with_failover(
            "fetch_projects",
            cursor,
            lambda t: t.fetch_projects(cursor=cursor),
        )

    async def fetch_project_detail(self, project_id: str) -> Optional[Project]:
        """Fetch project detail with fallback."""
        return await self._execute_with_failover(
            "fetch_project_detail",
            project_id,
            lambda t: t.fetch_project_detail(project_id=project_id),
        )

    async def fetch_conversations(
        self,
        project_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 28,
    ) -> Dict[str, Any]:
        """Fetch conversations with fallback."""
        return await self._execute_with_failover(
            "fetch_conversations",
            project_id,
            lambda t: t.fetch_conversations(project_id=project_id, offset=offset, limit=limit),
        )

    async def fetch_conversation_tree(self, conversation_id: str) -> Dict[str, Any]:
        """Fetch conversation tree with fallback."""
        return await self._execute_with_failover(
            "fetch_conversation_tree",
            conversation_id,
            lambda t: t.fetch_conversation_tree(conversation_id=conversation_id),
        )

    async def send_message_stream(
        self,
        request: SendMessageRequest,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Executes streaming message transmission.
        Tries chosen or fallback transports until a stream successfully connects.
        """
        modes_to_try = [self.forced_mode] if self.forced_mode else self.fallback_chain
        target_id = request.conversation_id or "new_chat"
        last_exception: Optional[Exception] = None

        for idx, mode in enumerate(modes_to_try):
            transport = self.transports[mode]
            start_time = time.perf_counter()
            fallback_triggered = idx > 0

            try:
                # Test connection by attempting first chunk
                stream_iter = transport.send_message_stream(request)
                first_chunk = None

                try:
                    first_chunk = await stream_iter.__anext__()
                except StopAsyncIteration:
                    pass

                self.last_successful_mode = mode
                self.consecutive_failures[mode.value] = 0

                self.log_repo.log_operation(
                    TransportAuditRecord(
                        operation="send_message_stream",
                        attempted_mode=modes_to_try[0],
                        resolved_mode=mode,
                        target_id=target_id,
                        status_code=200,
                        latency_ms=(time.perf_counter() - start_time) * 1000,
                        success=True,
                        fallback_triggered=fallback_triggered,
                    )
                )

                if first_chunk:
                    yield first_chunk

                async for chunk in stream_iter:
                    yield chunk

                return

            except SessionExpiredError as e:
                self.log_repo.log_operation(
                    TransportAuditRecord(
                        operation="send_message_stream",
                        attempted_mode=mode,
                        resolved_mode=mode,
                        target_id=target_id,
                        status_code=401,
                        latency_ms=(time.perf_counter() - start_time) * 1000,
                        success=False,
                        error_message=str(e),
                        fallback_triggered=fallback_triggered,
                    )
                )
                raise

            except Exception as e:
                logger.warning(f"Streaming failed on mode {mode.value}: {e}")
                last_exception = e
                self.consecutive_failures[mode.value] += 1
                self.log_repo.log_operation(
                    TransportAuditRecord(
                        operation="send_message_stream",
                        attempted_mode=mode,
                        resolved_mode=mode,
                        target_id=target_id,
                        status_code=getattr(e, "status_code", None),
                        latency_ms=(time.perf_counter() - start_time) * 1000,
                        success=False,
                        error_message=str(e),
                        fallback_triggered=fallback_triggered,
                    )
                )

        if last_exception:
            raise last_exception
        raise TransportError("All transports failed to stream message")

    async def close(self) -> None:
        """Close browser resources in transports."""
        for t in self.transports.values():
            if hasattr(t, "close"):
                try:
                    await t.close()
                except Exception:
                    pass
