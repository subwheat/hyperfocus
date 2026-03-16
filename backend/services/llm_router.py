"""
================================================================================
                    LLM ROUTER — Multi-Model with Shared Context
================================================================================

Provides a unified interface for DeepSeek (local), Claude (API), and Qwen3 (local)
with a shared conversation context that persists across model switches.

Author: Julien Tournier | Date: February 2026
================================================================================
"""

import os
import json
import httpx
import asyncio
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import logging
import sqlite3
from contextlib import contextmanager
from enum import Enum

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("LLM_ROUTER")

# ============================================================================
# CONFIGURATION
# ============================================================================

class LLMConfig:
    # DeepSeek (vLLM local)
    VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://172.17.0.1:8000")
    DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-ai/deepseek-coder-6.7b-instruct")
    
    # Claude API
    CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "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")
    CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
    CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
    
    # Qwen3 (vLLM local - separate port or same)
    QWEN_BASE_URL = os.getenv("QWEN_BASE_URL", "http://172.17.0.1:8001")
    QWEN_MODEL = os.getenv("QWEN_MODEL", "Qwen/Qwen3-8B")
    
    # Shared context
    MAX_CONTEXT_MESSAGES = 50
    MAX_CONTEXT_TOKENS = 8000  # Conservative limit for all models
    
    # Database
    DB_PATH = Path(os.getenv("DB_PATH", "/data/db/hyperfocus.db"))
    
    # System prompts
    SYSTEM_PROMPT = """Tu es ACP Assistant, un assistant IA spécialisé pour le développement, l'architecture produit et la recherche appliquée autour de ACP.

IDENTITÉ :
- Ton nom est ACP Assistant
- Tu es intégré à ACP Core
- Tu connais la verticale ACP : content -> claims -> evidence -> report
- Tu priorises Reality Check et Bullshitometer comme surfaces produit

STYLE :
- Réponses concises, structurées
- Adapté TDAH : étapes courtes, pas de murs de texte
- Français par défaut, anglais si demandé

MISSION :
- Assister sur le code, l'architecture, la recherche
- Stabiliser le pipeline evidence-bound
- Créer des artefacts quand pertinent
- Signaler clairement ce qui est prouvé, incertain ou hors scope

CONTEXTE ACP :
- claims extraction
- evidence packets
- scoring / disagreement / source quality
- report builder reproductible
- API-first, MCP ensuite
"""


# ============================================================================
# DATA STRUCTURES
# ============================================================================

class ModelType(str, Enum):
    DEEPSEEK = "deepseek"
    CLAUDE = "claude"
    QWEN = "qwen"


@dataclass
class Message:
    role: str  # "user", "assistant", "system"
    content: str
    model: Optional[str] = None  # Which model generated this (for assistant messages)
    timestamp: datetime = field(default_factory=datetime.now)
    tools_used: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "role": self.role,
            "content": self.content,
            "model": self.model,
            "timestamp": self.timestamp.isoformat(),
            "tools_used": self.tools_used
        }
    
    @classmethod
    def from_dict(cls, d: Dict) -> "Message":
        return cls(
            role=d["role"],
            content=d["content"],
            model=d.get("model"),
            timestamp=datetime.fromisoformat(d["timestamp"]) if "timestamp" in d else datetime.now(),
            tools_used=d.get("tools_used", [])
        )


