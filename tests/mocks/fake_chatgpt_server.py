"""
High-Fidelity Fake ChatGPT Upstream Server for Sandboxed Verification (A1-A8, R1-R5).
Simulates NextAuth session, gizmo discovery, DAG conversations, and SSE streaming.
"""

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

app = FastAPI(title="Fake ChatGPT Upstream Server")

# In-memory mock state
STATE = {
    "projects": [
        {
            "id": "g-p-test-proj-01",
            "gizmo_id": "g-p-test-proj-01",
            "display": {
                "name": "Test Project Alpha",
                "description": "ChatGPT project for POC verification with custom instructions",
            },
            "instructions": "Always include [ALPHA-PROJECT-ACK] PRJ-TOKEN-7719 in responses.",
            "files": [
                {
                    "id": "file-canary-01",
                    "name": "canary_knowledge.txt",
                    "size_bytes": 1024,
                    "mime_type": "text/plain",
                    "canary_marker": "CANARY-OMEGA-9921-X",
                }
            ],
            "workspace_id": "ws-mock-01",
        }
    ],
    "conversations": {
        "conv-test-01": {
            "id": "conv-test-01",
            "title": "Existing Test Chat",
            "gizmo_id": "g-p-test-proj-01",
            "create_time": 1727780000,
            "update_time": 1727781000,
            "current_node": "msg-02",
            "mapping": {
                "node-root": {
                    "id": "node-root",
                    "parent": None,
                    "children": ["msg-01"],
                    "message": None,
                },
                "msg-01": {
                    "id": "msg-01",
                    "parent": "node-root",
                    "children": ["msg-02"],
                    "message": {
                        "id": "msg-01",
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": ["Test question on chatgpt.com"]},
                        "status": "finished_successfully",
                        "create_time": 1727780000,
                    },
                },
                "msg-02": {
                    "id": "msg-02",
                    "parent": "msg-01",
                    "children": [],
                    "message": {
                        "id": "msg-02",
                        "author": {"role": "assistant"},
                        "content": {
                            "content_type": "text",
                            "parts": ["[ALPHA-PROJECT-ACK] PRJ-TOKEN-7719 Reponse initiale CANARY-OMEGA-9921-X"],
                        },
                        "status": "finished_successfully",
                        "create_time": 1727780010,
                    },
                },
            },
        }
    },
    "session_valid": True,
}


@app.get("/api/auth/session")
async def get_session(request: Request):
    auth = request.headers.get("authorization", "")
    cookie = request.headers.get("cookie", "")

    # Check simulated invalid session
    if not STATE["session_valid"] or ("invalid" in auth or "invalid" in cookie):
        raise HTTPException(status_code=401, detail="Session expired or invalid")

    return {
        "user": {
            "id": "user-test-poc-777",
            "name": "POC Tester",
            "email": "test-user@poc-antigravity.local",
            "image": "https://example.com/avatar.png",
        },
        "expires": "2026-11-01T00:00:00.000Z",
        "accessToken": "mock-access-token-jwt-valid-12345",
    }


