"""
Hyperfocus Backend
==================
Main FastAPI application entry point.

Run with:
    uvicorn backend.main:app --reload --host 0.0.0.0 --port 8080
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from .services.llm_router import router as llm_router
from .services.llm_terminal_bridge import router as terminal_bridge_router
from .services.terminal_bridge_v2 import router as sandbox_router
from .acp.api import router as acp_router

from .config import settings
from .models import init_db
from .routes import artefacts_router, chat_router, shared_router, terminal_router, me_router, documents_router, rooms_router
from .services import chat_service, terminal_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    # Startup
    print(f"🚀 Starting {settings.app_name} v{settings.app_version}")
    print(f"   Environment: {settings.environment}")
    print(f"   vLLM endpoint: {settings.vllm_base_url}")

    # Initialize database
    await init_db()
    print("   ✓ Database initialized")

    # Check vLLM connection
    vllm_ok = await chat_service.check_health()
    if vllm_ok:
        print("   ✓ vLLM connected")
    else:
        print("   ⚠ vLLM not reachable (will retry on first request)")

    # Ensure artefacts directory exists
    settings.artefacts_root.mkdir(parents=True, exist_ok=True)
    print(f"   ✓ Artefacts root: {settings.artefacts_root}")

    yield

    # Shutdown
    print("👋 Shutting down...")
    await chat_service.close()
    await terminal_service.shutdown()


# Create app
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI-powered development environment with chat, terminal, and artefact management.",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url="/api/redoc" if settings.debug else None,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins if settings.is_production else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
# Exception Handlers
# ─────────────────────────────────────────────────────────────────────────────


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler."""
    if settings.debug:
        import traceback
        traceback.print_exc()

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": str(exc) if settings.debug else "Internal server error",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# Health & Status
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    vllm_ok = await chat_service.check_health()

    return {
        "status": "healthy" if vllm_ok else "degraded",
        "version": settings.app_version,
        "vllm_status": "connected" if vllm_ok else "disconnected",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api")
async def api_root():
    """API root - version info."""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/api/docs" if settings.debug else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Mount Routers
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(llm_router)
app.include_router(terminal_bridge_router)
app.include_router(sandbox_router)
app.include_router(acp_router, prefix="/api")
app.include_router(chat_router, prefix="/api")
app.include_router(artefacts_router, prefix="/api")
app.include_router(shared_router, prefix="/api")
app.include_router(terminal_router, prefix="/api")
app.include_router(me_router, prefix="/api")
app.include_router(documents_router, prefix="/api")
app.include_router(rooms_router, prefix="/api")


# ─────────────────────────────────────────────────────────────────────────────
# Static Files (Frontend) - Mount last
# ─────────────────────────────────────────────────────────────────────────────

# Uncomment when frontend is ready:
# app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
