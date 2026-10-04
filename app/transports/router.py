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
    ConversationNotFoundError,
    SessionExpiredError,
    TransportError,
)
from app.transports.browser_fetch import BrowserFetchTransport
from app.transports.direct_http import DirectHttpTransport
from app.transports.ui_driven import UiDrivenTransport

logger = logging.getLogger(__name__)

_shared_router_instance: Optional["TransportRouter"] = None


def get_shared_router() -> "TransportRouter":
    """Return the global shared TransportRouter instance."""
    global _shared_router_instance
    if _shared_router_instance is None:
        _shared_router_instance = TransportRouter()
    return _shared_router_instance


def set_shared_router(router: Optional["TransportRouter"]) -> None:
    """Set the global shared TransportRouter instance (useful for test fixtures)."""
    global _shared_router_instance
    _shared_router_instance = router


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
        self._submission_locks: Dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

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
        if self.forced_mode:
            modes_to_try = [self.forced_mode]
        elif self.last_successful_mode:
            modes_to_try = [self.last_successful_mode] + [m for m in self.fallback_chain if m != self.last_successful_mode]
        else:
            modes_to_try = list(self.fallback_chain)

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

            except ConversationNotFoundError as e:
                latency_ms = (time.perf_counter() - start_time) * 1000
                self.consecutive_failures[mode.value] += 1
                self.log_repo.log_operation(
                    TransportAuditRecord(
                        operation=operation_name,
                        attempted_mode=attempted_mode,
                        resolved_mode=mode,
                        target_id=target_id,
                        status_code=404,
                        latency_ms=latency_ms,
                        success=False,
                        error_message=str(e),
                        fallback_triggered=fallback_triggered,
                    )
                )
                # Conversation is deleted or not found upstream; do not cycle fallbacks
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

    async def _get_submission_lock(self, key: str) -> asyncio.Lock:
        async with self._locks_guard:
            if key not in self._submission_locks:
                self._submission_locks[key] = asyncio.Lock()
            return self._submission_locks[key]

    async def send_message_stream(
        self,
        request: SendMessageRequest,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Executes streaming message transmission with anti-duplicate guarantees (R3, A8).
        Disallows re-submission if a message was potentially accepted upstream.
        Reconciles upstream persistence or returns an uncertain delivery state.
        """
        target_id = request.conversation_id or "new_chat"
        lock = await self._get_submission_lock(target_id)

        async with lock:
            if self.forced_mode:
                modes_to_try = [self.forced_mode]
            elif self.last_successful_mode:
                modes_to_try = [self.last_successful_mode] + [m for m in self.fallback_chain if m != self.last_successful_mode]
            else:
                modes_to_try = [TransportMode.UI_DRIVEN, TransportMode.BROWSER_FETCH, TransportMode.DIRECT_HTTP]

            last_exception: Optional[Exception] = None
            submission_potentially_accepted = False

            for idx, mode in enumerate(modes_to_try):
                transport = self.transports[mode]
                start_time = time.perf_counter()
                fallback_triggered = idx > 0

                try:
                    stream_iter = transport.send_message_stream(request)
                    first_chunk = None

                    try:
                        first_chunk = await stream_iter.__anext__()
                    except StopAsyncIteration:
                        pass

                    # If first_chunk was received or transport yielded chunks, submission was accepted upstream
                    if first_chunk:
                        submission_potentially_accepted = True

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

                    try:
                        async for chunk in stream_iter:
                            if not submission_potentially_accepted:
                                submission_potentially_accepted = True
                            yield chunk
                    except Exception as stream_err:
                        logger.warning(f"Stream interrupted on {mode.value} after initial connection: {stream_err}")
                        submission_potentially_accepted = True
                        raise stream_err

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

                    # CRITICAL: If the message was potentially accepted upstream, FORBID any fallback resubmission
                    if submission_potentially_accepted:
                        logger.warning(
                            f"Submission was potentially accepted on {mode.value} before failure. "
                            f"Disallowing failover to prevent duplicate message. Attempting upstream reconciliation..."
                        )
                        # Reconcile upstream persistence
                        conv_id = request.conversation_id
                        reconciled = False
                        if conv_id:
                            try:
                                tree = await self.fetch_conversation_tree(conv_id)
                                if tree and isinstance(tree, dict) and tree.get("current_node"):
                                    curr = tree["current_node"]
                                    node = tree.get("mapping", {}).get(curr, {})
                                    msg = node.get("message")
                                    if msg:
                                        role = msg.get("author", {}).get("role")
                                        parts = msg.get("content", {}).get("parts", [])
                                        text = "".join(parts) if parts else ""
                                        if role == "assistant" and text:
                                            yield {
                                                "event": "done",
                                                "text": text,
                                                "message_id": curr,
                                                "parent_id": node.get("parent"),
                                                "conversation_id": conv_id,
                                                "reconciled": True,
                                            }
                                            reconciled = True
                                            return
                            except Exception as rec_err:
                                logger.warning(f"Upstream reconciliation attempt failed: {rec_err}")

                        # If not reconciled, return uncertain delivery state
                        yield {
                            "event": "uncertain",
                            "status": "uncertain",
                            "text": "",
                            "error": f"Message may have been accepted upstream via {mode.value} but streaming was interrupted ({e}). Upstream state unconfirmed; resubmission aborted to prevent duplication.",
                            "conversation_id": conv_id,
                            "parent_id": request.parent_message_id,
                            "attempted_mode": mode.value,
                        }
                        return

            if last_exception:
                raise last_exception
            raise TransportError("All transports failed to stream message")

    async def get_interpreter_download_url(
        self,
        conversation_id: str,
        message_id: str,
        sandbox_path: str,
    ) -> str:
        async def _call(transport):
            return await transport.get_interpreter_download_url(
                conversation_id=conversation_id,
                message_id=message_id,
                sandbox_path=sandbox_path,
            )

        return await self._execute_with_failover(
            "get_interpreter_download_url",
            conversation_id,
            _call,
        )

    async def download_file_bytes(self, url: str) -> bytes:
        async def _call(transport):
            return await transport.download_file_bytes(url)

        return await self._execute_with_failover(
            "download_file_bytes",
            url[:50],
            _call,
        )

    async def close(self) -> None:
        """Close browser resources in transports."""
        for t in self.transports.values():
            if hasattr(t, "close"):
                try:
                    await t.close()
                except Exception:
                    pass
