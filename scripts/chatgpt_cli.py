#!/usr/bin/env python3
"""
ChatGPT CLI Bridge (YOW Integration).
Allows creating and renaming conversations headlessly via the ChatGPT Integration POC.
Supports calling the running FastAPI server (http://127.0.0.1:8000) or executing via internal services.
"""

import argparse
import asyncio
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Base dir setup
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

POC_API_URL = os.environ.get("CHATGPT_POC_API_URL", "http://127.0.0.1:8000")


def try_http_request(endpoint: str, data: dict = None, method: str = "GET") -> tuple[bool, dict]:
    url = f"{POC_API_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        headers = {"Accept": "application/json"}
        payload_bytes = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            payload_bytes = json.dumps(data).encode("utf-8")

        req = urllib.request.Request(url, data=payload_bytes, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=120) as resp:
            content = resp.read().decode("utf-8")
            return True, json.loads(content)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
            return False, {"error": f"HTTP {e.code}: {body}", "status_code": e.code}
        except Exception:
            return False, {"error": f"HTTP {e.code}", "status_code": e.code}
    except Exception as e:
        return False, {"error": str(e)}


async def create_conversation_direct(project_id: str, title: str, prompt: str, model: str = "auto"):
    from app.services.sync_service import SyncService
    from app.persistence.db import db_manager
    from app.config import settings

    settings.BROWSER_HEADLESS = True
    settings.ensure_isolated_storage()
    db_manager.initialize_schema()

    sync_service = SyncService()
    return await sync_service.create_titled_conversation(
        project_id=project_id,
        title=title,
        prompt=prompt,
        model=model,
    )


async def rename_conversation_direct(conversation_id: str, title: str):
    from app.services.conversation_service import ConversationService
    from app.persistence.db import db_manager
    from app.config import settings

    settings.BROWSER_HEADLESS = True
    settings.ensure_isolated_storage()
    db_manager.initialize_schema()

    service = ConversationService()
    new_title = await service.rename_conversation(conversation_id, title)
    return {"ok": True, "conversation_id": conversation_id, "title": new_title}


def main():
    parser = argparse.ArgumentParser(description="ChatGPT Integration CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Command: create
    create_parser = subparsers.add_parser("create", help="Create a titled conversation in a project")
    create_parser.add_argument("--title", required=True, help="Title of the conversation")
    create_parser.add_argument("--project-id", default=None, help="Target ChatGPT project/gizmo ID (optional)")
    create_parser.add_argument("--prompt", default=None, help="Prompt text")
    create_parser.add_argument("--prompt-file", default=None, help="Path to file containing prompt text")
    create_parser.add_argument("--model", default="auto", help="Model slug to use (default: auto)")
    create_parser.add_argument("--direct", action="store_true", help="Force direct execution without FastAPI HTTP bridge")

    # Command: rename
    rename_parser = subparsers.add_parser("rename", help="Rename an existing conversation")
    rename_parser.add_argument("--conversation-id", required=True, help="Conversation ID to rename")
    rename_parser.add_argument("--title", required=True, help="New title for the conversation")
    rename_parser.add_argument("--direct", action="store_true", help="Force direct execution without FastAPI HTTP bridge")

    # Command: status
    subparsers.add_parser("status", help="Get session status")

    args = parser.parse_args()

    if args.command == "create":
        prompt = args.prompt
        if args.prompt_file:
            with open(args.prompt_file, "r", encoding="utf-8") as f:
                prompt = f.read()

        if not prompt or not prompt.strip():
            print(json.dumps({"ok": False, "error": "Prompt cannot be empty"}))
            sys.exit(1)

        payload = {
            "title": args.title,
            "prompt": prompt,
            "project_id": args.project_id,
            "model": args.model,
        }

        # Try HTTP bridge first if not --direct
        if not args.direct:
            ok, result = try_http_request("api/chat/conversations", data=payload, method="POST")
            if ok:
                result["ok"] = True
                print(json.dumps(result, ensure_ascii=False))
                sys.exit(0)

        # Fallback to direct async execution
        try:
            res = asyncio.run(create_conversation_direct(
                project_id=args.project_id,
                title=args.title,
                prompt=prompt,
                model=args.model,
            ))
            res["ok"] = True
            print(json.dumps(res, ensure_ascii=False))
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            sys.exit(1)

    elif args.command == "rename":
        payload = {"title": args.title}
        if not args.direct:
            ok, result = try_http_request(f"api/conversations/{args.conversation_id}/rename", data=payload, method="POST")
            if ok:
                print(json.dumps(result, ensure_ascii=False))
                sys.exit(0)

        try:
            res = asyncio.run(rename_conversation_direct(args.conversation_id, args.title))
            print(json.dumps(res, ensure_ascii=False))
            sys.exit(0)
        except Exception as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            sys.exit(1)

    elif args.command == "status":
        ok, result = try_http_request("api/session/status", method="GET")
        if ok:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(json.dumps({"ok": False, "error": result.get("error", "Failed to check session")}))


if __name__ == "__main__":
    main()
