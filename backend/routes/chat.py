"""
Hyperfocus Chat Routes - Multi-Model Support
=============================================
REST API endpoints for chat operations with DeepSeek, Claude, and Qwen support.
"""

import json
import os
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import AuthContext, CurrentWorkspace, check_rate_limit
from ..models import get_db
from ..schemas import (
    APIResponse,
    ChatCompletionResponse,
    ChatMessageResponse,
    ChatRequest,
    ChatSessionListResponse,
    ChatSessionResponse,
)
from ..services import ChatServiceError, chat_service
from ..services.artefacts import artefact_service
from ..services.artefact_extract import extract_text
from ..services.project_memory import (
    detect_project_key_from_messages,
    ensure_project_memory_current,
    prepend_project_memory_to_messages,
)

# Multi-model config
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "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")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "http://172.17.0.1:8001")
QWEN_MODEL = os.getenv("QWEN_MODEL", "Qwen/Qwen3-8B")

SYSTEM_PROMPT_HYPERFOCUS = """Tu t'appelles ACP Assistant.

MISSION:
Tu es l'assistant IA intégré à ACP.

POSITIONNEMENT PRODUIT:
- ACP Core = moteur technique central.
- Reality Check = wedge commercial principal.
- Bullshitometer = surface d'évaluation rhétorique, vérifiabilité, qualité des sources et signaux de bullshit.
- L'API ACP est la première surface d'intégration.
- Le MCP vient plus tard comme adaptateur alpha, jamais comme cœur du produit.

VERTICALE CIBLE:
content -> claims -> evidence -> report

PRIORITÉS:
- Stabiliser ACP Core sans casser les contrats existants.
- Finir le slice E2E content -> claims -> evidence -> report.
- Garantir replay, traçabilité, append-only et reproductibilité.
- Ne jamais produire d'assertion finale sans source_ref, span_ref ou unknown.
- Faire de Reality Check / Bullshitometer le premier produit démontrable.

RÔLE:
- Pair technique direct et lucide.
- Builder orienté exécution.
- Analyste qualité / preuve / cohérence.

LANGUE:
- Français par défaut. Anglais si demandé.

STYLE TDAH:
- Méthodique, par étapes courtes.
- "court" -> 2-6 lignes max.
- "pas à pas" -> une étape, attendre "ok".
- Highlight le prochain petit pas concret.
- Zéro people-pleasing.

RÈGLES:
- N'invente pas de commandes, résultats ou fichiers.
- Utilise les fichiers, artefacts, logs et captures comme source de vérité.
- Toute conclusion doit être traçable.
- Si tu as besoin du web, dis-le.

CONTEXTE ACP:
- Claims, evidence packets, scoring, reporting.
- Routing API-first.
- Qwen local par défaut, heavy cloud par policy seulement.
- Reality Check et Bullshitometer sont les surfaces produit prioritaires.
"""


router = APIRouter(prefix="/chat", tags=["Chat"])


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Model Functions
# ─────────────────────────────────────────────────────────────────────────────

async def call_claude(messages: list[dict], max_tokens: int = 8192, temperature: float = 0.7) -> dict:
    """Call Claude API with prompt caching"""
    system_msg = ""
    claude_messages = []
    for m in messages:
        if m["role"] == "system":
            system_msg = m["content"]
        else:
            claude_messages.append({"role": m["role"], "content": m["content"]})
    
    if not CLAUDE_API_KEY:
        raise ChatServiceError("Claude API key not configured")
    
    # Use prompt caching for system prompt (90% cost reduction on cache hits)
    system_with_cache = [
        {
            "type": "text",
            "text": system_msg,
            "cache_control": {"type": "ephemeral"}
        }
    ]
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            CLAUDE_API_URL,
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
                "content-type": "application/json"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": max_tokens,
                "system": system_with_cache,
                "messages": claude_messages,
                "temperature": temperature
            }
        )
        response.raise_for_status()
        data = response.json()
        
        return {
            "choices": [{"message": {"content": data["content"][0]["text"]}}],
            "usage": {
                "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
                "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
                "total_tokens": data.get("usage", {}).get("input_tokens", 0) + data.get("usage", {}).get("output_tokens", 0)
            }
        }


