"""
ACP Core Models
===============
Immutable trace models for append-only Claims Processing.
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ..models import Base


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class RunStatus(str, Enum):
    """Status of a Run execution."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class EventType(str, Enum):
    """Types of events in RunTrace."""
    RUN_STARTED = "run_started"
    CONTENT_FETCHED = "content_fetched"
    CLAIMS_EXTRACTED = "claims_extracted"
    EVIDENCE_RETRIEVED = "evidence_retrieved"
    SCORED = "scored"
    POLICY_APPLIED = "policy_applied"
    RUN_COMPLETED = "run_completed"
    ERROR = "error"


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic Schemas
# ─────────────────────────────────────────────────────────────────────────────

class RunConfig(BaseModel):
    """Configuration for a Run."""
    content_source: str
    policy_pack: str = "default-v1.0.0"
    model_routing: Dict[str, Any] = Field(default_factory=dict)
    max_claims: int = 50
    require_sources: bool = True


class TraceEvent(BaseModel):
    """Single event in RunTrace - immutable."""
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    event_type: EventType
    data: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunTrace(BaseModel):
    """Complete immutable trace of a Run execution."""
    run_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    config: RunConfig
    status: RunStatus = RunStatus.PENDING
    events: List[TraceEvent] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    
    def add_event(self, event_type: EventType, data: Dict[str, Any], metadata: Dict[str, Any] = None) -> "RunTrace":
        """Add event to trace - returns new RunTrace (immutable)."""
        new_event = TraceEvent(
            event_type=event_type,
            data=data,
            metadata=metadata or {}
        )
        new_events = self.events + [new_event]
        return self.model_copy(update={"events": new_events})


# ─────────────────────────────────────────────────────────────────────────────
# SQLAlchemy Models
# ─────────────────────────────────────────────────────────────────────────────

class Run(Base):
    """Persistent Run record - append-only."""
    __tablename__ = "acp_runs"
    
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    config: Mapped[Dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.PENDING)
    trace: Mapped[Dict[str, Any]] = mapped_column(JSON)  # Full RunTrace as JSON
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    
    def to_run_trace(self) -> RunTrace:
        """Convert to RunTrace Pydantic model."""
        return RunTrace.model_validate(self.trace)


class PolicyPack(Base):
    """Versioned policy configuration - immutable."""
    __tablename__ = "acp_policy_packs"
    
    pack_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # e.g., "default-v1.0.0"
    version: Mapped[str] = mapped_column(String(20))
    config: Mapped[Dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
