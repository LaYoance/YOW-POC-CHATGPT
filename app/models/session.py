"""
Session domain models, credentials, and state machine schemas (R1, A7).
"""

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from app.models.base import BaseModel, ConfigDict, Field


class SessionStatus(str, Enum):
    """Session operational states."""
    VALID = "VALID"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"


class SessionUser(BaseModel):
    """User profile information returned by ChatGPT NextAuth."""
    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    image: Optional[str] = None
    plan_type: Optional[str] = "unknown"


class SessionCredentials(BaseModel):
    """
    Encapsulated session secrets stored in isolated local storage.
    Includes custom masking to prevent accidental leakage in logs or representations.
    """
    access_token: str = ""
    session_token: Optional[str] = None
    cf_clearance: Optional[str] = None
    device_id: Optional[str] = None
    user_id: Optional[str] = None
    email: Optional[str] = None
    account_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    cookies: Dict[str, str] = Field(default_factory=dict)
    raw_cookie_header: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        return bool(self.access_token or self.session_token or self.cookies or self.raw_cookie_header)

    def masked_access_token(self) -> str:
        """Returns safe masked representation for diagnostics."""
        if not self.access_token or len(self.access_token) < 10:
            return "***"
        return f"{self.access_token[:4]}...{self.access_token[-4:]}"

    def __repr__(self) -> str:
        return f"<SessionCredentials user_id={self.user_id} token={self.masked_access_token()}>"

    def __str__(self) -> str:
        return self.__repr__()


class Session(BaseModel):
    """Internal database representation of a session."""
    id: str = "default"
    access_token: Optional[str] = None
    cookies_json: str = "{}"
    user_agent: str = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    device_id: Optional[str] = None
    account_id: Optional[str] = None
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    status: SessionStatus = SessionStatus.MISSING
    expires_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    credentials: Optional[SessionCredentials] = None
    user: Optional[Dict[str, Any]] = None

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        if self.credentials:
            if not self.access_token and self.credentials.access_token:
                self.access_token = self.credentials.access_token
            if not self.device_id and self.credentials.device_id:
                self.device_id = self.credentials.device_id
            if not self.user_email and self.credentials.email:
                self.user_email = self.credentials.email
            if self.credentials.cookies:
                self.cookies_json = json.dumps(self.credentials.cookies)
            elif self.credentials.raw_cookie_header and not self.cookies:
                pairs = {}
                for part in self.credentials.raw_cookie_header.split(";"):
                    if "=" in part:
                        k, v = part.strip().split("=", 1)
                        pairs[k] = v
                self.cookies_json = json.dumps(pairs)
        elif self.access_token or self.cookies:
            raw_h = "; ".join([f"{k}={v}" for k, v in self.cookies.items()]) if self.cookies else None
            self.credentials = SessionCredentials(
                access_token=self.access_token or "",
                session_token=self.cookies.get("__Secure-next-auth.session-token"),
                cf_clearance=self.cookies.get("cf_clearance"),
                device_id=self.device_id,
                email=self.user_email,
                expires_at=self.expires_at,
                cookies=self.cookies,
                raw_cookie_header=raw_h,
            )
        else:
            self.credentials = SessionCredentials(access_token="")

    @property
    def cookies(self) -> Dict[str, str]:
        try:
            return json.loads(self.cookies_json or "{}")
        except Exception:
            return {}


class SessionSchema(BaseModel):
    """Public session view excluding sensitive tokens."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    status: SessionStatus = SessionStatus.MISSING
    user_id: Optional[str] = None
    email: Optional[str] = None
    account_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    created_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: Optional[datetime] = Field(default_factory=lambda: datetime.now(timezone.utc))

    model_config = ConfigDict(from_attributes=True)


class SessionCreate(BaseModel):
    """Payload to import or create a session."""
    raw_cookie_header: Optional[str] = None
    session_token: Optional[str] = None
    access_token: Optional[str] = None
    cf_clearance: Optional[str] = None
    device_id: Optional[str] = None


class SessionImportRequest(BaseModel):
    """Request payload for manual cookie injection."""
    cookies: str = Field(..., description="Raw cookie string, JSON cookies export, or Netscape cookie format")


class ReconnectRequest(BaseModel):
    """Request to initiate reconnection flow."""
    mode: Literal["browser", "manual"] = "browser"
    cookie_data: Optional[str] = None


class SessionStatusResponse(BaseModel):
    """Structured response for UI session badge, health checks, and reconnection."""
    status: SessionStatus
    is_valid: bool = False
    is_authenticated: bool = False
    user_email: Optional[str] = None
    user_name: Optional[str] = None
    user_id: Optional[str] = None
    plan_type: Optional[str] = None
    expires_at: Optional[datetime] = None
    last_verified_at: Optional[datetime] = None
    message: str = ""
    message_fr: str = ""
    guidance: Optional[List[str]] = None
    action_required: Optional[str] = None
    reconnect_guidance: Optional[str] = None

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        # Synchronize is_authenticated and is_valid
        if "is_authenticated" in kwargs and "is_valid" not in kwargs:
            self.is_valid = bool(kwargs["is_authenticated"])
        elif "is_valid" in kwargs and "is_authenticated" not in kwargs:
            self.is_authenticated = bool(kwargs["is_valid"])


class ReconnectWizardStatus(str, Enum):
    """Status of interactive browser login wizard."""
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class ReconnectFlowStatus(BaseModel):
    """Status report for the reconnection process."""
    wizard_status: ReconnectWizardStatus = ReconnectWizardStatus.IDLE
    status: str = "IDLE"
    elapsed_seconds: int = 0
    timeout_seconds: int = 300
    message: str = ""
    session_status: SessionStatus = SessionStatus.MISSING

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        if "wizard_status" in kwargs and "status" not in kwargs:
            ws = kwargs["wizard_status"]
            self.status = ws.value if hasattr(ws, "value") else str(ws)
        elif "status" in kwargs and "wizard_status" not in kwargs:
            try:
                self.wizard_status = ReconnectWizardStatus(kwargs["status"])
            except Exception:
                pass