async def call_qwen(messages: list[dict], max_tokens: int = 8192, temperature: float = 0.7) -> dict:
    """Call Qwen via vLLM"""
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{QWEN_BASE_URL}/v1/chat/completions",
            json={
                "model": QWEN_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature
            }
        )
        response.raise_for_status()
        return response.json()


def truncate_messages_for_deepseek(messages: list[dict], max_chars: int = 28000) -> list[dict]:
    """Truncate messages to fit DeepSeek context window (~8000 tokens ≈ 28000 chars)"""
    total_chars = sum(len(m.get("content", "")) for m in messages)
    if total_chars <= max_chars:
        return messages
    
    # Keep system prompt intact, truncate older messages
    truncated = []
    chars_used = 0
    
    # First pass: keep system messages
    for m in messages:
        if m.get("role") == "system":
            truncated.append(m)
            chars_used += len(m.get("content", ""))
    
    # Second pass: add messages from most recent, skip if too long
    remaining = max_chars - chars_used
    non_system = [m for m in messages if m.get("role") != "system"]
    
    for m in reversed(non_system):
        msg_len = len(m.get("content", ""))
        if msg_len <= remaining:
            truncated.insert(len([x for x in truncated if x.get("role") == "system"]), m)
            remaining -= msg_len
        elif remaining > 500:  # Truncate long message
            truncated_content = m.get("content", "")[:remaining-100] + "\n[...truncated...]"
            truncated.insert(len([x for x in truncated if x.get("role") == "system"]), 
                           {"role": m["role"], "content": truncated_content})
            break
    
    return truncated


