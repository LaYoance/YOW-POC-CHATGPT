"""
Conversation Service: manages conversation listing, DAG tree parsing, and active branch reconstruction (R2, A2).
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.models.conversation import (
    ActiveBranchResponse,
    Conversation,
    ConversationSchema,
    Message,
    MessageNode,
    PaginatedConversations,
)
from app.persistence.conversation_repo import ConversationRepo
from app.transports.router import TransportRouter

logger = logging.getLogger(__name__)


class ConversationService:
    """
    Coordinates conversation listing, DAG tree ingestion, and active branch resolution.
    Satisfies R2 (exploration & pagination) and A2 (faithful active branch tracking).
    """

    def __init__(
        self,
        conversation_repo: Optional[ConversationRepo] = None,
        transport_router: Optional[TransportRouter] = None,
    ):
        self.conversation_repo = conversation_repo or ConversationRepo()
        self.transport_router = transport_router or TransportRouter()

    async def list_conversations(
        self,
        project_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 28,
        sync_upstream: bool = False,
    ) -> PaginatedConversations:
        """
        List conversations, optionally filtered by project_id.
        If sync_upstream is True or local cache is empty, fetches from upstream.
        """
        if sync_upstream:
            try:
                await self.sync_conversations_from_upstream(project_id=project_id, offset=offset, limit=limit)
            except Exception as e:
                logger.warning(f"Upstream conversation sync failed: {e}")

        items, total = self.conversation_repo.list_conversations(
            project_id=project_id, offset=offset, limit=limit
        )

        # If cache was empty and we didn't sync yet, try syncing once
        if total == 0 and not sync_upstream:
            try:
                await self.sync_conversations_from_upstream(project_id=project_id, offset=offset, limit=limit)
                items, total = self.conversation_repo.list_conversations(
                    project_id=project_id, offset=offset, limit=limit
                )
            except Exception as e:
                logger.warning(f"Initial upstream sync failed: {e}")

        return PaginatedConversations(
            items=items,
            total=total,
            offset=offset,
            limit=limit,
            has_more=(offset + len(items)) < total,
        )

    async def sync_conversations_from_upstream(
        self,
        project_id: Optional[str] = None,
        offset: int = 0,
        limit: int = 28,
    ) -> List[Conversation]:
        """Fetch conversations from upstream and cache them locally."""
        data = await self.transport_router.fetch_conversations(
            project_id=project_id, offset=offset, limit=limit
        )
        items = data.get("items", [])
        saved_list: List[Conversation] = []

        for item in items:
            c_id = item.get("id")
            if not c_id:
                continue

            created_at = item.get("create_time")
            if isinstance(created_at, (int, float)):
                created_at = datetime.fromtimestamp(created_at)

            updated_at = item.get("update_time")
            if isinstance(updated_at, (int, float)):
                updated_at = datetime.fromtimestamp(updated_at)

            # Determine project / gizmo attachment
            gizmo_id = item.get("gizmo_id") or project_id

            conv = Conversation(
                id=c_id,
                title=item.get("title") or "New Chat",
                project_id=gizmo_id,
                current_node_id=item.get("current_node"),
                created_at=created_at,
                updated_at=updated_at,
            )
            saved = self.conversation_repo.upsert_conversation(conv)
            saved_list.append(saved)

        return saved_list

    def ingest_conversation_tree(self, tree_data: Dict[str, Any]) -> ActiveBranchResponse:
        """
        Parses upstream conversation DAG tree, stores nodes in SQLite, updates leaf current_node,
        and reconstructs active branch via recursive CTE (A2).
        """
        conv_id = tree_data.get("conversation_id") or tree_data.get("id")
        if not conv_id:
            raise ValueError("Conversation tree data missing conversation_id")

        title = tree_data.get("title") or "New Chat"
        current_node_id = tree_data.get("current_node")
        mapping = tree_data.get("mapping", {})

        # Upsert base conversation record
        conv = Conversation(
            id=conv_id,
            title=title,
            current_node_id=current_node_id,
            project_id=tree_data.get("gizmo_id"),
        )
        self.conversation_repo.upsert_conversation(conv)

        # Parse messages from mapping
        parsed_messages: List[Message] = []
        for node_id, node in mapping.items():
            parent_id = node.get("parent")
            children = node.get("children", [])
            msg_obj = node.get("message")

            if not msg_obj:
                # Synthetic root or placeholder node
                continue

            author = msg_obj.get("author", {})
            role = author.get("role") or "user"
            content = msg_obj.get("content", {})
            parts = content.get("parts", [])
            content_type = content.get("content_type", "text")

            # Join parts to construct complete text
            text_parts = []
            for p in parts:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict) and "text" in p:
                    text_parts.append(p["text"])
            content_text = "".join(text_parts)

            # Ignore empty system instructions or hidden internal nodes
            if role == "system" and not content_text:
                continue

            created_at = msg_obj.get("create_time")
            if isinstance(created_at, (int, float)):
                created_at = datetime.fromtimestamp(created_at)

            parsed_messages.append(
                Message(
                    id=node_id,
                    conversation_id=conv_id,
                    parent_id=parent_id,
                    children=children,
                    role=role,
                    content_text=content_text,
                    content_type=content_type,
                    status=msg_obj.get("status", "finished_successfully"),
                    metadata=msg_obj.get("metadata", {}),
                    created_at=created_at,
                )
            )

        if parsed_messages:
            self.conversation_repo.save_messages(parsed_messages)

        if current_node_id:
            self.conversation_repo.update_current_node(conv_id, current_node_id)

        # Retrieve verified active branch via recursive CTE
        active_messages = self.conversation_repo.get_active_branch(conv_id)

        return ActiveBranchResponse(
            conversation_id=conv_id,
            title=title,
            project_id=conv.project_id,
            current_node=current_node_id,
            messages=active_messages,
            total_messages=len(active_messages),
        )

    async def get_active_branch(
        self,
        conversation_id: str,
        force_sync: bool = False,
    ) -> ActiveBranchResponse:
        """
        Retrieves active branch for a conversation.
        If force_sync is True or local messages are empty, fetches upstream tree.
        """
        local_messages = self.conversation_repo.get_active_branch(conversation_id)
        conv = self.conversation_repo.get_conversation(conversation_id)

        if force_sync or not local_messages:
            tree_data = await self.transport_router.fetch_conversation_tree(conversation_id)
            return self.ingest_conversation_tree(tree_data)

        return ActiveBranchResponse(
            conversation_id=conversation_id,
            title=conv.title if conv else "Chat",
            project_id=conv.project_id if conv else None,
            current_node=conv.current_node_id if conv else None,
            messages=local_messages,
            total_messages=len(local_messages),
        )
