"""
Transport Audit Logging Repository.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from app.models.transport import TransportAuditRecord, TransportMode
from app.persistence.db import DatabaseManager, db_manager


class TransportLogRepo:
    """Repository recording telemetry and audit entries for transport operations (R4)."""

    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def log_operation(
        self,
        operation: Union[str, TransportAuditRecord],
        transport_mode: Optional[str] = None,
        target_id: Optional[str] = None,
        status_code: Optional[int] = None,
        latency_ms: Optional[float] = None,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> int:
        """Appends a transport audit log entry to the database."""
        if isinstance(operation, TransportAuditRecord):
            record = operation
            op_name = record.operation
            t_mode = record.resolved_mode.value if hasattr(record.resolved_mode, "value") else str(record.resolved_mode)
            t_id = record.target_id
            s_code = record.status_code
            lat = record.latency_ms
            succ = record.success
            err = record.error_message
        else:
            op_name = operation
            t_mode = transport_mode.value if hasattr(transport_mode, "value") else str(transport_mode or "BROWSER_FETCH")
            t_id = target_id
            s_code = status_code
            lat = latency_ms
            succ = success
            err = error_message

        sql = """
        INSERT INTO transport_logs (
            operation, transport_mode, target_id, status_code, latency_ms, success, error_message, created_at
        ) VALUES (
            :operation, :transport_mode, :target_id, :status_code, :latency_ms, :success, :error_message, CURRENT_TIMESTAMP
        );
        """
        params = {
            "operation": op_name,
            "transport_mode": t_mode,
            "target_id": t_id,
            "status_code": s_code,
            "latency_ms": int(lat) if lat is not None else None,
            "success": 1 if succ else 0,
            "error_message": err,
        }
        with self.db.get_connection() as conn:
            cur = conn.execute(sql, params)
            return cur.lastrowid or 0

    def list_logs(
        self,
        limit: int = 50,
        operation: Optional[str] = None,
        transport_mode: Optional[str] = None,
    ) -> List[TransportAuditRecord]:
        """Lists recent transport audit records filtered by operation or mode."""
        clauses = []
        params: List[Any] = []
        if operation:
            clauses.append("operation = ?")
            params.append(operation)
        if transport_mode:
            clauses.append("transport_mode = ?")
            params.append(transport_mode)

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT id, operation, transport_mode, target_id, status_code, latency_ms, success, error_message, created_at
        FROM transport_logs
        {where_clause}
        ORDER BY created_at DESC
        LIMIT ?;
        """
        params.append(limit)

        with self.db.get_read_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            logs: List[TransportAuditRecord] = []
            for r in rows:
                item = dict(r)
                created_at = item.pop("created_at", None)
                if created_at and isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    except Exception:
                        created_at = None

                mode_val = item.get("transport_mode", TransportMode.BROWSER_FETCH)
                logs.append(
                    TransportAuditRecord(
                        id=item.get("id"),
                        timestamp=created_at or datetime.utcnow(),
                        operation=item.get("operation", ""),
                        attempted_mode=mode_val,
                        resolved_mode=mode_val,
                        target_id=item.get("target_id"),
                        status_code=item.get("status_code"),
                        latency_ms=float(item.get("latency_ms") or 0.0),
                        success=bool(item.get("success")),
                        error_message=item.get("error_message"),
                    )
                )
            return logs

    def clear_logs(self) -> None:
        """Deletes all logged audit records."""
        sql = "DELETE FROM transport_logs;"
        with self.db.get_connection() as conn:
            conn.execute(sql)
