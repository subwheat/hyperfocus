"""
ACP Repositories
================
Append-only data access layer.
"""

from typing import List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime

from .models import Run, PolicyPack, RunTrace, RunStatus


class RunRepository:
    """Repository for Run operations - append-only only."""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_run(self, run_trace: RunTrace) -> Run:
        """Create new Run - append-only."""
        run = Run(
            run_id=run_trace.run_id,
            config=run_trace.config.model_dump(mode="json"),
            status=run_trace.status.value,
            trace=run_trace.model_dump(mode="json"),
            created_at=run_trace.created_at
        )
        self.session.add(run)
        await self.session.commit()
        await self.session.refresh(run)
        return run
    
    async def update_run_trace(self, run_id: str, new_trace: RunTrace) -> Run:
        """Update Run trace - append-only (no deletion)."""
        result = await self.session.execute(
            select(Run).where(Run.run_id == run_id)
        )
        run = result.scalar_one()
        
        # Update trace and status
        run.trace = new_trace.model_dump(mode="json")
        run.status = new_trace.status.value
        if new_trace.completed_at:
            run.completed_at = new_trace.completed_at
        
        await self.session.commit()
        await self.session.refresh(run)
        return run
    
    async def get_run(self, run_id: str) -> Optional[Run]:
        """Get Run by ID."""
        result = await self.session.execute(
            select(Run).where(Run.run_id == run_id)
        )
        return result.scalar_one_or_none()
    
    async def list_runs(self, limit: int = 100, offset: int = 0) -> List[Run]:
        """List recent runs."""
        result = await self.session.execute(
            select(Run)
            .order_by(Run.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())


class PolicyRepository:
    """Repository for PolicyPack operations - immutable."""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_policy_pack(self, pack_id: str, version: str, config: dict) -> PolicyPack:
        """Create new PolicyPack - immutable."""
        policy_pack = PolicyPack(
            pack_id=pack_id,
            version=version,
            config=config
        )
        self.session.add(policy_pack)
        await self.session.commit()
        await self.session.refresh(policy_pack)
        return policy_pack
    
    async def get_policy_pack(self, pack_id: str) -> Optional[PolicyPack]:
        """Get PolicyPack by ID."""
        result = await self.session.execute(
            select(PolicyPack).where(PolicyPack.pack_id == pack_id)
        )
        return result.scalar_one_or_none()
    
    async def list_policy_packs(self) -> List[PolicyPack]:
        """List all policy packs."""
        result = await self.session.execute(select(PolicyPack))
        return list(result.scalars().all())