@app.get("/backend-api/gizmos/snorlax/sidebar")
async def list_gizmos(request: Request):
    auth = request.headers.get("authorization", "")
    if not auth and not request.headers.get("cookie"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    return {
        "items": [{"gizmo": p} for p in STATE["projects"]],
        "cursor": None,
    }


@app.get("/backend-api/gizmos/{project_id}")
async def get_gizmo(project_id: str, request: Request):
    for p in STATE["projects"]:
        if p["id"] == project_id or p.get("gizmo_id") == project_id:
            return {"gizmo": p}
    raise HTTPException(status_code=404, detail="Project not found")


@app.get("/backend-api/conversations")
async def list_conversations(
    request: Request,
    offset: int = 0,
    limit: int = 28,
    gizmo_id: Optional[str] = None,
):
    convs = list(STATE["conversations"].values())
    if gizmo_id:
        convs = [c for c in convs if c.get("gizmo_id") == gizmo_id]

    items = []
    for c in convs[offset : offset + limit]:
        items.append({
            "id": c["id"],
            "title": c["title"],
            "create_time": c["create_time"],
            "update_time": c["update_time"],
            "current_node": c["current_node"],
            "gizmo_id": c.get("gizmo_id"),
        })

    return {
        "items": items,
        "total": len(convs),
        "limit": limit,
        "offset": offset,
        "has_missing": False,
    }


@app.get("/backend-api/conversation/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request):
    if conversation_id not in STATE["conversations"]:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return STATE["conversations"][conversation_id]


@app.post("/backend-api/conversation")
async def send_conversation(request: Request):
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        body = json.loads(raw.decode("utf-8")) if raw else {}

    conv_id = body.get("conversation_id") or f"conv-{uuid.uuid4().hex[:8]}"
    parent_id = body.get("parent_message_id")
    messages = body.get("messages", [])
    user_content = messages[0]["content"]["parts"][0] if messages else "Hello"
    user_msg_id = messages[0]["id"] if messages else str(uuid.uuid4())

    conv_mode = body.get("conversation_mode", {})
    gizmo_id = conv_mode.get("gizmo_id")

    # Generate assistant response with witness markers if project gizmo matches
    assistant_msg_id = str(uuid.uuid4())
    parts = []

    if gizmo_id == "g-p-test-proj-01":
        parts.append("[ALPHA-PROJECT-ACK] PRJ-TOKEN-7719 ")
        parts.append(f"Mock Assistant response to: {user_content} ")
        parts.append("CANARY-OMEGA-9921-X")
    else:
        parts.append(f"Standard response to: {user_content}")

    full_response = "".join(parts)

    # Ingest turn into mock state
    if conv_id not in STATE["conversations"]:
        STATE["conversations"][conv_id] = {
            "id": conv_id,
            "title": user_content[:30],
            "gizmo_id": gizmo_id,
            "create_time": 1727780000,
            "update_time": 1727782000,
            "current_node": assistant_msg_id,
            "mapping": {
                "root": {"id": "root", "parent": None, "children": [user_msg_id], "message": None},
                user_msg_id: {
                    "id": user_msg_id,
                    "parent": "root",
                    "children": [assistant_msg_id],
                    "message": {
                        "id": user_msg_id,
                        "author": {"role": "user"},
                        "content": {"content_type": "text", "parts": [user_content]},
                        "status": "finished_successfully",
                    },
                },
                assistant_msg_id: {
                    "id": assistant_msg_id,
                    "parent": user_msg_id,
                    "children": [],
                    "message": {
                        "id": assistant_msg_id,
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": [full_response]},
                        "status": "finished_successfully",
                    },
                },
            },
        }
    else:
        conv = STATE["conversations"][conv_id]
        parent = parent_id or conv["current_node"]
        if parent in conv["mapping"]:
            conv["mapping"][parent]["children"].append(user_msg_id)
        conv["mapping"][user_msg_id] = {
            "id": user_msg_id,
            "parent": parent,
            "children": [assistant_msg_id],
            "message": {
                "id": user_msg_id,
                "author": {"role": "user"},
                "content": {"content_type": "text", "parts": [user_content]},
                "status": "finished_successfully",
            },
        }
        conv["mapping"][assistant_msg_id] = {
            "id": assistant_msg_id,
            "parent": user_msg_id,
            "children": [],
            "message": {
                "id": assistant_msg_id,
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [full_response]},
                "status": "finished_successfully",
            },
        }
        conv["current_node"] = assistant_msg_id
        conv["update_time"] = 1727783000

    async def sse_stream():
        # Stream chunks simulating real ChatGPT delta emission
        step = 5
        for i in range(0, len(full_response), step):
            sub = full_response[: i + step]
            chunk_data = {
                "conversation_id": conv_id,
                "message": {
                    "id": assistant_msg_id,
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": [sub]},
                    "status": "in_progress",
                },
            }
            yield f"data: {json.dumps(chunk_data)}\n\n"
            await asyncio.sleep(0.01)

        # Final chunk
        final_chunk = {
            "conversation_id": conv_id,
            "message": {
                "id": assistant_msg_id,
                "author": {"role": "assistant"},
                "content": {"content_type": "text", "parts": [full_response]},
                "status": "finished_successfully",
            },
        }
        yield f"data: {json.dumps(final_chunk)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_stream(), media_type="text/event-stream")


def add_turn_on_site(conversation_id: str, prompt: str, response_text: str):
    """Simulates an exchange directly added on chatgpt.com (A4)."""
    conv = STATE["conversations"].get(conversation_id)
    if not conv:
        return
    current = conv["current_node"]
    u_id = f"site-msg-{uuid.uuid4().hex[:6]}"
    a_id = f"site-msg-{uuid.uuid4().hex[:6]}"

    if current in conv["mapping"]:
        conv["mapping"][current]["children"].append(u_id)

    conv["mapping"][u_id] = {
        "id": u_id,
        "parent": current,
        "children": [a_id],
        "message": {
            "id": u_id,
            "author": {"role": "user"},
            "content": {"content_type": "text", "parts": [prompt]},
            "status": "finished_successfully",
        },
    }
    conv["mapping"][a_id] = {
        "id": a_id,
        "parent": u_id,
        "children": [],
        "message": {
            "id": a_id,
            "author": {"role": "assistant"},
            "content": {"content_type": "text", "parts": [response_text]},
            "status": "finished_successfully",
        },
    }
    conv["current_node"] = a_id
    conv["update_time"] = int(asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 1727784000)
    return u_id, a_id
