"""
Transport layer modes and audit records (R4).
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from app.models.base import BaseModel, ConfigDict, Field


class TransportMode(str, Enum):
    """Available transport mechanisms."""
    DIRECT_HTTP = "DIRECT_HTTP"      # Mode 1: curl_cffi / Direct HTTP
    BROWSER_FETCH = "BROWSER_FETCH"  # Mode 2: Playwright CDP browser context fetch
    UI_DRIVEN = "UI_DRIVEN"          # Mode 3: DOM automation fallback


class TransportAuditRecord(BaseModel):
    """Execution telemetry for each transport operation."""
    id: Optional[int] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    operation: str = ""
    attempted_mode: TransportMode = TransportMode.BROWSER_FETCH
    resolved_mode: TransportMode = TransportMode.BROWSER_FETCH
    target_id: Optional[str] = None
    status_code: Optional[int] = None
    latency_ms: float = 0.0
    success: bool = True
    error_message: Optional[str] = None
    fallback_triggered: bool = False

    model_config = ConfigDict(from_attributes=True)


class TransportStatusReport(BaseModel):
    """Current health and configuration of the transport router."""
    current_mode: TransportMode = TransportMode.BROWSER_FETCH
    forced_mode: Optional[TransportMode] = None
    available_modes: List[TransportMode] = Field(
        default_factory=lambda: [
            TransportMode.DIRECT_HTTP,
            TransportMode.BROWSER_FETCH,
            TransportMode.UI_DRIVEN,
        ]
    )
    last_successful_mode: Optional[TransportMode] = None
    consecutive_failures: Dict[str, int] = Field(default_factory=dict)
    circuit_breaker_open: bool = False
