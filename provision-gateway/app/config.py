"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path


class Settings:
    """Gateway settings sourced from environment variables."""

    def __init__(self) -> None:
        # ---- Paths ----
        self.PROVISION_DIR = Path(
            os.environ.get("PROVISION_DIR", "/srv/provision")
        )
        self.GATEWAY_DATA_DIR = Path(
            os.environ.get("GATEWAY_DATA_DIR", str(self.PROVISION_DIR / "gateway_data"))
        )
        self.GATEWAY_DATA_DIR.mkdir(parents=True, exist_ok=True)

        # ---- Secrets ----
        self.GATEWAY_SECRET_KEY: str = os.environ.get(
            "GATEWAY_SECRET_KEY", "dev-secret-change-me-in-production-32chars!"
        )

        # ---- provision-api connection ----
        self.PROVISION_API_URL: str = os.environ.get(
            "PROVISION_API_URL", "http://subnet-acl-provision-api:8000"
        )

        # ---- Nginx display ports (for URL generation) ----
        self.NGINX_HTTP_PORT: int = int(os.environ.get("NGINX_HTTP_PORT", "80"))
        self.NGINX_HTTPS_PORT: int = int(os.environ.get("NGINX_HTTPS_PORT", "443"))

        # ---- Docker log path ----
        # Log streaming is now proxied to provision-api's per-task SSE endpoint.
        # DOCKER_OPS_LOG is kept for backward-compatible reference only.
        self.DOCKER_OPS_LOG: Path = Path(
            os.environ.get(
                "DOCKER_OPS_LOG",
                str(self.PROVISION_DIR / "generated" / "docker_ops.log"),
            )
        )

        # ---- Generated / source paths (mirrored from provision-api for file ops) ----
        self.GENERATED_DIR: Path = self.PROVISION_DIR / "generated"
        self.SOURCE_PROJECTS_DIR: Path = self.PROVISION_DIR / "source_projects"
        self.USER_DATA_DIR: Path = self.PROVISION_DIR / "user_data"
        self.SSL_DIR: Path = self.PROVISION_DIR / "ssl"



        # ---- Database ----
        self.DATABASE_URL: str = os.environ.get(
            "DATABASE_URL",
            f"sqlite:///{self.GATEWAY_DATA_DIR / 'gateway.db'}",
        )

        # ---- Generation capacity (GRILLING G29) ----
        # The GATEWAY's own pool limit, a sibling of the -api's
        # TASK_MAX_CONCURRENCY. Same mechanism, different reason: the -api's
        # guards the host against concurrent docker work; this one guards the
        # LLM endpoint and cost. Kept as a separate number on purpose — do not
        # "unify" them.
        self.GATEWAY_MAX_GENERATIONS: int = int(
            os.environ.get("GATEWAY_MAX_GENERATIONS", "4")
        )

        # ---- Cleanup contract (GRILLING G30) ----
        # ONE contract, two engine-adapted implementations: the terminal set,
        # the log disposal and the trigger rule are shared, but one engine scans
        # an in-memory dict and rewrites a JSON file while this one issues a SQL
        # `DELETE`. These two numbers therefore carry the SAME DEFAULTS as the
        # -api's `TASK_TTL_SECONDS` / `TASK_MAX_COUNT` so the two stores cannot
        # drift apart, while the env names stay the gateway's own: they configure
        # a different engine, and a shared variable would silently retune both.
        self.GATEWAY_JOB_TTL_SECONDS: int = int(
            os.environ.get("GATEWAY_JOB_TTL_SECONDS", "604800")     # = the -api's 1 week
        )
        self.GATEWAY_MAX_JOBS: int = int(
            os.environ.get("GATEWAY_MAX_JOBS", "1000")              # = the -api's cap
        )

        # ---- JWT ----
        self.JWT_EXPIRE_SEC: int = int(os.environ.get("JWT_EXPIRE_SEC", "3600"))
        self.JWT_REFRESH_EXPIRE_SEC: int = int(
            os.environ.get("JWT_REFRESH_EXPIRE_SEC", "604800")
        )
        self.JWT_ALGORITHM: str = "HS256"

        # ---- ACL ----
        self.ENABLE_ACL: bool = os.environ.get("ENABLE_ACL", "false").lower() == "true"
        self.REGISTRY_FILE: str = os.environ.get(
            "REGISTRY_FILE",
            str(self.PROVISION_DIR / "generated" / "user_registry.yml"),
        )
        self.PROVISION_COOKIE_TTL: int = int(os.environ.get("PROVISION_COOKIE_TTL", "604800"))

        # ---- Exchange code (v4 §6.2) ----
        self.EXCHANGE_CODE_TTL_SEC: int = int(os.environ.get("EXCHANGE_CODE_TTL_SEC", "30"))

        # ---- Logging ----
        self.LOG_LEVEL: str = os.environ.get("GATEWAY_LOG_LEVEL", "INFO")


settings = Settings()
