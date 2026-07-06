"""Reconciliation service — verifies nginx upstreams match running containers.

All Docker operations are delegated to provision-api via ProvisionService.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import settings
from .provision_service import provision_service


class ReconciliationService:
    """Verifies and repairs nginx upstream connections."""

    async def run_reconciliation(self) -> dict[str, Any]:
        """Run a full reconciliation pass."""
        state_file = settings.NGINX_STATE_FILE
        generated_dir = settings.GENERATED_DIR

        # Read all nginx conf files
        conf_files = sorted(generated_dir.glob("*.nginx.conf"))
        upstreams = []
        reachable = 0
        unreachable = 0
        unreachable_details = []
        networks_reconnected = 0

        for cf in conf_files:
            try:
                content = cf.read_text()
                server_name = None
                proxy_pass = None

                m = re.search(r"server_name\s+([^;]+);", content)
                if m:
                    server_name = m.group(1).strip().split()[0]

                m = re.search(r"proxy_pass\s+(https?://[^;]+);", content)
                if m:
                    proxy_pass = m.group(1).strip()

                upstream = {
                    "conf_file": cf.name,
                    "server_name": server_name,
                    "proxy_pass": proxy_pass,
                }

                # Verify target container
                if proxy_pass:
                    # Extract container name from proxy_pass URL
                    # Format: http://container-name:port
                    url_match = re.match(r"https?://([^:]+)(?::(\d+))?", proxy_pass)
                    if url_match:
                        target_container = url_match.group(1)
                        upstream["target_container"] = target_container

                        running = await provision_service.container_running(target_container)
                        exists = await provision_service.container_exists(target_container)
                        if running:
                            upstream["reachable"] = True
                            reachable += 1
                        else:
                            upstream["reachable"] = False
                            unreachable += 1
                            reason = "container not running" if exists else "container not found"
                            upstream["reason"] = reason
                            unreachable_details.append({
                                "upstream": proxy_pass,
                                "target_container": target_container,
                                "reason": reason,
                            })
                else:
                    upstream["reachable"] = None

                upstreams.append(upstream)
            except Exception as e:
                unreachable_details.append({
                    "conf_file": cf.name,
                    "reason": str(e),
                })
                unreachable += 1

        # Reconnect provision-nginx to networks where needed
        # (We check per-user networks based on what's in the conf files)
        for cf in conf_files:
            try:
                content = cf.read_text()
                # The network name is derived from the conf file name pattern
                # Format: {service_name}.user-{user_name}.{label}.nginx.conf
                match = re.match(r"(.+)\.user-(.+)\.(\d+)\.nginx\.conf", cf.name)
                if match:
                    service_name = match.group(1)
                    user_name = match.group(2)
                    label = match.group(3)
                    network_name = f"{service_name}-user_{user_name}-{label}"

                    # Try connecting provision-nginx to this network (via provision-api)
                    try:
                        await provision_service.network_connect(network_name, "provision-nginx")
                        networks_reconnected += 1
                    except Exception:
                        pass
            except Exception:
                pass

        # Reload nginx (via provision-api)
        try:
            await provision_service.nginx_reload()
            nginx_reloaded = True
        except Exception:
            nginx_reloaded = False

        # Build report
        report = {
            "last_run": datetime.now(timezone.utc).isoformat(),
            "total_upstreams": len(upstreams),
            "reachable": reachable,
            "unreachable": unreachable,
            "unreachable_details": unreachable_details,
            "networks_reconnected": networks_reconnected,
            "nginx_reloaded": nginx_reloaded,
            "upstreams": upstreams,
        }

        # Write state file
        state = {
            "version": 1,
            "last_updated": report["last_run"],
            "networks": {},  # populated by detailed inspection
            "upstreams": upstreams,
        }
        try:
            state_file.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

        return report

    async def get_state(self) -> dict[str, Any]:
        """Return the current cached state from provision_nginx_state.json."""
        state_file = settings.NGINX_STATE_FILE
        if state_file.exists():
            try:
                return json.loads(state_file.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"version": 1, "upstreams": [], "networks": {}}


# Singleton
reconciliation_service = ReconciliationService()
