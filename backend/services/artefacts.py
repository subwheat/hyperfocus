"""
Hyperfocus Artefact Service
===========================
File storage and metadata management for generated artefacts.
Phase 1: Local filesystem
Phase 2: S3/Object storage
"""

import hashlib
import json
import mimetypes
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Optional

import aiofiles
import aiofiles.os
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Artefact, generate_uuid
from ..auth import generate_share_token


# Initialize mimetypes
mimetypes.init()


class ArtefactService:
    """Service for artefact operations."""

    def __init__(self):
        self.root = settings.artefacts_root
        # Ensure root exists
        self.root.mkdir(parents=True, exist_ok=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Path Management (Security)
    # ─────────────────────────────────────────────────────────────────────────

    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent path traversal."""
        # Remove any path components
        filename = os.path.basename(filename)
        # Remove or replace dangerous characters
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
        # Ensure not empty
        if not filename or filename in (".", ".."):
            filename = "unnamed"
        return filename[:255]  # Limit length

    def _get_workspace_path(self, workspace_id: str) -> Path:
        """Get workspace directory path."""
        # Validate workspace_id format (UUID)
        if not re.match(r"^[a-f0-9\-]{36}$", workspace_id, re.IGNORECASE):
            raise ValueError("Invalid workspace ID format")
        return self.root / workspace_id

    def _get_artefact_path(
        self,
        workspace_id: str,
        artefact_id: str,
        filename: str,
    ) -> Path:
        """
        Get full path for artefact storage.
        Structure: /{workspace_id}/{YYYY}/{MM}/{artefact_id}/{filename}
        """
        now = datetime.now(timezone.utc)
        workspace_path = self._get_workspace_path(workspace_id)
        safe_filename = self._sanitize_filename(filename)

        return (
            workspace_path
            / now.strftime("%Y")
            / now.strftime("%m")
            / artefact_id
            / safe_filename
        )

    def _get_relative_path(self, full_path: Path) -> str:
        """Get path relative to artefacts root."""
        return str(full_path.relative_to(self.root))

    def _resolve_path(self, relative_path: str) -> Path:
        """
        Resolve relative path to full path with security checks.
        Prevents path traversal attacks.
        """
        full_path = (self.root / relative_path).resolve()
        # Ensure path is still under root
        if not str(full_path).startswith(str(self.root.resolve())):
            raise ValueError("Path traversal detected")
        return full_path

    # ─────────────────────────────────────────────────────────────────────────
    # File Operations
    # ─────────────────────────────────────────────────────────────────────────

    async def _compute_sha256(self, file_path: Path) -> str:
        """Compute SHA256 hash of file."""
        sha256 = hashlib.sha256()
        async with aiofiles.open(file_path, "rb") as f:
            while chunk := await f.read(8192):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _detect_mime_type(self, filename: str) -> str:
        """Detect MIME type from filename."""
        mime_type, _ = mimetypes.guess_type(filename)
        return mime_type or "application/octet-stream"

    def _get_file_type(self, filename: str) -> str:
        """Extract file extension as type."""
        ext = Path(filename).suffix.lower()
        return ext[1:] if ext else "unknown"

    # ─────────────────────────────────────────────────────────────────────────
    # CRUD Operations
    # ─────────────────────────────────────────────────────────────────────────

    async def create_from_content(
        self,
        db: AsyncSession,
        workspace_id: str,
        filename: str,
        content: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        source: str = "chat",
        project_key: Optional[str] = None,
    ) -> Artefact:
        """Create artefact from text content."""
        artefact_id = generate_uuid()
        safe_filename = self._sanitize_filename(filename)
        file_path = self._get_artefact_path(workspace_id, artefact_id, safe_filename)

        # Create directory structure
        await aiofiles.os.makedirs(file_path.parent, exist_ok=True)

        # Write content
        async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
            await f.write(content)

        # Get file info
        file_size = len(content.encode("utf-8"))
        sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        mime_type = self._detect_mime_type(safe_filename)
        file_type = self._get_file_type(safe_filename)

        # Create database record
        artefact = Artefact(
            id=artefact_id,
            workspace_id=workspace_id,
            session_id=session_id,
            filename=safe_filename,
            file_type=file_type,
            mime_type=mime_type,
            file_size=file_size,
            file_path=self._get_relative_path(file_path),
            sha256=sha256,
            title=title,
            description=description,
            tags=json.dumps(tags) if tags else None,
            source=source,
            project_key=project_key,
        )
        db.add(artefact)
        await db.commit()
        await db.refresh(artefact)

        return artefact

    async def create_from_upload(
        self,
        db: AsyncSession,
        workspace_id: str,
        filename: str,
        file: BinaryIO,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        source: str = "upload",
        project_key: Optional[str] = None,
    ) -> Artefact:
        """Create artefact from uploaded file."""
        artefact_id = generate_uuid()
        safe_filename = self._sanitize_filename(filename)
        file_path = self._get_artefact_path(workspace_id, artefact_id, safe_filename)

        # Create directory structure
        await aiofiles.os.makedirs(file_path.parent, exist_ok=True)

        # Write file
        file_size = 0
        sha256 = hashlib.sha256()

        async with aiofiles.open(file_path, "wb") as f:
            while chunk := file.read(8192):
                await f.write(chunk)
                file_size += len(chunk)
                sha256.update(chunk)

        mime_type = self._detect_mime_type(safe_filename)
        file_type = self._get_file_type(safe_filename)

        # Create database record
        artefact = Artefact(
            id=artefact_id,
            workspace_id=workspace_id,
            session_id=session_id,
            filename=safe_filename,
            file_type=file_type,
            mime_type=mime_type,
            file_size=file_size,
            file_path=self._get_relative_path(file_path),
            sha256=sha256.hexdigest(),
            title=title,
            description=description,
            tags=json.dumps(tags) if tags else None,
            source=source,
            project_key=project_key,
        )
        db.add(artefact)
        await db.commit()
        await db.refresh(artefact)

        return artefact

    async def get_by_id(
        self,
        db: AsyncSession,
        artefact_id: str,
        workspace_id: str,
    ) -> Optional[Artefact]:
        """Get artefact by ID."""
        result = await db.execute(
            select(Artefact).where(
                Artefact.id == artefact_id,
                Artefact.workspace_id == workspace_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_share_token(
        self,
        db: AsyncSession,
        share_token: str,
    ) -> Optional[Artefact]:
        """Get artefact by share token (for public access)."""
        result = await db.execute(
            select(Artefact).where(
                Artefact.share_token == share_token,
            )
        )
        artefact = result.scalar_one_or_none()

        if artefact and artefact.share_expires_at:
            if artefact.share_expires_at < datetime.now(timezone.utc):
                return None  # Expired

        return artefact

    async def list_artefacts(
        self,
        db: AsyncSession,
        workspace_id: str,
        file_type: Optional[str] = None,
        source: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Artefact], int]:
        """List artefacts for workspace."""
        # Build query
        query = select(Artefact).where(Artefact.workspace_id == workspace_id)

        if file_type:
            query = query.where(Artefact.file_type == file_type)
        if source:
            query = query.where(Artefact.source == source)

        # Get total count
        count_query = select(func.count(Artefact.id)).where(
            Artefact.workspace_id == workspace_id
        )
        if file_type:
            count_query = count_query.where(Artefact.file_type == file_type)
        if source:
            count_query = count_query.where(Artefact.source == source)

        count_result = await db.execute(count_query)
        total = count_result.scalar() or 0

        # Get artefacts (most recent first)
        query = query.order_by(Artefact.created_at.desc()).offset(offset).limit(limit)
        result = await db.execute(query)
        artefacts = list(result.scalars().all())

        return artefacts, total

    async def get_file_path(self, artefact: Artefact) -> Path:
        """Get resolved file path for artefact."""
        return self._resolve_path(artefact.file_path)

    async def read_content(self, artefact: Artefact) -> Optional[str]:
        """Read text content of artefact."""
        file_path = await self.get_file_path(artefact)
        if not file_path.exists():
            return None

        # Only read text files
        if not artefact.mime_type.startswith("text/") and artefact.file_type not in (
            "md",
            "json",
            "csv",
            "txt",
            "py",
            "js",
            "html",
            "css",
            "yaml",
            "yml",
            "toml",
            "xml",
        ):
            return None

        try:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                return await f.read()
        except UnicodeDecodeError:
            return None

    async def delete(
        self,
        db: AsyncSession,
        artefact: Artefact,
    ) -> bool:
        """Delete artefact and its file."""
        try:
            # Delete file
            file_path = await self.get_file_path(artefact)
            if file_path.exists():
                await aiofiles.os.remove(file_path)

            # Try to remove empty parent directories
            parent = file_path.parent
            while parent != self.root:
                try:
                    parent.rmdir()
                    parent = parent.parent
                except OSError:
                    break

            # Delete database record
            await db.delete(artefact)
            await db.commit()
            return True
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Sharing
    # ─────────────────────────────────────────────────────────────────────────

    async def create_share_link(
        self,
        db: AsyncSession,
        artefact: Artefact,
        expires_in_hours: int = 24,
    ) -> tuple[str, datetime]:
        """Create or update share link for artefact."""
        share_token = generate_share_token()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

        artefact.share_token = share_token
        artefact.share_expires_at = expires_at

        await db.commit()
        await db.refresh(artefact)

        return share_token, expires_at

    async def revoke_share_link(
        self,
        db: AsyncSession,
        artefact: Artefact,
    ):
        """Revoke share link for artefact."""
        artefact.share_token = None
        artefact.share_expires_at = None
        await db.commit()


# Singleton instance
artefact_service = ArtefactService()
