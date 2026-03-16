from __future__ import annotations

from typing import Any, Dict


class BaseScorer:
    name = "base"

    async def score(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return {"score": 0.0, "details": {}}


class VerifyRatioScorer(BaseScorer):
    name = "verify_ratio"

    async def score(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        claims = payload.get("claims", []) or []
        if not claims:
            return {"score": 0.0, "details": {"verified": 0, "total": 0}}

        verified = 0
        for c in claims:
            if isinstance(c, dict) and (c.get("source_ref") or c.get("span_ref")):
                verified += 1

        return {
            "score": verified / len(claims),
            "details": {"verified": verified, "total": len(claims)},
        }


class SourceQualityScorer(BaseScorer):
    name = "source_quality"

    async def score(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        evidence = payload.get("evidence", []) or []
        return {"score": 1.0 if evidence else 0.0, "details": {"evidence_count": len(evidence)}}


verify_ratio_scorer = VerifyRatioScorer()
source_quality_scorer = SourceQualityScorer()
