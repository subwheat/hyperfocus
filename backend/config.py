"""
Hyperfocus Configuration
========================
Centralized settings with environment variable support.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Application
    # ─────────────────────────────────────────────────────────────────────────
    app_name: str = "ACP"
    app_version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = True

    # ─────────────────────────────────────────────────────────────────────────
    # Authentication
    # ─────────────────────────────────────────────────────────────────────────
    api_key: str = "hf-dev-key-change-me-in-production"
    jwt_secret: str = "hyperfocus-jwt-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expiration_hours: int = 24

    # ─────────────────────────────────────────────────────────────────────────
    # vLLM / DeepSeek
    # ─────────────────────────────────────────────────────────────────────────
    vllm_base_url: str = "http://localhost:8000"
    vllm_model: str = "deepseek-ai/DeepSeek-V3"
    vllm_timeout: float = 120.0
    vllm_max_tokens: int = 4096

    # ─────────────────────────────────────────────────────────────────────────
    # Storage
    # ─────────────────────────────────────────────────────────────────────────
    artefacts_root: Path = Path("/mnt/hyperfocus/artefacts")
    models_root: Path = Path("/mnt/hyperfocus/models")
    cache_root: Path = Path("/mnt/hyperfocus/cache")

    # ─────────────────────────────────────────────────────────────────────────
    # Database
    # ─────────────────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./hyperfocus.db"
    # Phase 2: postgres+asyncpg://user:pass@host:5432/hyperfocus

    # ─────────────────────────────────────────────────────────────────────────
    # Rate Limiting
    # ─────────────────────────────────────────────────────────────────────────
    rate_limit_requests: int = 100
    rate_limit_window_seconds: int = 60

    # ─────────────────────────────────────────────────────────────────────────
    # CORS (for separate frontend)
    # ─────────────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # ─────────────────────────────────────────────────────────────────────────
    # Terminal (Phase 1.1+)
    # ─────────────────────────────────────────────────────────────────────────
    terminal_enabled: bool = True
    terminal_shell: str = "/bin/bash"
    terminal_timeout_seconds: int = 300
    terminal_max_sessions: int = 5

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


# Singleton instance
settings = Settings()
