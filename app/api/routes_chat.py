"""
Chat & Streaming API Endpoints (R3, A3, A4, A8).
"""

import json
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.sync_service import SyncService

router = APIRouter(prefix="/api/chat", tags=["Chat"])

_sync_service = SyncService()


def get_sync_service() -> SyncService:
    return _sync_service


class SendPromptRequest(BaseModel):
    prompt: str
    conversation_id: Optional[str] = None
    project_id: Optional[str] = None
    model: str = "auto"


@router.post("/send")
async def send_prompt(
    payload: SendPromptRequest,
    service: SyncService = Depends(get_sync_service),
):
    """
    Sends message and streams back response via Server-Sent Events (SSE).
    """
    if not payload.prompt.strip():
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    async def event_generator():
        try:
            async for chunk in service.send_and_stream(
                conversation_id=payload.conversation_id,
                project_id=payload.project_id,
                prompt=payload.prompt,
                model=payload.model,
            ):
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as e:
            err_data = {"event": "error", "error": str(e)}
            yield f"data: {json.dumps(err_data)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/poll-site")
async def poll_site_updates(
    limit: int = 10,
    service: SyncService = Depends(get_sync_service),
):
    """Poll upstream chatgpt.com for new messages and turns (A4)."""
    return await service.poll_site_updates(limit=limit)


@router.post("/verify-3cycle")
async def verify_3cycle(
    conversation_id: Optional[str] = None,
    project_id: Optional[str] = None,
    service: SyncService = Depends(get_sync_service),
):
    """Run 3-cycle continuity verification (A8)."""
    return await service.verify_3cycle_continuity(
        conversation_id=conversation_id, project_id=project_id
    )
