"""
Conversation and Message DAG models (R2, R3, A2).
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from app.models.base import BaseModel, ConfigDict, Field


class MessageAuthor(BaseModel):
    """Message sender identity."""
    role: Literal["user", "assistant", "system", "tool"] = "user"
    name: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MessageContent(BaseModel):
    """Message text payload."""
    content_type: str = "text"
    parts: List[str] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.parts)


class Message(BaseModel):
    """
    Message entity supporting both flat database row and ChatGPT DAG node representations.
    """
    id: str
    conversation_id: Optional[str] = None
    parent_id: Optional[str] = None
    parent: Optional[str] = None
    children: List[str] = Field(default_factory=list)
    role: str = "user"
    author_role: str = "user"
    content_text: str = ""
    content_type: str = "text"
    status: Optional[str] = "finished_successfully"
    is_active_branch: bool = False
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        # Synchronize parent and parent_id
        if self.parent and not self.parent_id:
            self.parent_id = self.parent
        elif self.parent_id and not self.parent:
            self.parent = self.parent_id
        # Synchronize role and author_role
        if "author_role" in kwargs and "role" not in kwargs:
            self.role = kwargs["author_role"]
        elif "role" in kwargs and "author_role" not in kwargs:
            self.author_role = kwargs["role"]


# Alias for DAG node representation
MessageNode = Message


class Conversation(BaseModel):
    """
    Conversation entity supporting both flat repository and nested DAG mapping.
    """
    id: str
    title: str = "New Chat"
    project_id: Optional[str] = None
    current_node_id: Optional[str] = None
    current_node: Optional[str] = None
    model_slug: Optional[str] = "gpt-4o"
    is_archived: bool = False
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None
    mapping: Optional[Dict[str, MessageNode]] = None

    model_config = ConfigDict(from_attributes=True, protected_namespaces=())

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        # Synchronize current_node and current_node_id
        if self.current_node and not self.current_node_id:
            self.current_node_id = self.current_node
        elif self.current_node_id and not self.current_node:
            self.current_node = self.current_node_id


# Alias for schema compatibility across codebase
ConversationSchema = Conversation


class ActiveBranchResponse(BaseModel):
    """Chronologically ordered active branch from root to leaf."""
    conversation_id: str
    title: str = "New Chat"
    project_id: Optional[str] = None
    current_node: Optional[str] = None
    messages: List[MessageNode] = Field(default_factory=list)
    total_messages: int = 0


class SendMessageRequest(BaseModel):
    """Outbound message payload for continuing conversation."""
    conversation_id: Optional[str] = None
    parent_message_id: Optional[str] = None
    project_id: Optional[str] = None
    content: str = ""
    model: str = "auto"


class PaginatedConversations(BaseModel):
    """Paginated conversation list."""
    items: List[ConversationSchema] = Field(default_factory=list)
    total: int = 0
    offset: int = 0
    limit: int = 28
    has_more: bool = False
