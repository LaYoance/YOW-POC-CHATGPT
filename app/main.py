"""
FastAPI Main Application and Lifespan Manager (R1-R5).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import (
    chat_router,
    conversations_router,
    projects_router,
    session_router,
    transport_router,
)
from app.config import settings
from app.core.logging_filter import SensitiveDataFilter
from app.persistence.db import db_manager

# Configure logging with security redaction filter
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
root_logger = logging.getLogger()
root_logger.addFilter(SensitiveDataFilter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle."""
    # Ensure 0700 isolated storage directory and 0600 DB
    settings.ensure_isolated_storage()
    # Initialize SQLite schema and pragmas
    db_manager.initialize_schema()
    logging.info(f"Initialized SQLite database at {settings.db_path} with WAL mode.")
    yield
    # Shutdown hook
    logging.info("Shutting down ChatGPT Integration POC service.")


app = FastAPI(
    title="ChatGPT Integration POC",
    description="Autonomous local POC for bidirectional ChatGPT projects and conversations synchronization.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routers
app.include_router(session_router)
app.include_router(projects_router)
app.include_router(conversations_router)
app.include_router(chat_router)
app.include_router(transport_router)

# Mount static frontend
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)

if (STATIC_DIR / "index.html").exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/c/{conversation_id}", include_in_schema=False)
    @app.get("/c/{conversation_id}/", include_in_schema=False)
    async def serve_conversation(conversation_id: str):
        return FileResponse(str(STATIC_DIR / "index.html"))
else:
    @app.get("/", include_in_schema=False)
    async def serve_fallback_index():
        return {
            "name": "ChatGPT Integration POC",
            "status": "online",
            "docs": "/docs",
        }