@dataclass
class SharedContext:
    """Shared conversation context across all LLM instances"""
    session_id: str
    messages: List[Message] = field(default_factory=list)
    artefact_context: List[str] = field(default_factory=list)  # Artefact IDs in context
    created_at: datetime = field(default_factory=datetime.now)
    last_model: Optional[str] = None
    
    def add_message(self, msg: Message):
        self.messages.append(msg)
        if msg.role == "assistant":
            self.last_model = msg.model
        # Trim if too many messages
        if len(self.messages) > LLMConfig.MAX_CONTEXT_MESSAGES:
            # Keep system message if present, trim oldest
            if self.messages[0].role == "system":
                self.messages = [self.messages[0]] + self.messages[-(LLMConfig.MAX_CONTEXT_MESSAGES-1):]
            else:
                self.messages = self.messages[-LLMConfig.MAX_CONTEXT_MESSAGES:]
    
    def get_messages_for_api(self, include_system: bool = True) -> List[Dict]:
        """Format messages for API calls (OpenAI/Anthropic format)"""
        result = []
        for msg in self.messages:
            if msg.role == "system" and not include_system:
                continue
            result.append({"role": msg.role, "content": msg.content})
        return result
    
    def get_context_summary(self) -> str:
        """Generate a summary for context handoff between models"""
        if len(self.messages) < 3:
            return ""
        
        # Get last few exchanges
        recent = self.messages[-6:]  # Last 3 exchanges
        summary_parts = []
        
        for msg in recent:
            if msg.role == "user":
                summary_parts.append(f"User: {msg.content[:200]}...")
            elif msg.role == "assistant":
                model_tag = f"[{msg.model}]" if msg.model else ""
                summary_parts.append(f"Assistant{model_tag}: {msg.content[:200]}...")
        
        return "\n".join(summary_parts)


# ============================================================================
# DATABASE LAYER
# ============================================================================

