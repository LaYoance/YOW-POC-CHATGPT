"""
Projects API Endpoints (R2, A1, A5).
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException

from app.models.project import Project, ProjectContextSchema, ProjectListResponse
from app.services.project_service import ProjectService

router = APIRouter(prefix="/api/projects", tags=["Projects"])

_project_service = ProjectService()


def get_project_service() -> ProjectService:
    return _project_service


@router.get("", response_model=ProjectListResponse)
async def list_projects(service: ProjectService = Depends(get_project_service)):
    """List all cached projects."""
    projects = await service.list_cached_projects()
    return ProjectListResponse(items=projects, total=len(projects))


@router.post("/sync", response_model=ProjectListResponse)
async def sync_projects(service: ProjectService = Depends(get_project_service)):
    """Fetch latest projects from upstream and update local cache."""
    projects = await service.sync_projects_from_upstream()
    return ProjectListResponse(items=projects, total=len(projects))


@router.get("/{project_id}", response_model=Project)
async def get_project(project_id: str, service: ProjectService = Depends(get_project_service)):
    """Retrieve single project by ID."""
    p = await service.get_project(project_id)
    if not p:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return p


@router.get("/{project_id}/context", response_model=ProjectContextSchema)
async def get_project_context(project_id: str, service: ProjectService = Depends(get_project_service)):
    """Retrieve full project context (instructions, files)."""
    ctx = await service.get_project_with_context(project_id)
    if not ctx:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return ctx
