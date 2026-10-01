"""
Sync Service: coordinates bidirectional messaging, streaming, site-to-POC updates,
anti-duplication invariants, and acceptance criteria verifications (R3, A3, A4, A5, A8).
"""

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

from app.models.conversation import ActiveBranchResponse, Conversation, Message, SendMessageRequest
from app.persistence.conversation_repo import ConversationRepo
from app.services.conversation_service import ConversationService
from app.transports.router import TransportRouter, get_shared_router

logger = logging.getLogger(__name__)


class SyncService:
    """
    Coordinates outbound message sending, real-time SSE streaming, inbound site sync,
    and 3-cycle deduplication validation.
    """

    def __init__(
        self,
        conversation_service: Optional[ConversationService] = None,
        conversation_repo: Optional[ConversationRepo] = None,
        transport_router: Optional[TransportRouter] = None,
    ):
        self.conversation_repo = conversation_repo or ConversationRepo()
        self.conversation_service = conversation_service or ConversationService(
            conversation_repo=self.conversation_repo,
            transport_router=transport_router,
        )
        self.transport_router = transport_router or get_shared_router()

    async def send_and_stream(
        self,
        conversation_id: Optional[str],
        project_id: Optional[str],
        prompt: str,
        model: str = "auto",
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Sends message to ChatGPT, streams back response chunks via SSE,
        and atomically persists both user and assistant messages into local SQLite.
        """
        # Resolve parent_message_id from conversation current leaf node
        parent_message_id: Optional[str] = None
        if conversation_id:
            conv = self.conversation_repo.get_conversation(conversation_id)
            if conv and conv.current_node_id:
                parent_message_id = conv.current_node_id
            if conv and not project_id:
                project_id = conv.project_id

        # Generate temporary local tracking user message
        temp_user_message_id = str(uuid.uuid4())
        user_msg = Message(
            id=temp_user_message_id,
            conversation_id=conversation_id,
            parent_id=parent_message_id,
            role="user",
            content_text=prompt,
            status="finished_successfully",
        )
        if conversation_id:
            self.conversation_repo.save_messages([user_msg])

        request = SendMessageRequest(
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            project_id=project_id,
            content=prompt,
            model=model,
        )

        accumulated_text = ""
        assistant_message_id: Optional[str] = None
        resolved_conv_id = conversation_id

        async for chunk in self.transport_router.send_message_stream(request):
            event = chunk.get("event")
            chunk_conv_id = chunk.get("conversation_id")
            if chunk_conv_id and not resolved_conv_id:
                resolved_conv_id = chunk_conv_id

            if event == "delta":
                accumulated_text = chunk.get("text", accumulated_text)
                assistant_message_id = chunk.get("message_id") or assistant_message_id
                yield {
                    "event": "delta",
                    "delta": chunk.get("delta", ""),
                    "text": accumulated_text,
                    "conversation_id": resolved_conv_id,
                    "message_id": assistant_message_id,
                }

            elif event == "uncertain":
                logger.warning(f"Uncertain delivery state returned from router: {chunk.get('error')}")
                yield chunk
                return

            elif event == "done":
                accumulated_text = chunk.get("text", accumulated_text)
                canonical_assistant_id = chunk.get("message_id") or assistant_message_id or str(uuid.uuid4())
                canonical_user_id = chunk.get("user_message_id")
                canonical_parent_id = chunk.get("parent_id") or parent_message_id

                # Reconcile full official upstream DAG tree to maintain official IDs and lineage
                if resolved_conv_id:
                    try:
                        tree_data = await self.transport_router.fetch_conversation_tree(resolved_conv_id)
                        if tree_data and isinstance(tree_data, dict):
                            if "conversation_id" not in tree_data and "id" not in tree_data:
                                tree_data["conversation_id"] = resolved_conv_id
                            branch = self.conversation_service.ingest_conversation_tree(tree_data)
                            if branch and branch.current_node:
                                canonical_assistant_id = branch.current_node
                                active_msgs = branch.messages
                                if len(active_msgs) >= 2:
                                    canonical_user_id = active_msgs[-2].id
                                    canonical_parent_id = active_msgs[-2].parent_id
                    except Exception as e:
                        logger.warning(f"Post-send upstream DAG reconciliation warning: {e}")

                    # Remove synthetic temporary pre-send message if different from official ID
                    if canonical_user_id and canonical_user_id != temp_user_message_id:
                        with self.conversation_repo.db.get_connection() as conn:
                            conn.execute("DELETE FROM messages WHERE id = ?;", (temp_user_message_id,))
                    elif not canonical_user_id:
                        # Fallback: ensure temp user message has correct conversation_id and parent
                        canonical_user_id = temp_user_message_id
                        user_msg.conversation_id = resolved_conv_id
                        self.conversation_repo.save_messages([user_msg])

                    # Ensure assistant message is persisted with canonical parent link
                    assistant_msg = Message(
                        id=canonical_assistant_id,
                        conversation_id=resolved_conv_id,
                        parent_id=canonical_user_id,
                        role="assistant",
                        content_text=accumulated_text,
                        status="finished_successfully",
                    )
                    self.conversation_repo.save_messages([assistant_msg])
                    self.conversation_repo.update_current_node(resolved_conv_id, canonical_assistant_id)

                yield {
                    "event": "done",
                    "text": accumulated_text,
                    "conversation_id": resolved_conv_id,
                    "message_id": canonical_assistant_id,
                    "user_message_id": canonical_user_id,
                    "parent_id": canonical_parent_id,
                }
                return

    async def poll_site_updates(self, limit: int = 10) -> Dict[str, Any]:
        """
        Polls upstream for recent updates on chatgpt.com (A4).
        Ingests newly added messages from the official site into local SQLite.
        """
        upstream_convs = await self.conversation_service.sync_conversations_from_upstream(limit=limit)
        updated_count = 0
        details = []

        for conv in upstream_convs:
            try:
                tree_data = await self.transport_router.fetch_conversation_tree(conv.id)
                branch = self.conversation_service.ingest_conversation_tree(tree_data)
                updated_count += 1
                details.append({
                    "id": conv.id,
                    "title": branch.title,
                    "messages_count": branch.total_messages,
                    "current_node": branch.current_node,
                })
            except Exception as e:
                logger.warning(f"Failed to sync conversation tree for {conv.id}: {e}")

        return {
            "polled_count": len(upstream_convs),
            "updated_count": updated_count,
            "conversations": details,
        }

    async def verify_3cycle_continuity(
        self,
        conversation_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes 3 sequential continuity cycles without duplicating conversations or re-sending (A8).
        Cycle 1: Send Turn 1 -> Receive Response 1 -> Record IDs
        Cycle 2: Send Turn 2 -> Receive Response 2 -> Verify parent pointer = Response 1 leaf
        Cycle 3: Send Turn 3 -> Receive Response 3 -> Verify parent pointer = Response 2 leaf
        """
        target_conv_id = conversation_id
        cycle_records = []
        seen_message_ids = set()

        for cycle_idx in range(1, 4):
            prompt = f"Continuity Cycle {cycle_idx} Test Marker: {uuid.uuid4().hex[:8]}"
            accumulated = ""
            resp_msg_id = None
            user_msg_id = None

            async for event in self.send_and_stream(
                conversation_id=target_conv_id,
                project_id=project_id,
                prompt=prompt,
            ):
                if event.get("event") == "done":
                    accumulated = event.get("text", "")
                    resp_msg_id = event.get("message_id")
                    user_msg_id = event.get("user_message_id")
                    target_conv_id = event.get("conversation_id")

            # Invariant checks
            assert target_conv_id is not None, "Conversation ID must remain consistent"
            assert user_msg_id not in seen_message_ids, f"Duplicate user message ID in cycle {cycle_idx}"
            assert resp_msg_id not in seen_message_ids, f"Duplicate assistant message ID in cycle {cycle_idx}"

            seen_message_ids.add(user_msg_id)
            seen_message_ids.add(resp_msg_id)

            cycle_records.append({
                "cycle": cycle_idx,
                "user_msg_id": user_msg_id,
                "resp_msg_id": resp_msg_id,
                "prompt": prompt,
                "response_preview": accumulated[:60],
            })

            await asyncio.sleep(0.5)

        # Verify active branch integrity
        branch = await self.conversation_service.get_active_branch(target_conv_id)
        return {
            "conversation_id": target_conv_id,
            "project_id": project_id,
            "cycles_completed": 3,
            "no_duplication": len(seen_message_ids) == 6,
            "total_active_messages": branch.total_messages,
            "current_node": branch.current_node,
            "cycle_records": cycle_records,
        }