class ContextDatabase:
    """Persists shared context to SQLite"""
    
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS shared_contexts (
        session_id TEXT PRIMARY KEY,
        messages_json TEXT NOT NULL,
        artefact_context_json TEXT DEFAULT '[]',
        last_model TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    
    CREATE INDEX IF NOT EXISTS idx_contexts_updated ON shared_contexts(updated_at);
    """
    
    def __init__(self, db_path: Path = None):
        self.db_path = db_path or LLMConfig.DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
    
    def _init_schema(self):
        with self._connect() as conn:
            conn.executescript(self.SCHEMA)
    
    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
    
    def save_context(self, ctx: SharedContext):
        messages_json = json.dumps([m.to_dict() for m in ctx.messages])
        artefacts_json = json.dumps(ctx.artefact_context)
        
        with self._connect() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO shared_contexts 
                (session_id, messages_json, artefact_context_json, last_model, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ctx.session_id, messages_json, artefacts_json, ctx.last_model, 
                  ctx.created_at.isoformat(), datetime.now().isoformat()))
    
    def load_context(self, session_id: str) -> Optional[SharedContext]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM shared_contexts WHERE session_id = ?", 
                (session_id,)
            ).fetchone()
            
            if not row:
                return None
            
            messages = [Message.from_dict(m) for m in json.loads(row["messages_json"])]
            artefacts = json.loads(row["artefact_context_json"])
            
            return SharedContext(
                session_id=session_id,
                messages=messages,
                artefact_context=artefacts,
                created_at=datetime.fromisoformat(row["created_at"]),
                last_model=row["last_model"]
            )
    
    def delete_context(self, session_id: str):
        with self._connect() as conn:
            conn.execute("DELETE FROM shared_contexts WHERE session_id = ?", (session_id,))


# ============================================================================
# LLM CLIENTS
# ============================================================================

class DeepSeekClient:
    """Client for local DeepSeek via vLLM (OpenAI-compatible API)"""
    
    def __init__(self):
        self.base_url = LLMConfig.VLLM_BASE_URL
        self.model = LLMConfig.DEEPSEEK_MODEL
    
    async def chat(self, messages: List[Dict], temperature: float = 0.7, max_tokens: int = 8192) -> Dict:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
            )
            response.raise_for_status()
            data = response.json()
            
            return {
                "content": data["choices"][0]["message"]["content"],
                "model": self.model,
                "usage": data.get("usage", {})
            }
    
    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/v1/models")
                return response.status_code == 200
        except:
            return False


class ClaudeClient:
    """Client for Anthropic Claude API"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or LLMConfig.CLAUDE_API_KEY
        self.model = LLMConfig.CLAUDE_MODEL
        self.api_url = LLMConfig.CLAUDE_API_URL
    
    async def chat(self, messages: List[Dict], system: str = None, 
                   temperature: float = 0.7, max_tokens: int = 4096) -> Dict:
        """
        Send messages to Claude API.
        Note: Claude uses a different format - system is separate from messages.
        """
        # Separate system message from conversation
        system_content = system or LLMConfig.SYSTEM_PROMPT
        api_messages = []
        
        for msg in messages:
            if msg["role"] == "system":
                system_content = msg["content"]
            else:
                api_messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
        
        
        # Ensure messages alternate user/assistant
        cleaned_messages = self._ensure_alternating(api_messages)
        
        # Retry logic for rate limits (429)
        max_retries = 3
        retry_delay = 10  # seconds between retries
        
        for attempt in range(max_retries):
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    self.api_url,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": self.model,
                        "max_tokens": max_tokens,
                        "system": system_content,
                        "messages": cleaned_messages,
                        "temperature": temperature
                    }
                )
                
                # Handle rate limit (429) with retry
                if response.status_code == 429:
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (attempt + 1)  # Exponential backoff
                        logger.warning(f"Claude API rate limit (429). Retry {attempt + 1}/{max_retries} in {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        logger.error("Claude API rate limit exceeded after all retries")
                        raise Exception("Claude API rate limit exceeded. Please wait a moment and try again.")
                
                if response.status_code != 200:
                    error_body = response.text
                    logger.error(f"Claude API error {response.status_code}: {error_body}")
                    raise Exception(f"Claude API error: {response.status_code} - {error_body}")
                
                data = response.json()
                
                # Extract text from response
                content = ""
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        content += block.get("text", "")
                
                return {
                    "content": content,

                "model": self.model,
                "usage": data.get("usage", {})
            }
    
    def _ensure_alternating(self, messages: List[Dict]) -> List[Dict]:
        """Ensure messages alternate between user and assistant"""
        if not messages:
            return messages
        
        result = []
        last_role = None
        
        for msg in messages:
            role = msg["role"]
            
            # Skip if same role as last (merge or skip)
            if role == last_role:
                if role == "user":
                    # Merge user messages
                    result[-1]["content"] += "\n\n" + msg["content"]
                continue
            
            # First message must be user
            if not result and role != "user":
                continue
            
            result.append(msg)
            last_role = role
        
        # Must end with user message for API call
        if result and result[-1]["role"] != "user":
            result = result[:-1]
        
        return result
    
    async def health_check(self) -> bool:
        """Check if API key is valid"""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(
                    self.api_url,
                    headers={
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": self.model,
                        "max_tokens": 10,
                        "messages": [{"role": "user", "content": "ping"}]
                    }
                )
                return response.status_code == 200
        except:
            return False


class QwenClient:
    """Client for local Qwen via vLLM (OpenAI-compatible API)"""
    
    def __init__(self):
        self.base_url = LLMConfig.QWEN_BASE_URL
        self.model = LLMConfig.QWEN_MODEL
    
    async def chat(self, messages: List[Dict], temperature: float = 0.7, max_tokens: int = 8192) -> Dict:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens
                }
            )
            response.raise_for_status()
            data = response.json()
            
            return {
                "content": data["choices"][0]["message"]["content"],
                "model": self.model,
                "usage": data.get("usage", {})
            }
    
    async def health_check(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/v1/models")
                return response.status_code == 200
        except:
            return False


# ============================================================================
# LLM ROUTER
# ============================================================================

class LLMRouter:
    """
    Routes requests to appropriate LLM while maintaining shared context.
    Handles model switching with context preservation.
    """
    
    def __init__(self, db: ContextDatabase = None):
        self.db = db or ContextDatabase()
        self.deepseek = DeepSeekClient()
        self.claude = ClaudeClient()
        self.qwen = QwenClient()
        self._contexts: Dict[str, SharedContext] = {}
    
    def get_client(self, model: ModelType):
        """Get the appropriate client for a model"""
        if model == ModelType.DEEPSEEK:
            return self.deepseek
        elif model == ModelType.CLAUDE:
            return self.claude
        elif model == ModelType.QWEN:
            return self.qwen
        else:
            raise ValueError(f"Unknown model: {model}")
    
    def get_or_create_context(self, session_id: str) -> SharedContext:
        """Get existing context or create new one"""
        # Check memory cache
        if session_id in self._contexts:
            return self._contexts[session_id]
        
        # Try loading from DB
        ctx = self.db.load_context(session_id)
        if ctx:
            self._contexts[session_id] = ctx
            return ctx
        
        # Create new context with system message
        ctx = SharedContext(session_id=session_id)
        ctx.add_message(Message(role="system", content=LLMConfig.SYSTEM_PROMPT))
        self._contexts[session_id] = ctx
        return ctx
    
    async def chat(
        self,
        session_id: str,
        user_message: str,
        model: ModelType = ModelType.DEEPSEEK,
        artefact_context: List[str] = None,
        features: Dict[str, bool] = None,
        temperature: float = 0.7
    ) -> Dict[str, Any]:
        """
        Main chat method - routes to appropriate model while maintaining shared context.
        """
        ctx = self.get_or_create_context(session_id)
        features = features or {}
        
        # Update artefact context if provided
        if artefact_context is not None:
            ctx.artefact_context = artefact_context
        
        # Build enhanced user message with context
        enhanced_message = user_message
        
        # Add artefact content to message if available
        if ctx.artefact_context:
            artefact_content = await self._get_artefact_content(ctx.artefact_context)
            if artefact_content:
                enhanced_message = f"[CONTEXT - Referenced files]\n{artefact_content}\n\n[USER MESSAGE]\n{user_message}"
        
        # Add user message to context
        ctx.add_message(Message(role="user", content=user_message))
        
        # Check if model changed - add context handoff note
        if ctx.last_model and ctx.last_model != model.value:
            handoff_note = f"[Contexte transféré depuis {ctx.last_model}]"
            logger.info(f"Model switch: {ctx.last_model} -> {model.value}")
        
        # Get messages for API
        messages = ctx.get_messages_for_api(include_system=(model != ModelType.CLAUDE))
        
        # If enhanced message, replace last user message
        if enhanced_message != user_message:
            messages[-1]["content"] = enhanced_message
        
        # Call appropriate client
        client = self.get_client(model)
        tools_used = []
        
        try:
            if model == ModelType.CLAUDE:
                result = await client.chat(
                    messages=messages,
                    system=LLMConfig.SYSTEM_PROMPT,
                    temperature=temperature
                )
            else:
                result = await client.chat(
                    messages=messages,
                    temperature=temperature
                )
            
            # Add assistant response to context
            assistant_msg = Message(
                role="assistant",
                content=result["content"],
                model=model.value,
                tools_used=tools_used
            )
            ctx.add_message(assistant_msg)
            
            # Save context
            self.db.save_context(ctx)
            
            return {
                "session_id": session_id,
                "message": {
                    "role": "assistant",
                    "content": result["content"]
                },
                "model": model.value,
                "usage": result.get("usage", {}),
                "tools_used": tools_used,
                "context_length": len(ctx.messages)
            }
            
        except Exception as e:
            logger.error(f"Chat error with {model.value}: {e}")
            raise
    
    async def _get_artefact_content(self, artefact_ids: List[str]) -> str:
        """Fetch content of artefacts for context injection"""
        # This would be implemented to fetch from artefacts storage
        # For now, return empty - will be integrated with artefacts service
        # TODO: Integrate with artefacts database
        return ""
    
    async def get_model_status(self) -> Dict[str, bool]:
        """Check health of all models"""
        results = await asyncio.gather(
            self.deepseek.health_check(),
            self.claude.health_check(),
            self.qwen.health_check(),
            return_exceptions=True
        )
        
        return {
            "deepseek": results[0] if isinstance(results[0], bool) else False,
            "claude": results[1] if isinstance(results[1], bool) else False,
            "qwen": results[2] if isinstance(results[2], bool) else False
        }
    
    def clear_context(self, session_id: str):
        """Clear a session's context"""
        if session_id in self._contexts:
            del self._contexts[session_id]
        self.db.delete_context(session_id)


# ============================================================================
# FASTAPI ROUTES
# ============================================================================

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

router = APIRouter(prefix="/llm", tags=["llm"])

# Singleton router instance
_llm_router: Optional[LLMRouter] = None

def get_llm_router() -> LLMRouter:
    global _llm_router
    if _llm_router is None:
        _llm_router = LLMRouter()
    return _llm_router


class ChatRequest(BaseModel):
    messages: List[Dict[str, str]]
    session_id: Optional[str] = None
    model: str = "deepseek"
    features: Dict[str, bool] = Field(default_factory=dict)
    context: List[str] = Field(default_factory=list)  # Artefact IDs
    temperature: float = 0.7


class ChatResponse(BaseModel):
    session_id: str
    message: Dict[str, str]
    model: str
    usage: Dict[str, Any] = Field(default_factory=dict)
    tools_used: List[str] = Field(default_factory=list)
    context_length: int = 0


@router.post("/chat/completions", response_model=ChatResponse)
async def chat_deepseek(request: ChatRequest, llm: LLMRouter = Depends(get_llm_router)):
    """DeepSeek chat endpoint (default)"""
    return await _handle_chat(request, ModelType.DEEPSEEK, llm)


@router.post("/chat/claude", response_model=ChatResponse)
async def chat_claude(request: ChatRequest, llm: LLMRouter = Depends(get_llm_router)):
    """Claude API chat endpoint"""
    return await _handle_chat(request, ModelType.CLAUDE, llm)


@router.post("/chat/qwen", response_model=ChatResponse)
async def chat_qwen(request: ChatRequest, llm: LLMRouter = Depends(get_llm_router)):
    """Qwen chat endpoint"""
    return await _handle_chat(request, ModelType.QWEN, llm)


async def _handle_chat(request: ChatRequest, model: ModelType, llm: LLMRouter) -> ChatResponse:
    """Common chat handler for all models"""
    import uuid
    
    session_id = request.session_id or str(uuid.uuid4())
    
    # Get last user message
    user_message = ""
    for msg in reversed(request.messages):
        if msg.get("role") == "user":
            user_message = msg.get("content", "")
            break
    
    if not user_message:
        raise HTTPException(status_code=400, detail="No user message found")
    
    try:
        result = await llm.chat(
            session_id=session_id,
            user_message=user_message,
            model=model,
            artefact_context=request.context,
            features=request.features,
            temperature=request.temperature
        )
        return ChatResponse(**result)
    except Exception as e:
        logger.error(f"Chat error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/llm/status")
async def get_llm_status(llm: LLMRouter = Depends(get_llm_router)):
    """Get status of all LLM backends"""
    return await llm.get_model_status()


@router.delete("/chat/context/{session_id}")
async def clear_context(session_id: str, llm: LLMRouter = Depends(get_llm_router)):
    """Clear a session's context"""
    llm.clear_context(session_id)
    return {"status": "cleared", "session_id": session_id}


# ============================================================================
# DEMO / TEST
# ============================================================================

async def demo():
    """Quick demo/test"""
    router = LLMRouter()
    
    print("=" * 60)
    print("LLM Router Demo - Shared Context")
    print("=" * 60)
    
    # Check model status
    status = await router.get_model_status()
    print(f"\nModel Status: {status}")
    
    # Test with DeepSeek
    session_id = "test-session-001"
    
    print("\n--- DeepSeek ---")
    result = await router.chat(
        session_id=session_id,
        user_message="Bonjour! Explique-moi ce qu'est ROSETTA en une phrase.",
        model=ModelType.DEEPSEEK
    )
    print(f"DeepSeek: {result['message']['content'][:200]}...")
    
    # Switch to Claude (same context)
    print("\n--- Claude (même contexte) ---")
    result = await router.chat(
        session_id=session_id,
        user_message="Et qu'est-ce que le Competition Index (CI)?",
        model=ModelType.CLAUDE
    )
    print(f"Claude: {result['message']['content'][:200]}...")
    
    print(f"\nContext length: {result['context_length']} messages")


if __name__ == "__main__":
    asyncio.run(demo())