async def call_model(model: str, messages: list[dict], max_tokens: int = 8192, temperature: float = 0.7) -> dict:
    """Route to appropriate model"""
    if model == "claude":
        return await call_claude(messages, max_tokens, temperature)
    elif model == "qwen":
        return await call_qwen(messages, max_tokens, temperature)
    else:  # default deepseek - truncate to avoid 503
        truncated_messages = truncate_messages_for_deepseek(messages)
        return await chat_service.complete(
            messages=truncated_messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Chat Completions
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/completions", response_model=ChatCompletionResponse)
async def create_chat_completion(
    request: ChatRequest,
    auth: AuthContext = Depends(check_rate_limit),
    db: AsyncSession = Depends(get_db),
):
    """
    Create a chat completion with multi-model support.
    Supports: deepseek (default), claude, qwen
    """
    try:
        # Get or create session
        session = await chat_service.get_or_create_session(
            db, auth.workspace_id, request.session_id
        )

        # Get history if continuing session
        history = []
        if request.session_id:
            history = await chat_service.get_session_messages(db, session.id)

        # Build messages for LLM
        messages = chat_service._build_messages(
            user_messages=request.messages,
            history=history,
        )

        # Add system prompt
        if not messages or messages[0].get('role') != 'system':
            messages = [{'role': 'system', 'content': SYSTEM_PROMPT_HYPERFOCUS}] + messages

        # Inject persistent project memory
        try:
            project_key = detect_project_key_from_messages(getattr(request, "messages", []) or [])
            project_memory_text = await ensure_project_memory_current(
                db, auth.workspace_id, project_key
            )
            if project_memory_text:
                request.messages = prepend_project_memory_to_messages(
                    list(request.messages), project_memory_text, project_key
                )
                try:
                    first_msg = request.messages[0]
                    first_content = first_msg.get("content") if isinstance(first_msg, dict) else getattr(first_msg, "content", "")
                    print("DEBUG_PROJECT_MEMORY_PRESENT=", "[CONTEXTE PROJET SERVEUR:" in str(first_content))
                    print("DEBUG_PROJECT_MEMORY_HEAD=", str(first_content)[:900])
                except Exception as dbg_e:
                    print(f"DEBUG_PROJECT_MEMORY_LOG_FAILED: {dbg_e}")
        except Exception as e:
            print(f"⚠ project memory injection failed: {e}")

        # Inject artefacts context
        if getattr(request, "artefact_ids", None):
            parts = []
            for aid in request.artefact_ids:
                a = await artefact_service.get_by_id(db, aid, auth.workspace_id)
                if not a:
                    continue
                t = await extract_text(a, max_chars=20000)
                if not t:
                    continue
                parts.append(f"### {a.filename} ({a.mime_type})\n{t}")
            if parts:
                context = "DOCUMENTS FOURNIS (PJ) — utilise comme source de vérité.\n\n" + "\n\n---\n\n".join(parts)
                messages = messages[:1] + [{"role": "user", "content": context}] + messages[1:]

        # Save user message(s)
        for msg in request.messages:
            if msg.role == "user":
                await chat_service.save_message(
                    db, session.id, msg.role, msg.content
                )
                if not history and len(request.messages) == 1:
                    await chat_service.update_session_title(
                        db, session.id, msg.content
                    )

        # Get model from request (default: deepseek)
        model = getattr(request, "model", "deepseek") or "deepseek"
        
        # Calculate appropriate max_tokens
        max_tokens = request.max_tokens or 8192
        
        # Call the appropriate model
        result = await call_model(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=request.temperature or 0.7,
        )

        # Extract response
        choice = result.get("choices", [{}])[0]
        assistant_content = choice.get("message", {}).get("content", "")
        usage = result.get("usage")

        # Save assistant response
        assistant_msg = await chat_service.save_message(
            db,
            session.id,
            "assistant",
            assistant_content,
            token_count=usage.get("completion_tokens") if usage else None,
        )

        return ChatCompletionResponse(
            session_id=session.id,
            message=ChatMessageResponse(
                id=assistant_msg.id,
                role="assistant",
                content=assistant_content,
                created_at=assistant_msg.created_at,
                token_count=assistant_msg.token_count,
            ),
            usage=usage,
        )

    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"Model API error: {e.response.text[:200]}"
        )
    except ChatServiceError as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Chat error: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# Session Management
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sessions", response_model=ChatSessionListResponse)
async def list_sessions(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """List chat sessions for the current workspace."""
    sessions, total = await chat_service.list_sessions(
        db, auth.workspace_id, limit=limit, offset=offset
    )

    return ChatSessionListResponse(
        sessions=[
            ChatSessionResponse(
                id=s.id,
                title=s.title,
                created_at=s.created_at,
                updated_at=s.updated_at,
                message_count=len(s.messages) if s.messages else 0,
            )
            for s in sessions
        ],
        total=total,
    )


@router.get("/sessions/{session_id}", response_model=ChatSessionResponse)
async def get_session(
    session_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Get chat session details."""
    session = await chat_service.get_or_create_session(
        db, auth.workspace_id, session_id
    )
    if session.workspace_id != auth.workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = await chat_service.get_session_messages(db, session.id)

    return ChatSessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(messages),
    )


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageResponse])
async def get_session_messages(
    session_id: str,
    limit: int = Query(100, ge=1, le=500),
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a chat session."""
    session = await chat_service.get_or_create_session(
        db, auth.workspace_id, session_id
    )
    if session.workspace_id != auth.workspace_id:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = await chat_service.get_session_messages(db, session.id, limit=limit)

    return [
        ChatMessageResponse(
            id=m.id,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
            token_count=m.token_count,
        )
        for m in messages
    ]


@router.delete("/sessions/{session_id}", response_model=APIResponse)
async def delete_session(
    session_id: str,
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Delete a chat session."""
    success = await chat_service.delete_session(db, session_id, auth.workspace_id)
    if not success:
        raise HTTPException(status_code=404, detail="Session not found")

    return APIResponse(success=True)


@router.post("/sessions", response_model=ChatSessionResponse)
async def create_session(
    auth: AuthContext = CurrentWorkspace,
    db: AsyncSession = Depends(get_db),
):
    """Create a new chat session."""
    session = await chat_service.get_or_create_session(db, auth.workspace_id, None)

    return ChatSessionResponse(
        id=session.id,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=0,
    )
