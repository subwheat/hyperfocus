"""
ACP Policy Runtime
==================
Policy enforcement and decision engine.
"""

from typing import Any, Dict, List, Optional
from datetime import datetime
import json

from .models import RunTrace, TraceEvent, EventType


class PolicyDecision:
    """Result of policy evaluation."""
    
    def __init__(self, action: str, reason: str, metadata: Dict[str, Any] = None):
        self.action = action
        self.reason = reason
        self.metadata = metadata or {}
        self.timestamp = datetime.utcnow()


class PolicyRuntime:
    """Runtime for policy evaluation and enforcement."""
    
    def __init__(self, policy_config: Dict[str, Any]):
        self.config = policy_config
        self.decisions: List[PolicyDecision] = []
    
    async def evaluate_content_policy(self, content: Dict[str, Any]) -> PolicyDecision:
        """Evaluate content fetching policy."""
        # Check content source allowlist
        source = content.get("source", "")
        allowed_sources = self.config.get("allowed_sources", [])
        
        if allowed_sources and not any(allowed in source for allowed in allowed_sources):
            return PolicyDecision(
                action="REJECT",
                reason=f"Source not in allowlist: {source}",
                metadata={"source": source}
            )
        
        # Check content size limits
        content_size = len(content.get("raw_text", ""))
        max_size = self.config.get("max_content_size", 1_000_000)  # 1MB default
        
        if content_size > max_size:
            return PolicyDecision(
                action="TRUNCATE",
                reason=f"Content too large: {content_size} > {max_size}",
                metadata={"size": content_size, "limit": max_size}
            )
        
        return PolicyDecision(action="ALLOW", reason="Content passed all checks")
    
    async def evaluate_model_routing_policy(self, context: Dict[str, Any]) -> PolicyDecision:
        """Evaluate which model to use based on policy."""
        routing_config = self.config.get("model_routing", {})
        
        # Default to local model
        preferred_model = "local_qwen"
        escalation_reasons = []
        
        # Check escalation conditions
        content_length = context.get("content_length", 0)
        if content_length > routing_config.get("heavy_threshold", 50000):
            preferred_model = "heavy_cloud"
            escalation_reasons.append("content_too_long")
        
        # Check complexity indicators
        has_images = context.get("has_images", False)
        if has_images and routing_config.get("vision_model"):
            preferred_model = routing_config["vision_model"]
            escalation_reasons.append("vision_required")
        
        # Check disagreement history
        disagreement_rate = context.get("recent_disagreement_rate", 0.0)
        if disagreement_rate > routing_config.get("disagreement_threshold", 0.3):
            preferred_model = "heavy_cloud"
            escalation_reasons.append("high_disagreement")
        
        return PolicyDecision(
            action="ROUTE",
            reason=f"Route to {preferred_model}: {', '.join(escalation_reasons) or 'default'}",
            metadata={
                "model": preferred_model,
                "escalation_reasons": escalation_reasons
            }
        )
    
    async def evaluate_claims_policy(self, claims: List[Dict[str, Any]]) -> PolicyDecision:
        """Evaluate claims extraction policy."""
        max_claims = self.config.get("max_claims", 50)
        require_sources = self.config.get("require_sources", True)
        
        if len(claims) > max_claims:
            return PolicyDecision(
                action="TRUNCATE",
                reason=f"Too many claims: {len(claims)} > {max_claims}",
                metadata={"claim_count": len(claims), "limit": max_claims}
            )
        
        # Check source requirements
        if require_sources:
            unsourced_claims = [
                claim for claim in claims 
                if not claim.get("source_ref") and not claim.get("unknown", False)
            ]
            
            if unsourced_claims:
                return PolicyDecision(
                    action="REJECT",
                    reason=f"Claims without sources: {len(unsourced_claims)}",
                    metadata={"unsourced_count": len(unsourced_claims)}
                )
        
        return PolicyDecision(action="ALLOW", reason="Claims passed policy checks")
    
    async def evaluate_scoring_policy(self, scores: Dict[str, Any]) -> PolicyDecision:
        """Evaluate scoring results against policy."""
        score_thresholds = self.config.get("score_thresholds", {})
        
        # Check verify ratio
        verify_ratio = scores.get("verify_ratio", 0.0)
        min_verify = score_thresholds.get("min_verify_ratio", 0.5)
        
        if verify_ratio < min_verify:
            return PolicyDecision(
                action="FLAG",
                reason=f"Low verify ratio: {verify_ratio} < {min_verify}",
                metadata={"verify_ratio": verify_ratio, "threshold": min_verify}
            )
        
        # Check unsupported claims rate
        unsupported_rate = scores.get("unsupported_claim_rate", 0.0)
        max_unsupported = score_thresholds.get("max_unsupported_rate", 0.2)
        
        if unsupported_rate > max_unsupported:
            return PolicyDecision(
                action="FLAG",
                reason=f"High unsupported rate: {unsupported_rate} > {max_unsupported}",
                metadata={"unsupported_rate": unsupported_rate, "threshold": max_unsupported}
            )
        
        return PolicyDecision(action="PASS", reason="Scores within policy bounds")
    
    def record_decision(self, decision: PolicyDecision):
        """Record policy decision for audit trail."""
        self.decisions.append(decision)
    
    def get_decisions_summary(self) -> Dict[str, Any]:
        """Get summary of all policy decisions."""
        return {
            "total_decisions": len(self.decisions),
            "actions": {
                action: len([d for d in self.decisions if d.action == action])
                for action in set(d.action for d in self.decisions)
            },
            "recent_decisions": [
                {
                    "action": d.action,
                    "reason": d.reason,
                    "timestamp": d.timestamp.isoformat()
                }
                for d in self.decisions[-10:]  # Last 10 decisions
            ]
        }


# Default policy configuration
DEFAULT_POLICY_CONFIG = {
    "max_content_size": 1_000_000,
    "max_claims": 50,
    "require_sources": True,
    "model_routing": {
        "heavy_threshold": 50000,
        "vision_model": "local_gemma",
        "disagreement_threshold": 0.3
    },
    "score_thresholds": {
        "min_verify_ratio": 0.5,
        "max_unsupported_rate": 0.2
    }
}
