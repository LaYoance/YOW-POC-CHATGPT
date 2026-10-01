"""
Project Service: manages ChatGPT projects, metadata, attached files and context (R2, A1, A5).
"""

import logging
from typing import List, Optional

from app.models.project import Project, ProjectContextSchema, ProjectSchema
from app.persistence.project_repo import ProjectRepo
from app.transports.router import TransportRouter, get_shared_router

logger = logging.getLogger(__name__)


class ProjectService:
    """
    Coordinates project discovery, local caching, and project context resolution
    (instructions and attached knowledge files).
    """

    def __init__(
        self,
        project_repo: Optional[ProjectRepo] = None,
        transport_router: Optional[TransportRouter] = None,
    ):
        self.project_repo = project_repo or ProjectRepo()
        self.transport_router = transport_router or get_shared_router()

    async def list_cached_projects(self) -> List[Project]:
        """Return projects stored in local SQLite cache."""
        return self.project_repo.list_projects()

    async def sync_projects_from_upstream(self) -> List[Project]:
        """Fetch latest projects from upstream and update local cache."""
        projects = await self.transport_router.fetch_projects()
        saved_projects: List[Project] = []
        for p in projects:
            try:
                # Optionally fetch detailed files/instructions if missing
                if not p.instructions or not p.files:
                    try:
                        detail = await self.transport_router.fetch_project_detail(p.id)
                        if detail:
                            p.instructions = detail.instructions or p.instructions
                            p.files = detail.files or p.files
                    except Exception as e:
                        logger.warning(f"Could not fetch detail for project {p.id}: {e}")

                saved = self.project_repo.upsert_project(p)
                saved_projects.append(saved)
            except Exception as e:
                logger.error(f"Failed to upsert project {p.id}: {e}")

        return saved_projects if saved_projects else await self.list_cached_projects()

    async def get_project(self, project_id: str) -> Optional[Project]:
        """Retrieve project from local cache, syncing from upstream if absent."""
        p = self.project_repo.get_project(project_id)
        if not p:
            try:
                detail = await self.transport_router.fetch_project_detail(project_id)
                if detail:
                    return self.project_repo.upsert_project(detail)
            except Exception as e:
                logger.warning(f"Failed to fetch upstream project {project_id}: {e}")
        return p

    async def get_project_with_context(self, project_id: str) -> Optional[ProjectContextSchema]:
        """
        Returns full project context including system instructions and attached files metadata (A5).
        """
        project = await self.get_project(project_id)
        if not project:
            return None

        return ProjectContextSchema(
            project=project,
            effective_instructions=project.instructions or "",
            attached_files_count=len(project.files),
            gizmo_id=project.gizmo_id or project.id,
        )
