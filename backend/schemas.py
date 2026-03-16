"""
Hyperfocus API Schemas
======================
Pydantic models for request/response validation.
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ConfigDict


# ─────────────────────────────────────────────────────────────────────────────
# Common
# ─────────────────────────────────────────────────────────────────────────────


class APIResponse(BaseModel):
    """Standard API response wrapper."""

    success: bool = True
    data: Any = None
    error: Optional[str] = None


class PaginatedResponse(BaseModel):
    """Paginated list response."""

    items: list[Any]
    total: int
    page: int = 1
    page_size: int = 50
    has_more: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Chat
# ─────────────────────────────────────────────────────────────────────────────


class ChatMessageInput(BaseModel):
    """Single message in chat request."""

    role: Literal["user", "assistant", "system"] = "user"
    content: str = Field(..., min_length=1, max_length=100000)


class ChatRequest(BaseModel):
    """Chat completion request."""

    messages: list[ChatMessageInput] = Field(..., min_length=1)
    session_id: Optional[str] = None
    stream: bool = False
    max_tokens: Optional[int] = Field(None, ge=1, le=16384)
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    artefact_ids: Optional[list[str]] = None  # IDs des PJ à injecter
    model: Optional[str] = "deepseek"  # Model to use: deepseek, claude, qwen


class ChatMessageResponse(BaseModel):
    """Single message in response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    role: str
    content: str
    created_at: datetime
    token_count: Optional[int] = None


class ChatSessionResponse(BaseModel):
    """Chat session metadata."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    title: Optional[str]
    created_at: datetime
    updated_at: datetime
    message_count: int = 0


class ChatCompletionResponse(BaseModel):
    """Chat completion response."""

    session_id: str
    message: ChatMessageResponse
    usage: Optional[dict] = None


class ChatSessionListResponse(BaseModel):
    """List of chat sessions."""

    sessions: list[ChatSessionResponse]
    total: int


# ─────────────────────────────────────────────────────────────────────────────
# Artefacts
# ─────────────────────────────────────────────────────────────────────────────


class ArtefactCreate(BaseModel):
    """Create artefact request (for file upload or generation)."""

    filename: str = Field(..., min_length=1, max_length=255)
    content: Optional[str] = None  # For text-based artefacts
    title: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    tags: Optional[list[str]] = None
    session_id: Optional[str] = None
    source: Literal["chat", "upload", "terminal"] = "chat"


class ArtefactResponse(BaseModel):
    """Artefact metadata response."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    filename: str
    file_type: str
    mime_type: str
    file_size: int
    title: Optional[str]
    description: Optional[str]
    tags: Optional[list[str]] = None
    source: str
    created_at: datetime
    download_url: Optional[str] = None
    preview_url: Optional[str] = None


class ArtefactListResponse(BaseModel):
    """List of artefacts."""

    artefacts: list[ArtefactResponse]
    total: int


class ShareLinkResponse(BaseModel):
    """Share link for artefact."""

    share_url: str
    expires_at: Optional[datetime]


# ─────────────────────────────────────────────────────────────────────────────
# Terminal (Phase 1.1+)
# ─────────────────────────────────────────────────────────────────────────────


class TerminalSessionCreate(BaseModel):
    """Create terminal session."""

    cols: int = Field(80, ge=40, le=500)
    rows: int = Field(24, ge=10, le=200)


class TerminalSessionResponse(BaseModel):
    """Terminal session info."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    started_at: datetime
    is_active: bool
    websocket_url: str


class TerminalInput(BaseModel):
    """Terminal input message."""

    type: Literal["input", "resize"] = "input"
    data: Optional[str] = None
    cols: Optional[int] = None
    rows: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────
# Health & Status
# ─────────────────────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Health check response."""

    status: Literal["healthy", "degraded", "unhealthy"]
    version: str
    vllm_status: Literal["connected", "disconnected", "unknown"]
    database_status: Literal["connected", "disconnected"]
    timestamp: datetime


class WorkspaceInfo(BaseModel):
    """Current workspace info."""

    id: str
    name: str
    created_at: datetime
    artefact_count: int
    session_count: int


# Shared Room
class SharedRoomMessageCreate(BaseModel):
    role: Literal["user", "assistant"] = "user"
    author_key: Literal["julien", "nico", "assistant"]
    author_label: Optional[str] = None
    message_type: Literal["discussion", "ask-acp", "decision", "todo"] = "discussion"
    content: str = Field(..., min_length=1, max_length=100000)
    model: Optional[str] = None


class SharedRoomMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    room_id: str
    role: str
    author_key: str
    author_label: str
    message_type: str
    content: str
    model: Optional[str] = None
    created_at: datetime


class PresenceHeartbeat(BaseModel):
    participant_key: Literal["julien", "nico", "assistant"]
    participant_label: Optional[str] = None
    client_id: str = Field(..., min_length=4, max_length=128)


class SharedRoomPresenceResponse(BaseModel):
    participant_key: str
    participant_label: str
    client_id: str
    last_seen_at: datetime
    is_online: bool


class SharedRoomStateResponse(BaseModel):
    room_id: str
    room_name: str
    messages: list[SharedRoomMessageResponse]
    online: list[SharedRoomPresenceResponse]
    last_message_id: int = 0
