"""
Project and Project Context models (R2, A5).
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from app.models.base import BaseModel, ConfigDict, Field


class ProjectFileSchema(BaseModel):
    """File attached to a ChatGPT Project (Gizmo)."""
    id: str
    name: str
    size_bytes: Optional[int] = None
    mime_type: Optional[str] = None
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class Project(BaseModel):
    """ChatGPT Project metadata and instructions."""
    id: str  # Gizmo ID (e.g. g-p-674b8892abcdef)
    title: str
    description: Optional[str] = None
    instructions: Optional[str] = None
    gizmo_id: Optional[str] = None
    files: List[ProjectFileSchema] = Field(default_factory=list)
    workspace_id: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_synced_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        if not self.gizmo_id and self.id:
            self.gizmo_id = self.id
        elif not self.id and self.gizmo_id:
            self.id = self.gizmo_id


# Alias for schema compatibility across codebase
ProjectSchema = Project


class ProjectListResponse(BaseModel):
    """Response returned by projects listing endpoints."""
    items: List[ProjectSchema] = Field(default_factory=list)
    total: int = 0


class ProjectContextSchema(BaseModel):
    """Resolved project context including instructions and attached files."""
    project: ProjectSchema
    effective_instructions: str = ""
    attached_files_count: int = 0
    gizmo_id: Optional[str] = None
