"""
Hyperfocus Artefact Routes
==========================
REST API endpoints for artefact operations.
"""

import json
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, CurrentWorkspace
from ..models import AsyncSessionLocal, get_db
from ..schemas import (
    APIResponse,
    ArtefactCreate,
    ArtefactListResponse,
    ArtefactResponse,
    ShareLinkResponse,
)
from ..services import artefact_service
from ..services.project_memory import load_project_memory_text

router = APIRouter(prefix="/artefacts", tags=["Artefacts"])


# ─────────────────────────────────────────────────────────────────────────────
# Helper Functions
# ─────────────────────────────────────────────────────────────────────────────


def artefact_to_response(artefact, base_url: str = "") -> ArtefactResponse:
    """Convert artefact model to response schema."""
    tags = None
    if artefact.tags:
        try:
            tags = json.loads(artefact.tags)
        except json.JSONDecodeError:
            tags = None

    return ArtefactResponse(
        id=artefact.id,
        filename=artefact.filename,
        file_type=artefact.file_type,
        mime_type=artefact.mime_type,
        file_size=artefact.file_size,
        title=artefact.title,
        description=artefact.description,
        tags=tags,
        source=artefact.source,
        created_at=artefact.created_at,
        download_url=f"{base_url}/api/artefacts/{artefact.id}/download",
        preview_url=f"{base_url}/api/artefacts/{artefact.id}/preview",
    )


# ─────────────────────────────────────────────────────────────────────────────
# CRUD Operations
# ─────────────────────────────────────────────────────────────────────────────


