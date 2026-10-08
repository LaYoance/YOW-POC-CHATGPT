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
from app.transports.router import TransportRouter, get_shared_router

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
        self._transport_router = transport_router

    @property
    def transport_router(self) -> TransportRouter:
        if self._transport_router is not None:
            return self._transport_router
        return get_shared_router()

    @transport_router.setter
    def transport_router(self, router: TransportRouter) -> None:
        self._transport_router = router

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

            # Handle non-parts payload types (code blocks, tool executions, errors, thoughts)
            if not content_text:
                if content_type == "code":
                    lang = content.get("language") or ""
                    if lang == "unknown":
                        lang = ""
                    raw_code = content.get("text", "")
                    content_text = f"```{lang}\n{raw_code}\n```"
                elif content_type == "execution_output":
                    raw_out = content.get("text", "")
                    content_text = f"```output\n{raw_out}\n```"
                elif content_type == "system_error":
                    raw_err = content.get("text", "")
                    content_text = f"```error\n{raw_err}\n```"
                elif content_type == "reasoning_recap":
                    recap = content.get("content", "")
                    if recap:
                        content_text = f"> 💭 *{recap}*"
                elif content_type == "thoughts":
                    thoughts_list = content.get("thoughts", [])
                    summaries = [t.get("summary", "") for t in thoughts_list if isinstance(t, dict) and t.get("summary")]
                    if summaries:
                        content_text = f"> 💭 *{'; '.join(summaries)}*"
                elif content.get("text"):
                    content_text = content.get("text", "")

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

    async def rename_conversation(self, conversation_id: str, title: str) -> str:
        """
        Rename a conversation upstream, then mirror the new title in local SQLite.
        Returns the normalized title actually applied.
        """
        clean = " ".join((title or "").split())
        if not clean:
            raise ValueError("Title cannot be empty")
        clean = clean[:200]

        await self.transport_router.rename_conversation(conversation_id, clean)

        with self.conversation_repo.db.get_connection() as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, last_synced_at = CURRENT_TIMESTAMP WHERE id = ?;",
                (clean, conversation_id),
            )
        return clean

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

    async def download_interpreter_file(
        self,
        conversation_id: str,
        sandbox_path: str,
        message_id: Optional[str] = None,
    ) -> Tuple[Any, str, str]:
        """
        Downloads a file generated in Code Interpreter sandbox for a conversation.
        Caches file locally in .session_data/downloads/{conversation_id}/{filename}.
        Returns (local_path, filename, media_type).
        """
        import mimetypes
        from pathlib import Path
        from app.config import settings

        clean_path = sandbox_path.strip()
        if clean_path.startswith("sandbox:"):
            clean_path = clean_path[len("sandbox:"):]

        filename = Path(clean_path).name
        if not filename or filename in (".", ".."):
            raise ValueError(f"Invalid sandbox path or filename: {sandbox_path}")

        download_dir = settings.full_session_data_dir / "downloads" / conversation_id
        download_dir.mkdir(parents=True, exist_ok=True)
        target_file = download_dir / filename

        # Return cached copy if available and non-empty
        if target_file.exists() and target_file.stat().st_size > 0:
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            return target_file, filename, media_type

        # Build candidate message IDs
        candidates: List[str] = []
        if message_id:
            candidates.append(message_id)

        # Look up candidate messages in SQLite if needed
        try:
            with self.conversation_repo.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, parent_id, role, content_type
                    FROM messages
                    WHERE conversation_id = ?
                      AND (content_text LIKE ? OR content_text LIKE ?)
                    ORDER BY (role = 'assistant') DESC, created_at DESC
                    """,
                    (conversation_id, f"%{filename}%", f"%{clean_path}%"),
                )
                rows = cursor.fetchall()
                for r in rows:
                    mid = r[0]
                    pid = r[1]
                    if mid and mid not in candidates:
                        candidates.append(mid)
                    if pid and pid not in candidates:
                        candidates.append(pid)
        except Exception as e:
            logger.warning(f"Failed to query messages for interpreter download: {e}")

        if not candidates:
            # Fallback to current_node_id of the conversation
            conv = self.conversation_repo.get_conversation(conversation_id)
            if conv and conv.current_node_id:
                candidates.append(conv.current_node_id)

        if not candidates:
            raise ValueError(f"No message found associated with {sandbox_path} in conversation {conversation_id}")

        last_error = None
        download_url = None
        for candidate_id in candidates:
            try:
                download_url = await self.transport_router.get_interpreter_download_url(
                    conversation_id=conversation_id,
                    message_id=candidate_id,
                    sandbox_path=clean_path,
                )
                if download_url:
                    break
            except Exception as e:
                last_error = e
                logger.debug(f"Candidate {candidate_id} failed for {clean_path}: {e}")

        if not download_url:
            raise ValueError(f"Failed to obtain interpreter download URL for {clean_path}: {last_error}")

        file_bytes = await self.transport_router.download_file_bytes(download_url)
        target_file.write_bytes(file_bytes)
        try:
            target_file.chmod(0o600)
        except OSError:
            pass

        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return target_file, filename, media_type

