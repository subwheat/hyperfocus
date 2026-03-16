"""
ACP Core Package
================
Append-only Claims Processing engine.

Core components:
- RunTrace: immutable execution traces
- PolicyRuntime: policy enforcement
- Replay: deterministic reconstruction
- Scoring: quality metrics
"""

__version__ = "1.3.0"
__all__ = ["api", "models", "repositories", "replay", "policy_runtime"]
