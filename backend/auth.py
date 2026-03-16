"""
Hyperfocus Authentication Service
=================================
Phase 1: Simple API key authentication.
Phase 2: JWT tokens + multi-user.
"""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .models import AsyncSessionLocal, Workspace, generate_uuid

# ─────────────────────────────────────────────────────────────────────────────
# Security utilities
# ─────────────────────────────────────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def hash_api_key(api_key: str) -> str:
    """Hash API key for storage."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a secure API key."""
    return f"hf_{secrets.token_urlsafe(32)}"


def generate_share_token() -> str:
    """Generate a share token for artefacts."""
    return secrets.token_urlsafe(32)


# ─────────────────────────────────────────────────────────────────────────────
# JWT utilities (Phase 2)
# ─────────────────────────────────────────────────────────────────────────────


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(hours=settings.jwt_expiration_hours)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> Optional[dict]:
    """Decode and validate JWT token."""
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        return payload
    except JWTError:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Workspace management
# ─────────────────────────────────────────────────────────────────────────────


async def get_or_create_workspace(
    db: AsyncSession, api_key: str
) -> Workspace:
    """Get workspace by API key, or create if using default dev key."""
    api_key_hash = hash_api_key(api_key)

    # Try to find existing workspace
    result = await db.execute(
        select(Workspace).where(Workspace.api_key_hash == api_key_hash)
    )
    workspace = result.scalar_one_or_none()

    if workspace:
        if not workspace.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Workspace is deactivated",
            )
        return workspace

    # Accept the configured server API key in any mode and auto-create workspace
    if api_key == settings.api_key:
        workspace = Workspace(
            id=generate_uuid(),
            name="Default Workspace",
            api_key_hash=api_key_hash,
        )
        db.add(workspace)
        await db.commit()
        await db.refresh(workspace)
        return workspace

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI Dependencies
# ─────────────────────────────────────────────────────────────────────────────


class AuthContext:
    """Authentication context for requests."""

    def __init__(self, workspace: Workspace, api_key: str):
        self.workspace = workspace
        self.workspace_id = workspace.id
        self.api_key = api_key


async def get_current_workspace(
    request: Request,
    api_key: Optional[str] = Depends(api_key_header),
) -> AuthContext:
    """
    Dependency to get current authenticated workspace.

    Checks, in order:
    1. X-API-Key header
    2. Authorization Bearer token
    3. Reverse-proxy auth headers from Nginx/Authelia
    """
    # Trust upstream auth headers injected by Nginx/Authelia FIRST
    remote_user = (
        request.headers.get("Remote-User")
        or request.headers.get("X-Auth-User")
        or request.headers.get("Remote-Email")
    )
    remote_name = (
        request.headers.get("Remote-Name")
        or request.headers.get("Remote-User")
        or request.headers.get("X-Auth-User")
        or "Authelia User"
    )

    if remote_user:
        synthetic_api_key = f"authelia::{remote_user}"

        async with AsyncSessionLocal() as db:
            api_key_hash = hash_api_key(synthetic_api_key)
            result = await db.execute(
                select(Workspace).where(Workspace.api_key_hash == api_key_hash)
            )
            workspace = result.scalar_one_or_none()

            if workspace:
                if not workspace.is_active:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Workspace is deactivated",
                    )
            else:
                workspace = Workspace(
                    id=generate_uuid(),
                    name=remote_name[:255],
                    api_key_hash=api_key_hash,
                )
                db.add(workspace)
                await db.commit()
                await db.refresh(workspace)

            return AuthContext(workspace=workspace, api_key=synthetic_api_key)

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide X-API-Key header, Authorization Bearer token, or authenticate via Authelia.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate API key and get workspace
    async with AsyncSessionLocal() as db:
        workspace = await get_or_create_workspace(db, api_key)
        return AuthContext(workspace=workspace, api_key=api_key)


# Alias for cleaner dependency injection
CurrentWorkspace = Depends(get_current_workspace)


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiting (simple in-memory)
# ─────────────────────────────────────────────────────────────────────────────


class RateLimiter:
    """Simple in-memory rate limiter."""

    def __init__(self):
        self._requests: dict[str, list[datetime]] = {}

    def is_allowed(self, key: str) -> bool:
        """Check if request is allowed under rate limit."""
        now = datetime.now(timezone.utc)
        window_start = now - timedelta(seconds=settings.rate_limit_window_seconds)

        # Clean old entries
        if key in self._requests:
            self._requests[key] = [
                t for t in self._requests[key] if t > window_start
            ]
        else:
            self._requests[key] = []

        # Check limit
        if len(self._requests[key]) >= settings.rate_limit_requests:
            return False

        # Record request
        self._requests[key].append(now)
        return True

    def reset(self, key: str):
        """Reset rate limit for key."""
        self._requests.pop(key, None)


# Global rate limiter instance
rate_limiter = RateLimiter()


async def check_rate_limit(auth: AuthContext = CurrentWorkspace) -> AuthContext:
    """Dependency to check rate limit."""
    if not rate_limiter.is_allowed(auth.workspace_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please slow down.",
        )
    return auth