@router.get("", response_model=ArtefactListResponse)
async def list_artefacts(
    file_type: Optional[str] = Query(None, description="Filter by file type"),
    source: Optional[str] = Query(None, description="Filter by source"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """
    List artefacts for the current workspace.
    
    Returns most recent first.
    """
    artefacts, total = await artefact_service.list_artefacts(
        db,
        auth.workspace_id,
        file_type=file_type,
        source=source,
        limit=limit,
        offset=offset,
    )

    return ArtefactListResponse(
        artefacts=[artefact_to_response(a) for a in artefacts],
        total=total,
    )


@router.post("", response_model=ArtefactResponse, status_code=status.HTTP_201_CREATED)
async def create_artefact(
    request: ArtefactCreate,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """
    Create an artefact from text content.
    
    For binary file uploads, use POST /artefacts/upload instead.
    """
    if not request.content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Content is required for text artefacts",
        )

    artefact = await artefact_service.create_from_content(
        db=db,
        workspace_id=auth.workspace_id,
        filename=request.filename,
        content=request.content,
        title=request.title,
        description=request.description,
        tags=request.tags,
        session_id=request.session_id,
        source=request.source,
    )

    return artefact_to_response(artefact)


@router.post(
    "/upload", response_model=ArtefactResponse, status_code=status.HTTP_201_CREATED
)
async def upload_artefact(
    file: UploadFile = File(...),
    title: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),  # JSON array as string
    session_id: Optional[str] = Form(None),
    project_key: Optional[str] = Form(None),
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Upload a binary file as an artefact."""
    # Parse tags
    parsed_tags = None
    if tags:
        try:
            parsed_tags = json.loads(tags)
        except json.JSONDecodeError:
            parsed_tags = None

    artefact = await artefact_service.create_from_upload(
        db=db,
        workspace_id=auth.workspace_id,
        filename=file.filename or "unnamed",
        file=file.file,
        title=title,
        description=description,
        tags=parsed_tags,
        session_id=session_id,
        source="upload",
        project_key=project_key,
    )

    return artefact_to_response(artefact)


@router.get("/{artefact_id}", response_model=ArtefactResponse)
async def get_artefact(
    artefact_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Get artefact metadata."""
    artefact = await artefact_service.get_by_id(db, artefact_id, auth.workspace_id)
    if not artefact:
        raise HTTPException(status_code=404, detail="Artefact not found")

    return artefact_to_response(artefact)


@router.delete("/{artefact_id}", response_model=APIResponse)
async def delete_artefact(
    artefact_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Delete an artefact."""
    artefact = await artefact_service.get_by_id(db, artefact_id, auth.workspace_id)
    if not artefact:
        raise HTTPException(status_code=404, detail="Artefact not found")

    success = await artefact_service.delete(db, artefact)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete artefact",
        )

    return APIResponse(success=True)


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox Import
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/from-sandbox", response_model=ArtefactResponse)
async def import_from_sandbox(
    filepath: str = Form(...),
    project_key: Optional[str] = Form(None),
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Import a file from sandbox workspace to artefacts."""
    import os
    
    # Map to host path - the sandbox workspace is mounted
    project_key = (project_key or "cresus").strip().lower()
    sandbox_workspace = f"/opt/philae/sandboxes/{project_key}/workspace"
    
    # Clean filepath
    clean_path = filepath.replace("/workspace/", "").replace("/workspace", "").lstrip("/")
    source_path = os.path.join(sandbox_workspace, clean_path)
    
    if not os.path.exists(source_path):
        raise HTTPException(status_code=404, detail=f"File not found: {filepath}")
    
    if os.path.isdir(source_path):
        raise HTTPException(status_code=400, detail="Cannot import directory")
    
    # Read file
    with open(source_path, 'rb') as f:
        file_content = f.read()
    
    filename = os.path.basename(clean_path)
    
    # Create artefact using upload method
    from io import BytesIO

    artefact = await artefact_service.create_from_upload(
        db=db,
        workspace_id=auth.workspace_id,
        filename=filename,
        file=BytesIO(file_content),
        source="sandbox",
        project_key=project_key,
    )
    
    return artefact_to_response(artefact)


# ─────────────────────────────────────────────────────────────────────────────
# File Access
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/{artefact_id}/download")
async def download_artefact(
    artefact_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Download artefact file."""
    artefact = await artefact_service.get_by_id(db, artefact_id, auth.workspace_id)
    if not artefact:
        raise HTTPException(status_code=404, detail="Artefact not found")

    file_path = await artefact_service.get_file_path(artefact)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=artefact.filename,
        media_type=artefact.mime_type,
    )


@router.get("/{artefact_id}/preview")
async def preview_artefact(
    artefact_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """
    Get artefact content for preview.
    
    Only works for text-based files.
    """
    artefact = await artefact_service.get_by_id(db, artefact_id, auth.workspace_id)
    if not artefact:
        raise HTTPException(status_code=404, detail="Artefact not found")

    content = await artefact_service.read_content(artefact)
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Preview not available for this file type",
        )

    # Return as plain text or JSON based on file type
    if artefact.file_type == "json":
        return Response(
            content=content,
            media_type="application/json",
        )
    else:
        return Response(
            content=content,
            media_type="text/plain; charset=utf-8",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Sharing
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/{artefact_id}/share", response_model=ShareLinkResponse)
async def create_share_link(
    artefact_id: str,
    expires_in_hours: int = Query(24, ge=1, le=720),  # Max 30 days
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Create a shareable link for an artefact."""
    artefact = await artefact_service.get_by_id(db, artefact_id, auth.workspace_id)
    if not artefact:
        raise HTTPException(status_code=404, detail="Artefact not found")

    share_token, expires_at = await artefact_service.create_share_link(
        db, artefact, expires_in_hours
    )

    return ShareLinkResponse(
        share_url=f"/api/shared/{share_token}",
        expires_at=expires_at,
    )


@router.delete("/{artefact_id}/share", response_model=APIResponse)
async def revoke_share_link(
    artefact_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Revoke share link for an artefact."""
    artefact = await artefact_service.get_by_id(db, artefact_id, auth.workspace_id)
    if not artefact:
        raise HTTPException(status_code=404, detail="Artefact not found")

    await artefact_service.revoke_share_link(db, artefact)

    return APIResponse(success=True)


# ─────────────────────────────────────────────────────────────────────────────
# Public Shared Access (no auth required)
# ─────────────────────────────────────────────────────────────────────────────



@router.get("/project-memory/{project_key}/preview")
async def preview_project_memory(
    project_key: str,
    auth: AuthContext = CurrentWorkspace,
):
    """Preview backend persistent project memory for current workspace."""
    content = load_project_memory_text(auth.workspace_id, project_key) or ""
    return {"project": project_key, "content": content}


# Note: This route is mounted at /api/shared in main.py, not under /artefacts
shared_router = APIRouter(prefix="/shared", tags=["Shared"])


@shared_router.get("/{share_token}")
async def get_shared_artefact(
    share_token: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Access a shared artefact (no auth required).
    
    Returns file download.
    """
    artefact = await artefact_service.get_by_share_token(db, share_token)
    if not artefact:
        raise HTTPException(status_code=404, detail="Shared link not found or expired")

    file_path = await artefact_service.get_file_path(artefact)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(
        path=file_path,
        filename=artefact.filename,
        media_type=artefact.mime_type,
    )


@shared_router.get("/{share_token}/info", response_model=ArtefactResponse)
async def get_shared_artefact_info(
    share_token: str,
    db: AsyncSession = Depends(get_db),
):
    """Get metadata for a shared artefact (no auth required)."""
    artefact = await artefact_service.get_by_share_token(db, share_token)
    if not artefact:
        raise HTTPException(status_code=404, detail="Shared link not found or expired")

    return artefact_to_response(artefact)
