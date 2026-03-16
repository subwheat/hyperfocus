"""
ACP Replay System
=================
Minimal deterministic replay engine for ACP lab.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict

from .models import RunTrace


class ReplayEngine:
    """Minimal replay engine for ACP traces."""

    async def replay_trace(self, trace: RunTrace) -> Dict[str, Any]:
        payload = trace.model_dump(mode="json")
        serialized = json.dumps(payload, sort_keys=True, default=str)
        replay_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()

        return {
            "run_id": payload.get("run_id"),
            "events_processed": len(payload.get("events", []) or []),
            "content": None,
            "claims": [],
            "evidence": [],
            "scores": {},
            "errors": [],
            "final_status": payload.get("status"),
            "replay_hash": replay_hash,
            "is_deterministic": True,
        }


replay_engine = ReplayEngine()
