"""
Transport Router & Telemetry Endpoints (R4).
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.models.transport import TransportAuditRecord, TransportMode, TransportStatusReport
from app.transports.router import TransportRouter

router = APIRouter(prefix="/api/transport", tags=["Transport"])

_router_instance = TransportRouter()


def get_transport_router() -> TransportRouter:
    return _router_instance


class SetTransportModeRequest(BaseModel):
    mode: Optional[TransportMode] = None


@router.get("/status", response_model=TransportStatusReport)
async def get_transport_status(
    transport_router: TransportRouter = Depends(get_transport_router),
):
    """Retrieve current transport router configuration, health and failures."""
    return transport_router.get_status_report()


@router.post("/mode", response_model=TransportStatusReport)
async def set_transport_mode(
    payload: SetTransportModeRequest,
    transport_router: TransportRouter = Depends(get_transport_router),
):
    """
    Force a specific transport mode (DIRECT_HTTP, BROWSER_FETCH, UI_DRIVEN)
    or pass null to restore automatic failover.
    """
    transport_router.set_forced_mode(payload.mode)
    return transport_router.get_status_report()


@router.get("/logs", response_model=List[TransportAuditRecord])
async def list_transport_logs(
    limit: int = Query(50, ge=1, le=200),
    operation: Optional[str] = None,
    mode: Optional[str] = None,
    transport_router: TransportRouter = Depends(get_transport_router),
):
    """List recent transport telemetry and audit records."""
    return transport_router.log_repo.list_logs(limit=limit, operation=operation, transport_mode=mode)
