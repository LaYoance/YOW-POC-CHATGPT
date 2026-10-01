"""
Session Persistence & Credentials Repository.
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional, Union

from app.models.session import Session, SessionCredentials, SessionStatus
from app.persistence.db import DatabaseManager, db_manager


class SessionRepo:
    """Repository handling session record persistence and credentials retrieval."""

    def __init__(self, db: DatabaseManager = db_manager):
        self.db = db

    def get_default(self) -> Optional[Session]:
        """Loads default active session from database, or None if not configured."""
        query = """
        SELECT id, access_token, cookies_json, user_agent, device_id, account_id,
               user_email, user_name, status, expires_at, last_verified_at, created_at, updated_at
        FROM sessions
        WHERE id = 'default';
        """
        with self.db.get_read_connection() as conn:
            row = conn.execute(query).fetchone()
            if not row:
                return None
            data = dict(row)
            # Parse timestamps if string
            for ts_col in ("expires_at", "last_verified_at", "created_at", "updated_at"):
                if data.get(ts_col) and isinstance(data[ts_col], str):
                    try:
                        data[ts_col] = datetime.fromisoformat(data[ts_col].replace("Z", "+00:00"))
                    except Exception:
                        pass
            return Session(**data)

    def save(self, session: Union[Session, Dict[str, Any]]) -> Session:
        """Upserts a session into SQLite with conflict resolution."""
        if isinstance(session, dict):
            s_dict = session
        else:
            s_dict = session.model_dump()

        creds = s_dict.get("credentials")
        if creds:
            if not s_dict.get("access_token") and creds.get("access_token"):
                s_dict["access_token"] = creds["access_token"]
            if creds.get("cookies"):
                cookies_json = json.dumps(creds["cookies"])
            elif creds.get("raw_cookie_header"):
                pairs = {}
                for part in creds["raw_cookie_header"].split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        pairs[k] = v
                cookies_json = json.dumps(pairs)
            else:
                cookies_json = "{}"
        else:
            cookies_json = s_dict.get("cookies_json")
            if cookies_json is None and "cookies" in s_dict:
                cookies_json = json.dumps(s_dict["cookies"])
            elif cookies_json is None:
                cookies_json = "{}"

        # Format timestamps for SQLite
        expires_at = s_dict.get("expires_at")
        if isinstance(expires_at, datetime):
            expires_at = expires_at.isoformat()

        last_verified_at = s_dict.get("last_verified_at")
        if isinstance(last_verified_at, datetime):
            last_verified_at = last_verified_at.isoformat()

        status_val = s_dict.get("status", SessionStatus.VALID)
        if hasattr(status_val, "value"):
            status_val = status_val.value

        sql = """
        INSERT INTO sessions (
            id, access_token, cookies_json, user_agent, device_id, account_id,
            user_email, user_name, status, expires_at, last_verified_at, updated_at
        ) VALUES (
            :id, :access_token, :cookies_json, :user_agent, :device_id, :account_id,
            :user_email, :user_name, :status, :expires_at, :last_verified_at, CURRENT_TIMESTAMP
        )
        ON CONFLICT(id) DO UPDATE SET
            access_token = COALESCE(excluded.access_token, sessions.access_token),
            cookies_json = excluded.cookies_json,
            user_agent = excluded.user_agent,
            device_id = COALESCE(excluded.device_id, sessions.device_id),
            account_id = COALESCE(excluded.account_id, sessions.account_id),
            user_email = COALESCE(excluded.user_email, sessions.user_email),
            user_name = COALESCE(excluded.user_name, sessions.user_name),
            status = excluded.status,
            expires_at = COALESCE(excluded.expires_at, sessions.expires_at),
            last_verified_at = COALESCE(excluded.last_verified_at, sessions.last_verified_at),
            updated_at = CURRENT_TIMESTAMP;
        """
        params = {
            "id": s_dict.get("id", "default"),
            "access_token": s_dict.get("access_token"),
            "cookies_json": cookies_json,
            "user_agent": s_dict.get("user_agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
            "device_id": s_dict.get("device_id"),
            "account_id": s_dict.get("account_id"),
            "user_email": s_dict.get("user_email"),
            "user_name": s_dict.get("user_name"),
            "status": str(status_val),
            "expires_at": expires_at,
            "last_verified_at": last_verified_at,
        }

        with self.db.get_connection() as conn:
            conn.execute(sql, params)

        saved = self.get_default()
        if not saved:
            raise RuntimeError("Failed to retrieve session immediately after saving")
        return saved

    # Alias for save
    save_session = save

    def upsert_session(self, session_data: Dict[str, Any]) -> Session:
        """Alias for save."""
        return self.save(session_data)

    def update_status(self, status: Union[SessionStatus, str], session_id: str = "default") -> None:
        """Updates status of target session."""
        status_str = status.value if hasattr(status, "value") else str(status)
        sql = "UPDATE sessions SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
        with self.db.get_connection() as conn:
            conn.execute(sql, (status_str, session_id))

    def get_credentials(self, session_id: str = "default") -> Optional[SessionCredentials]:
        """Returns safe encapsulated credentials or None if missing."""
        sess = self.get_default()
        if not sess or sess.status == SessionStatus.MISSING or not sess.access_token:
            return None

        cookies = sess.cookies
        session_token = cookies.get("__Secure-next-auth.session-token") or cookies.get("next-auth.session-token")
        cf_clearance = cookies.get("cf_clearance")

        return SessionCredentials(
            access_token=sess.access_token,
            session_token=session_token,
            cf_clearance=cf_clearance,
            device_id=sess.device_id,
            user_id=sess.account_id,
            email=sess.user_email,
            account_id=sess.account_id,
            expires_at=sess.expires_at,
        )

    def delete_session(self, session_id: str = "default") -> None:
        """Removes a session record completely."""
        sql = "DELETE FROM sessions WHERE id = ?;"
        with self.db.get_connection() as conn:
            conn.execute(sql, (session_id,))
