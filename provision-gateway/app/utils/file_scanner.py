"""File scanner — analyzes a project directory for LLM config generation context."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RepoContext:
    """Context extracted from a source project directory."""
    directory: str = ""
    repo_description: str = ""
    repo_files: list[str] = field(default_factory=list)
    port: int = 8000
    needs_db: bool = False
    needs_cache: bool = False
    needs_volume: bool = False
    language: str = "unknown"
    framework: str = "unknown"
    has_dockerfile: bool = False
    has_compose: bool = False
    has_nginx_conf: bool = False
    has_env_file: bool = False
    dockerfile_content: str = ""
    env_content: str = ""


def scan_directory(directory: str | Path) -> RepoContext:
    """Scan a directory and build a RepoContext for LLM config generation.
    
    Detects:
    - Language/framework from known config files
    - Exposed ports
    - Database dependencies
    - Cache dependencies
    - Volume needs
    """
    root = Path(directory)
    ctx = RepoContext(directory=str(root))

    if not root.exists():
        return ctx

    # List all non-hidden files (max depth 2)
    files = []
    for p in root.iterdir():
        if p.name.startswith("."):
            continue
        if p.is_file():
            files.append(p.name)
        elif p.is_dir():
            for sp in p.iterdir():
                if sp.is_file() and not sp.name.startswith("."):
                    files.append(f"{p.name}/{sp.name}")

    ctx.repo_files = sorted(files)

    # ---- Dockerfile detection ----
    dockerfile_path = root / "Dockerfile"
    if dockerfile_path.exists():
        ctx.has_dockerfile = True
        ctx.dockerfile_content = dockerfile_path.read_text()
        # Extract EXPOSE port
        import re
        m = re.search(r"EXPOSE\s+(\d+)", ctx.dockerfile_content)
        if m:
            ctx.port = int(m.group(1))

    # ---- docker-compose detection ----
    compose_files = list(root.glob("docker-compose*.yml")) + list(root.glob("docker-compose*.yaml"))
    if compose_files:
        ctx.has_compose = True

    # ---- nginx conf detection ----
    nginx_files = list(root.glob("*.nginx.conf")) + list(root.glob("nginx*.conf"))
    if nginx_files:
        ctx.has_nginx_conf = True

    # ---- .env detection ----
    env_path = root / ".env"
    if env_path.exists():
        ctx.has_env_file = True
        ctx.env_content = env_path.read_text()

    # ---- Language / framework detection ----
    if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        ctx.language = "python"
        reqs = ""
        if (root / "requirements.txt").exists():
            reqs = (root / "requirements.txt").read_text().lower()
        if "fastapi" in reqs:
            ctx.framework = "fastapi"
        elif "flask" in reqs:
            ctx.framework = "flask"
        elif "django" in reqs:
            ctx.framework = "django"
        else:
            ctx.framework = "python"
        if not ctx.port:
            ctx.port = 8000
        # Check for DB deps
        if any(d in reqs for d in ["psycopg2", "sqlalchemy", "pymongo", "redis", "aiosqlite"]):
            ctx.needs_db = True
        if "redis" in reqs:
            ctx.needs_cache = True

    elif (root / "package.json").exists():
        ctx.language = "node"
        try:
            import json
            pkg = json.loads((root / "package.json").read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                ctx.framework = "nextjs"
                ctx.port = 3000
            elif "express" in deps:
                ctx.framework = "express"
                ctx.port = ctx.port or 3000
            elif "react" in deps:
                ctx.framework = "react"
            else:
                ctx.framework = "node"
            if any(d in deps for d in ["pg", "mysql2", "mongoose", "prisma", "redis", "ioredis"]):
                ctx.needs_db = True
            if any(d in deps for d in ["redis", "ioredis"]):
                ctx.needs_cache = True
        except Exception:
            ctx.framework = "node"

    elif (root / "go.mod").exists():
        ctx.language = "go"
        ctx.framework = "go"
        ctx.port = ctx.port or 8080

    elif (root / "Cargo.toml").exists():
        ctx.language = "rust"
        ctx.framework = "rust"
        ctx.port = ctx.port or 8080

    elif (root / "pom.xml").exists() or (root / "build.gradle").exists():
        ctx.language = "java"
        ctx.framework = "java"
        pom = root / "pom.xml"
        if pom.exists():
            try:
                content = pom.read_text().lower()
                if "spring" in content:
                    ctx.framework = "spring"
            except Exception:
                pass
        ctx.port = ctx.port or 8080

    # ---- Needs volume detection ----
    if any(f.endswith((".db", ".sqlite", ".sqlite3")) for f in files):
        ctx.needs_volume = True
    if any("volume" in f.lower() for f in files):
        ctx.needs_volume = True

    # ---- Build description ----
    parts = []
    if ctx.language != "unknown":
        parts.append(f"A {ctx.language.upper()}{' ' + ctx.framework if ctx.framework != ctx.language else ''} app")
    else:
        parts.append("An application")
    if ctx.port:
        parts.append(f"listening on port {ctx.port}")
    if ctx.needs_db:
        parts.append("requiring a database")
    if ctx.needs_cache:
        parts.append("with Redis caching")
    ctx.repo_description = ". ".join(parts) + "."

    return ctx
