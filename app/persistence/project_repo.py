"""
Project Metadata & Instructions Repository.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from app.models.project import Project, ProjectFileSchema
from app.persistence.db import DatabaseManager, db_manager


class ProjectRepo:
    """Repository handling ChatGPT project metadata, system instructions, and file references."""

    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def upsert_project(self, project: Union[Project, Dict[str, Any]]) -> Project:
        """Inserts or updates a project record."""
        if isinstance(project, dict):
            p_dict = project
        else:
            p_dict = project.model_dump()

        files_json = p_dict.get("files_metadata_json")
        if files_json is None and "files" in p_dict:
            files_val = p_dict["files"]
            if isinstance(files_val, list):
                files_json = json.dumps([
                    f.model_dump() if hasattr(f, "model_dump") else f for f in files_val
                ])
            else:
                files_json = "[]"
        elif files_json is None:
            files_json = "[]"

        created_at = p_dict.get("created_at")
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()

        updated_at = p_dict.get("updated_at")
        if isinstance(updated_at, datetime):
            updated_at = updated_at.isoformat()

        sql = """
        INSERT INTO projects (
            id, title, description, instructions, files_metadata_json, workspace_id, created_at, updated_at, last_synced_at
        ) VALUES (
            :id, :title, :description, :instructions, :files_metadata_json, :workspace_id, :created_at, :updated_at, CURRENT_TIMESTAMP
        )
        ON CONFLICT(id) DO UPDATE SET
            title = excluded.title,
            description = excluded.description,
            instructions = COALESCE(excluded.instructions, projects.instructions),
            files_metadata_json = COALESCE(excluded.files_metadata_json, projects.files_metadata_json),
            workspace_id = COALESCE(excluded.workspace_id, projects.workspace_id),
            updated_at = COALESCE(excluded.updated_at, projects.updated_at),
            last_synced_at = CURRENT_TIMESTAMP;
        """
        params = {
            "id": p_dict["id"],
            "title": p_dict["title"],
            "description": p_dict.get("description"),
            "instructions": p_dict.get("instructions"),
            "files_metadata_json": files_json,
            "workspace_id": p_dict.get("workspace_id"),
            "created_at": created_at,
            "updated_at": updated_at,
        }

        with self.db.get_connection() as conn:
            conn.execute(sql, params)

        saved = self.get_project(p_dict["id"])
        if not saved:
            raise RuntimeError(f"Failed to load project {p_dict['id']} after upsert")
        return saved

    def list_projects(self) -> List[Project]:
        """Lists all cached projects sorted alphabetically by title."""
        sql = """
        SELECT id, title, description, instructions, files_metadata_json, workspace_id, created_at, updated_at, last_synced_at
        FROM projects
        ORDER BY title ASC;
        """
        with self.db.get_read_connection() as conn:
            rows = conn.execute(sql).fetchall()
            results: List[Project] = []
            for r in rows:
                item = dict(r)
                raw_files = item.pop("files_metadata_json", None) or "[]"
                try:
                    parsed_files = json.loads(raw_files)
                except Exception:
                    parsed_files = []
                item["files"] = [ProjectFileSchema(**f) if isinstance(f, dict) else f for f in parsed_files]
                results.append(Project(**item))
            return results

    def get_project(self, project_id: str) -> Optional[Project]:
        """Retrieves a single project by its ID."""
        sql = """
        SELECT id, title, description, instructions, files_metadata_json, workspace_id, created_at, updated_at, last_synced_at
        FROM projects
        WHERE id = ?;
        """
        with self.db.get_read_connection() as conn:
            row = conn.execute(sql, (project_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            raw_files = item.pop("files_metadata_json", None) or "[]"
            try:
                parsed_files = json.loads(raw_files)
            except Exception:
                parsed_files = []
            item["files"] = [ProjectFileSchema(**f) if isinstance(f, dict) else f for f in parsed_files]
            return Project(**item)

    def get_project_by_id(self, project_id: str) -> Optional[Project]:
        """Alias for get_project."""
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> None:
        """Deletes a project record."""
        sql = "DELETE FROM projects WHERE id = ?;"
        with self.db.get_connection() as conn:
            conn.execute(sql, (project_id,))
