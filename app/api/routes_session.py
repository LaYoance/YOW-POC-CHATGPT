"""
Session Management API Endpoints (R1, A7).
"""

from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.models.session import ReconnectFlowStatus, SessionStatusResponse
from app.services.session_service import SessionService

router = APIRouter(prefix="/api/session", tags=["Session"])

_session_service = SessionService()


def get_session_service() -> SessionService:
    return _session_service


class ImportSessionRequest(BaseModel):
    cookies: str
    access_token: Optional[str] = None
    device_id: Optional[str] = None


@router.get("/status", response_model=SessionStatusResponse)
async def get_session_status(service: SessionService = Depends(get_session_service)):
    """Check current session status against local DB and upstream."""
    return await service.get_status_response()


@router.post("/import", response_model=SessionStatusResponse)
async def import_session(
    payload: ImportSessionRequest,
    service: SessionService = Depends(get_session_service),
):
    """Import session cookies and/or JWT access token."""
    res = await service.import_session_data(
        cookies_input=payload.cookies,
        access_token=payload.access_token,
        device_id=payload.device_id,
    )
    return res


@router.post("/reconnect", response_model=ReconnectFlowStatus)
async def start_reconnect(service: SessionService = Depends(get_session_service)):
    """Launch interactive browser reconnection wizard."""
    return await service.start_reconnect_wizard()


@router.get("/reconnect/status", response_model=ReconnectFlowStatus)
async def get_reconnect_status(service: SessionService = Depends(get_session_service)):
    """Poll reconnection wizard status."""
    return service.get_reconnect_status()


@router.post("/sync-browser", response_model=SessionStatusResponse)
async def sync_browser_session(service: SessionService = Depends(get_session_service)):
    """Extract cookies directly from the Chromium profile directory."""
    return await service.sync_from_browser_profile()
