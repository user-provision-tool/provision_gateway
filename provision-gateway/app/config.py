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
            "PROVISION_API_URL", "http://provision-api:8000"
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

        # ---- JWT ----
        self.JWT_EXPIRE_SEC: int = int(os.environ.get("JWT_EXPIRE_SEC", "3600"))
        self.JWT_REFRESH_EXPIRE_SEC: int = int(
            os.environ.get("JWT_REFRESH_EXPIRE_SEC", "604800")
        )
        self.JWT_ALGORITHM: str = "HS256"

        # ---- Logging ----
        self.LOG_LEVEL: str = os.environ.get("GATEWAY_LOG_LEVEL", "INFO")


settings = Settings()
