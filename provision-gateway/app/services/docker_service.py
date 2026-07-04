"""Docker service — interacts with Docker daemon for container/network operations."""

from __future__ import annotations

import asyncio
import subprocess
import json
from pathlib import Path
from typing import Any


def _docker_cmd(args: list[str]) -> subprocess.CompletedProcess:
    """Run a docker command and return the result."""
    return subprocess.run(
        ["docker"] + args,
        text=True,
        capture_output=True,
    )


def docker_ps() -> list[dict[str, str]]:
    """Return list of all containers with name, status, image."""
    result = _docker_cmd(["ps", "-a", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.RunningFor}}"])
    containers = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            containers.append({
                "name": parts[0].strip(),
                "status": parts[1].strip(),
                "image": parts[2].strip(),
                "running_for": parts[3].strip() if len(parts) > 3 else "",
            })
    return containers


def docker_stats_snapshot() -> list[dict[str, str]]:
    """Return one-shot docker stats (no-stream)."""
    result = _docker_cmd([
        "stats", "--no-stream",
        "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}",
    ])
    stats = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            stats.append({
                "name": parts[0].strip(),
                "cpu_percent": parts[1].strip(),
                "mem_usage": parts[2].strip(),
                "mem_percent": parts[3].strip(),
            })
    return stats


def docker_info() -> dict[str, Any]:
    """Return docker system info (containers count, etc.)."""
    result = _docker_cmd(["info", "--format", "{{json .}}"])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def get_container_count() -> tuple[int, int]:
    """Return (total_containers, running_containers)."""
    result = _docker_cmd(["ps", "-a", "--format", "{{.Status}}"])
    lines = [l for l in result.stdout.splitlines() if l.strip()]
    total = len(lines)
    running = sum(1 for l in lines if l.lower().startswith("up"))
    return total, running


def get_host_stats() -> dict[str, Any]:
    """Get host-level stats (CPU, memory, disk)."""
    import shutil
    disk = shutil.disk_usage("/")
    return {
        "disk_total_gb": round(disk.total / (1024**3), 1),
        "disk_used_gb": round(disk.used / (1024**3), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "disk_percent": round(disk.used / disk.total * 100, 1),
    }


def get_provision_dir_size() -> dict[str, Any]:
    """Calculate PROVISION_DIR disk usage."""
    from ..config import settings
    prov_dir = settings.PROVISION_DIR
    total_size = 0
    if prov_dir.exists():
        for f in prov_dir.rglob("*"):
            if f.is_file():
                total_size += f.stat().st_size
    return {
        "path": str(prov_dir),
        "size_gb": round(total_size / (1024**3), 2),
    }


def nginx_reload(container: str = "provision-nginx") -> bool:
    """Reload nginx in the provision-nginx container."""
    result = _docker_cmd(["exec", container, "nginx", "-s", "reload"])
    return result.returncode == 0


def network_connect(container: str, network: str) -> bool:
    """Connect container to network. Returns True if successful."""
    result = _docker_cmd(["network", "connect", network, container])
    return result.returncode == 0


def container_running(name: str) -> bool:
    """Check if a container is running."""
    result = _docker_cmd(["inspect", "-f", "{{.State.Running}}", name])
    return result.stdout.strip() == "true"


def container_exists(name: str) -> bool:
    """Check if a container exists."""
    result = _docker_cmd(["inspect", name])
    return result.returncode == 0
