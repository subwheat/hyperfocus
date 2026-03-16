"""
Hyperfocus API Routes
=====================
"""

from .chat import router as chat_router
from .artefacts import router as artefacts_router, shared_router
from .terminal import router as terminal_router
from .me import router as me_router
from .documents import router as documents_router
from .rooms import router as rooms_router

__all__ = [
    "chat_router",
    "artefacts_router",
    "shared_router",
    "terminal_router",
    "me_router",
    "documents_router",
    "rooms_router",
]
