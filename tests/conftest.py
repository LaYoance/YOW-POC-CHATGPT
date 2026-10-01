"""
Pytest configuration and test fixtures for ChatGPT Integration POC.
Provides background mock server and isolated test database.
"""

import asyncio
import os
import socket
import threading
import time
from pathlib import Path
from typing import Generator

import pytest
import uvicorn

from app.config import settings
from app.models.session import Session, SessionCredentials, SessionStatus
from app.persistence.conversation_repo import ConversationRepo
from app.persistence.db import DatabaseManager
from app.persistence.project_repo import ProjectRepo
from app.persistence.session_repo import SessionRepo
from app.persistence.transport_log_repo import TransportLogRepo
from tests.mocks.fake_chatgpt_server import app as fake_app


def get_free_port() -> int:
    """Find available ephemeral loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def fake_upstream_server() -> Generator[str, None, None]:
    """Spins up fake ChatGPT upstream server on a loopback port in a background thread."""
    port = get_free_port()
    config = uvicorn.Config(app=fake_app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server readiness
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    yield url

    server.should_exit = True
    thread.join(timeout=2.0)


@pytest.fixture
def isolated_db(tmp_path: Path) -> DatabaseManager:
    """Provides clean SQLite database isolated in tmp_path with initialized schema."""
    db_file = tmp_path / "test_isolated.db"
    db_manager = DatabaseManager(db_path=db_file)
    db_manager.initialize_schema()
    return db_manager


@pytest.fixture
def test_repos(isolated_db: DatabaseManager):
    """Provides instantiated repositories for test DB."""
    return {
        "session_repo": SessionRepo(db=isolated_db),
        "project_repo": ProjectRepo(db=isolated_db),
        "conversation_repo": ConversationRepo(db=isolated_db),
        "log_repo": TransportLogRepo(db=isolated_db),
    }


@pytest.fixture
def valid_session(test_repos) -> Session:
    """Inserts a valid test session into the isolated session repository."""
    session_repo: SessionRepo = test_repos["session_repo"]
    s = Session(
        status=SessionStatus.VALID,
        credentials=SessionCredentials(
            access_token="valid-test-access-token",
            cookies={"__Secure-next-auth.session-token": "valid-session-cookie"},
            device_id="test-device-uuid-1234",
            raw_cookie_header="__Secure-next-auth.session-token=valid-session-cookie",
        ),
        user={"id": "user-test-poc", "email": "test@example.com"},
    )
    return session_repo.save_session(s)
