from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import re

MEM_ROOT = Path(os.environ.get("HF_MEMORY_ROOT", "/data/memory"))

def _safe(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "_", s.strip())
    return s[:120] if s else "item"

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def ensure_layout() -> None:
    (MEM_ROOT / "identity").mkdir(parents=True, exist_ok=True)
    (MEM_ROOT / "knowledge").mkdir(parents=True, exist_ok=True)
    (MEM_ROOT / "sessions").mkdir(parents=True, exist_ok=True)
    (MEM_ROOT / "artifacts" / "user").mkdir(parents=True, exist_ok=True)
    (MEM_ROOT / "artifacts" / "agent").mkdir(parents=True, exist_ok=True)
