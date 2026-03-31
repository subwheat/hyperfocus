"""
Hyperfocus Chat Service
=======================
Proxy to vLLM with session management.
Supports streaming and non-streaming responses.
"""

import json
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ChatMessage, ChatSession, generate_uuid
from ..schemas import ChatMessageInput, ChatMessageResponse

SYSTEM_PROMPT_HYPERFOCUS = '''Tu t'appelles ACP Assistant.

Mission:
- Assister Julien sur ACP, Reality Check et Bullshitometer.
- Agir comme un pair humain: direct, clair, lucide, tu dis quand c'est bancal.
- Prioriser le chemin critique: content -> claims -> evidence -> report.

Langue:
- Français par défaut. Anglais seulement si demandé explicitement.

Style TDAH:
- Méthodique, par étapes.
- Si je dis "court", tu fais court.
- Si je dis "pas à pas", tu attends mon "ok" entre les étapes.

Règles:
- N'invente pas de commandes, résultats ou fichiers.
- Utilise les fichiers fournis comme source de vérité.
- Aucune assertion finale sans source, span ou unknown.
'''



class ChatService:
    """Service for chat operations."""

    def __init__(self):
        self.client = httpx.AsyncClient(
            base_url=settings.vllm_base_url,
            timeout=httpx.Timeout(settings.vllm_timeout, connect=10.0),
        )

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

    # ─────────────────────────────────────────────────────────────────────────
    # Session Management
    # ─────────────────────────────────────────────────────────────────────────

    async def get_or_create_session(
        self,
        db: AsyncSession,
        workspace_id: str,
        session_id: Optional[str] = None,
    ) -> ChatSession:
        """Get existing session or create new one."""
        if session_id:
            result = await db.execute(
                select(ChatSession).where(
                    ChatSession.id == session_id,
                    ChatSession.workspace_id == workspace_id,
                    ChatSession.is_active == True,
                )
            )
            session = result.scalar_one_or_none()
            if session:
                return session

        # Create new session
        session = ChatSession(
            id=generate_uuid(),
            workspace_id=workspace_id,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session

    async def get_session_messages(
        self,
        db: AsyncSession,
        session_id: str,
        limit: int = 50,
    ) -> list[ChatMessage]:
        """Get messages for a session."""
        result = await db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def save_message(
        self,
        db: AsyncSession,
        session_id: str,
        role: str,
        content: str,
        token_count: Optional[int] = None,
    ) -> ChatMessage:
        """Save a message to the database."""
        message = ChatMessage(
            id=generate_uuid(),
            session_id=session_id,
            role=role,
            content=content,
            token_count=token_count,
        )
        db.add(message)

        # Update session timestamp
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if session:
            session.updated_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(message)
        return message

    async def update_session_title(
        self,
        db: AsyncSession,
        session_id: str,
        first_message: str,
    ):
        """Auto-generate session title from first message."""
        result = await db.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        session = result.scalar_one_or_none()
        if session and not session.title:
            # Truncate to reasonable title length
            title = first_message[:100].strip()
            if len(first_message) > 100:
                title += "..."
            session.title = title
            await db.commit()

    async def list_sessions(
        self,
        db: AsyncSession,
        workspace_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[ChatSession], int]:
        """List sessions for workspace."""
        # Get total count
        count_result = await db.execute(
            select(func.count(ChatSession.id)).where(
                ChatSession.workspace_id == workspace_id,
                ChatSession.is_active == True,
            )
        )
        total = count_result.scalar() or 0

        # Get sessions
        result = await db.execute(
            select(ChatSession)
            .where(
                ChatSession.workspace_id == workspace_id,
                ChatSession.is_active == True,
            )
            .order_by(ChatSession.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        sessions = list(result.scalars().all())

        return sessions, total

    async def delete_session(
        self,
        db: AsyncSession,
        session_id: str,
        workspace_id: str,
    ) -> bool:
        """Soft delete a session."""
        result = await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.workspace_id == workspace_id,
            )
        )
        session = result.scalar_one_or_none()
        if session:
            session.is_active = False
            await db.commit()
            return True
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # vLLM Communication
    # ─────────────────────────────────────────────────────────────────────────

    def _build_messages(
        self,
        user_messages: list[ChatMessageInput],
        history: list[ChatMessage],
        system_prompt: Optional[str] = None,
    ) -> list[dict]:
        """Build messages array for vLLM request."""
        messages = []

        # System prompt
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # History from database
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})

        # New user messages
        for msg in user_messages:
            messages.append({"role": msg.role, "content": msg.content})

        return messages


    async def complete(

        self,

        messages: list[dict],

        max_tokens: Optional[int] = None,

        temperature: Optional[float] = None,

        stream: bool = False,

    ) -> dict:

        """

        Send completion request to vLLM (non-streaming).

        """

        # --- Hyperfocus system prompt ---
        # Keep an existing system prompt if one is already present.
        messages = [m for m in (messages or []) if isinstance(m, dict)]
        if not messages or messages[0].get("role") != "system":
            messages = [{"role": "system", "content": SYSTEM_PROMPT_HYPERFOCUS}] + messages
        # -----------------------------------


        payload = {

            "model": settings.vllm_model,

            "messages": messages,

            "max_tokens": max_tokens or settings.vllm_max_tokens,

            "stream": False,

        }

        if temperature is not None:

            payload["temperature"] = temperature


        try:

            response = await self.client.post(

                "/v1/chat/completions",

                json=payload,

            )

            response.raise_for_status()

            return response.json()

        except httpx.HTTPStatusError as e:

            raise ChatServiceError(

                f"vLLM error: {e.response.status_code} url={e.request.url} body={e.response.text[:200]}"

            ) from e

        except httpx.RequestError as e:

            raise ChatServiceError(f"Connection error: {str(e)}") from e




    async def complete_stream(
        self,
        messages: list[dict],
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Send streaming completion request to vLLM.
        Yields content chunks as they arrive.
        """
        # --- Hyperfocus system prompt ---
        # Keep an existing system prompt if one is already present.
        messages = [m for m in (messages or []) if isinstance(m, dict)]
        if not messages or messages[0].get("role") != "system":
            messages = [{"role":"system","content": SYSTEM_PROMPT_HYPERFOCUS}] + messages
        # -----------------------------------

        payload = {
            "model": settings.vllm_model,
            "messages": messages,
            "max_tokens": max_tokens or settings.vllm_max_tokens,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature

        try:
            async with self.client.stream(
                "POST",
                "/v1/chat/completions",
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
        except httpx.HTTPStatusError as e:
            raise ChatServiceError(f"vLLM error: {e.response.status_code} url={e.request.url} body={e.response.text[:200]}") from e
        except httpx.RequestError as e:
            raise ChatServiceError(f"Connection error: {str(e)}") from e

    async def check_health(self) -> bool:
        """Check if vLLM is reachable."""
        try:
            response = await self.client.get("/health", timeout=5.0)
            return response.status_code == 200
        except Exception:
            # Try models endpoint as fallback
            try:
                response = await self.client.get("/v1/models", timeout=5.0)
                return response.status_code == 200
            except Exception:
                return False


class ChatServiceError(Exception):
    """Exception for chat service errors."""

    pass


# Singleton instance
chat_service = ChatService()
