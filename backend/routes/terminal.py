"""
Hyperfocus Terminal Routes
==========================
REST API + WebSocket endpoints for terminal operations.
"""

import asyncio
import json

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, CurrentWorkspace, hash_api_key
from ..models import AsyncSessionLocal, Workspace, get_db
from ..schemas import (
    APIResponse,
    TerminalSessionCreate,
    TerminalSessionResponse,
)
from ..services import TerminalServiceError, terminal_service

router = APIRouter(prefix="/terminal", tags=["Terminal"])


@router.get("/status")
async def terminal_status():
    """Check if terminal service is enabled."""
    return {
        "enabled": terminal_service.enabled,
        "max_sessions": 5,
    }


@router.post("/sessions", response_model=TerminalSessionResponse, status_code=201)
async def create_terminal_session(
    request: TerminalSessionCreate = TerminalSessionCreate(),
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Create a new terminal session."""
    if not terminal_service.enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Terminal service is disabled",
        )

    try:
        session = await terminal_service.create_session(
            db=db,
            workspace_id=auth.workspace_id,
            cols=request.cols,
            rows=request.rows,
        )

        return TerminalSessionResponse(
            id=session.id,
            started_at=session.started_at,
            is_active=session.is_active,
            websocket_url=f"/api/terminal/sessions/{session.id}/ws",
        )

    except TerminalServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )


@router.get("/sessions")
async def list_terminal_sessions(
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """List active terminal sessions."""
    sessions = await terminal_service.list_sessions(db, auth.workspace_id)
    return {
        "sessions": [
            TerminalSessionResponse(
                id=s.id,
                started_at=s.started_at,
                is_active=s.is_active,
                websocket_url=f"/api/terminal/sessions/{s.id}/ws",
            )
            for s in sessions
        ]
    }


@router.delete("/sessions/{session_id}", response_model=APIResponse)
async def close_terminal_session(
    session_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Close a terminal session."""
    success = await terminal_service.close_session(db, session_id, auth.workspace_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")
    return APIResponse(success=True)


@router.websocket("/sessions/{session_id}/ws")
async def terminal_websocket(
    websocket: WebSocket,
    session_id: str,
):
    """
    WebSocket endpoint for terminal I/O.
    
    Protocol:
    - Client sends: {"type": "input", "data": "..."} or {"type": "resize", "cols": N, "rows": N}
    - Server sends: {"type": "output", "data": "..."} or {"type": "exit", "code": N}
    
    Auth via query param: ?token=API_KEY
    """
    # Auth check
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    # Validate token
    async with AsyncSessionLocal() as db:
        api_key_hash = hash_api_key(token)
        result = await db.execute(
            select(Workspace).where(Workspace.api_key_hash == api_key_hash)
        )
        workspace = result.scalar_one_or_none()

        if not workspace:
            await websocket.close(code=4003, reason="Invalid token")
            return

        # Check session belongs to workspace
        session = await terminal_service.get_session(db, session_id, workspace.id)
        if not session:
            await websocket.close(code=4004, reason="Session not found")
            return

    # Accept connection
    await websocket.accept()

    # Check PTY exists
    pty = terminal_service.get_pty(session_id)
    if not pty:
        await websocket.send_json({"type": "error", "message": "PTY not available"})
        await websocket.close(code=4005, reason="PTY not available")
        return

    async def read_pty():
        """Read from PTY and send to WebSocket."""
        while terminal_service.is_session_alive(session_id):
            data = terminal_service.read_from_session(session_id, timeout=0.05)
            if data:
                try:
                    await websocket.send_json({
                        "type": "output",
                        "data": data.decode("utf-8", errors="replace"),
                    })
                except Exception:
                    break
            await asyncio.sleep(0.01)

        # Session ended
        pty = terminal_service.get_pty(session_id)
        exit_code = pty.get_exit_code() if pty else None
        try:
            await websocket.send_json({"type": "exit", "code": exit_code})
        except Exception:
            pass

    async def write_pty():
        """Read from WebSocket and write to PTY."""
        try:
            while True:
                message = await websocket.receive_json()
                msg_type = message.get("type", "input")

                if msg_type == "input":
                    data = message.get("data", "")
                    if data:
                        terminal_service.write_to_session(
                            session_id, data.encode("utf-8")
                        )

                elif msg_type == "resize":
                    cols = message.get("cols", 80)
                    rows = message.get("rows", 24)
                    terminal_service.resize_session(session_id, cols, rows)

        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    # Run both tasks
    read_task = asyncio.create_task(read_pty())
    write_task = asyncio.create_task(write_pty())

    try:
        await asyncio.gather(read_task, write_task, return_exceptions=True)
    finally:
        read_task.cancel()
        write_task.cancel()
        try:
            await websocket.close()
        except Exception:
            pass
