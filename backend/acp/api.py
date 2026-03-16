"""
ACP Core API
============
Core endpoints for ACP operations.
"""

from typing import Any, Dict, List, Optional
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from ..models import get_db
from .models import RunTrace, RunConfig, RunStatus, EventType
from .repositories import RunRepository, PolicyRepository
from .replay import replay_engine
from .policy_runtime import PolicyRuntime, DEFAULT_POLICY_CONFIG


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class CreateRunRequest(BaseModel):
    """Request to create new Run."""
    content_source: str
    policy_pack: str = "default-v1.0.0"
    model_routing: Dict[str, Any] = {}
    max_claims: int = 50
    require_sources: bool = True


class RunResponse(BaseModel):
    """Response with Run details."""
    run_id: str
    status: str
    config: Dict[str, Any]
    created_at: datetime
    completed_at: Optional[datetime] = None
    events_count: int


class ReplayResponse(BaseModel):
    """Response with replay results."""
    run_id: str
    replay_hash: str
    is_deterministic: bool
    events_processed: int
    content: Optional[Dict[str, Any]] = None
    claims: List[Dict[str, Any]]
    evidence: List[Dict[str, Any]]
    scores: Dict[str, Any]
    errors: List[Any]


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/acp", tags=["ACP Core"])


@router.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
async def create_run(
    request: CreateRunRequest,
    db: AsyncSession = Depends(get_db)
) -> RunResponse:
    """Create new ACP Run."""
    
    # Create RunTrace
    config = RunConfig(
        content_source=request.content_source,
        policy_pack=request.policy_pack,
        model_routing=request.model_routing,
        max_claims=request.max_claims,
        require_sources=request.require_sources
    )
    
    trace = RunTrace(config=config)
    
    # Add initial event
    trace = trace.add_event(
        event_type=EventType.RUN_STARTED,
        data={"config": config.model_dump()},
        metadata={"created_by": "api"}
    )
    
    # Store in database
    repo = RunRepository(db)
    run = await repo.create_run(trace)
    
    return RunResponse(
        run_id=run.run_id,
        status=run.status,
        config=run.config,
        created_at=run.created_at,
        completed_at=run.completed_at,
        events_count=len(trace.events)
    )


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_db)
) -> RunResponse:
    """Get Run by ID."""
    
    repo = RunRepository(db)
    run = await repo.get_run(run_id)
    
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found"
        )
    
    trace = run.to_run_trace()
    
    return RunResponse(
        run_id=run.run_id,
        status=run.status,
        config=run.config,
        created_at=run.created_at,
        completed_at=run.completed_at,
        events_count=len(trace.events)
    )


@router.get("/runs", response_model=List[RunResponse])
async def list_runs(
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db)
) -> List[RunResponse]:
    """List recent runs."""
    
    repo = RunRepository(db)
    runs = await repo.list_runs(limit=limit, offset=offset)
    
    return [
        RunResponse(
            run_id=run.run_id,
            status=run.status,
            config=run.config,
            created_at=run.created_at,
            completed_at=run.completed_at,
            events_count=len(run.to_run_trace().events)
        )
        for run in runs
    ]


@router.post("/runs/{run_id}/replay", response_model=ReplayResponse)
async def replay_run(
    run_id: str,
    db: AsyncSession = Depends(get_db)
) -> ReplayResponse:
    """Replay Run execution deterministically."""
    
    repo = RunRepository(db)
    run = await repo.get_run(run_id)
    
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found"
        )
    
    # Perform replay
    trace = run.to_run_trace()
    replay_result = await replay_engine.replay_trace(trace)
    
    return ReplayResponse(
        run_id=run_id,
        replay_hash=replay_result["replay_hash"],
        is_deterministic=replay_result["is_deterministic"],
        events_processed=replay_result["events_processed"],
        content=replay_result.get("content"),
        claims=replay_result.get("claims", []),
        evidence=replay_result.get("evidence", []),
        scores=replay_result.get("scores", {}),
        errors=replay_result.get("errors", [])
    )


@router.post("/runs/{run_id}/score")
async def score_run(
    run_id: str,
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """Re-score Run deterministically."""
    
    repo = RunRepository(db)
    run = await repo.get_run(run_id)
    
    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id} not found"
        )
    
    # Get replay state
    trace = run.to_run_trace()
    replay_result = await replay_engine.replay_trace(trace)
    
    # Compute basic scores
    claims = replay_result.get("claims", [])
    evidence = replay_result.get("evidence", [])
    
    scores = {
        "claim_count": len(claims),
        "evidence_count": len(evidence),
        "verify_ratio": len([c for c in claims if c.get("source_ref")]) / max(len(claims), 1),
        "unsupported_claim_rate": len([c for c in claims if not c.get("source_ref") and not c.get("unknown")]) / max(len(claims), 1),
        "scored_at": datetime.utcnow().isoformat()
    }
    
    return {
        "run_id": run_id,
        "scores": scores,
        "replay_hash": replay_result["replay_hash"]
    }


@router.get("/policy-packs/{pack_id}")
async def get_policy_pack(
    pack_id: str,
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """Get PolicyPack configuration."""
    
    repo = PolicyRepository(db)
    policy_pack = await repo.get_policy_pack(pack_id)
    
    if not policy_pack:
        # Return default policy if not found
        return {
            "pack_id": "default-v1.0.0",
            "version": "1.0.0",
            "config": DEFAULT_POLICY_CONFIG
        }
    
    return {
        "pack_id": policy_pack.pack_id,
        "version": policy_pack.version,
        "config": policy_pack.config
    }


@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """ACP Core health check."""
    return {
        "status": "healthy",
        "service": "acp-core",
        "version": "1.3.0",
        "timestamp": datetime.utcnow().isoformat()
    }
