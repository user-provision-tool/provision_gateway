"""Provision service — proxies requests to provision-api."""

from __future__ import annotations

import httpx
from typing import Any

from ..config import settings


class ProvisionService:
    """Async HTTP client wrapper for provision-api."""

    def __init__(self) -> None:
        self._base_url: str = settings.PROVISION_API_URL.rstrip("/")

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict | None = None,
        params: dict | None = None,
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        """Make an async request to provision-api."""
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                resp = await client.get(url, params=params)
            elif method == "POST":
                resp = await client.post(url, json=json_data, params=params)
            elif method == "DELETE":
                resp = await client.delete(url, params=params)
            elif method == "PUT":
                resp = await client.put(url, json=json_data, params=params)
            else:
                raise ValueError(f"Unsupported method: {method}")

            if resp.status_code >= 400:
                detail = resp.text
                try:
                    detail = resp.json().get("detail", detail)
                except Exception:
                    pass
                raise httpx.HTTPStatusError(
                    f"provision-api error: {detail}",
                    request=resp.request,
                    response=resp,
                )
            return resp.json()

    # ---- Users ----

    async def list_users(self) -> dict[str, Any]:
        return await self._request("GET", "/users")

    async def get_user(self, user_name: str) -> dict[str, Any]:
        return await self._request("GET", f"/users/{user_name}")

    async def register_user(self, **kwargs) -> dict[str, Any]:
        return await self._request("POST", "/users", json_data=kwargs)

    async def remove_user(self, user_name: str, service_name: str, label: str) -> dict[str, Any]:
        return await self._request(
            "DELETE", f"/users/{user_name}/services/{service_name}/{label}"
        )

    async def rebuild_user(
        self, user_name: str, service_name: str, label: str, **kwargs
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/users/{user_name}/services/{service_name}/{label}/rebuild",
            json_data=kwargs,
        )

    async def start_user(self, user_name: str, service_name: str, label: str) -> dict[str, Any]:
        """Start a user's service containers (proxy to provision-api)."""
        return await self._request(
            "POST", f"/users/{user_name}/services/{service_name}/{label}/up"
        )

    async def stop_user(self, user_name: str, service_name: str, label: str) -> dict[str, Any]:
        """Stop a user's service containers (proxy to provision-api)."""
        return await self._request(
            "POST", f"/users/{user_name}/services/{service_name}/{label}/down"
        )

    async def change_user_password(
        self, user_name: str, service_name: str, label: str, passwd: str
    ) -> dict[str, Any]:
        """Change a user's service password (proxy to provision-api)."""
        return await self._request(
            "PUT",
            f"/users/{user_name}/services/{service_name}/{label}/password",
            json_data={"passwd": passwd},
        )

    async def nginx_reconnect_all(self) -> dict[str, Any]:
        """Reconnect nginx to all user networks (proxy to provision-api)."""
        return await self._request("POST", "/nginx/reconnect-all")

    async def nginx_connections(self) -> dict[str, Any]:
        """Get nginx connection state (proxy to provision-api)."""
        return await self._request("GET", "/nginx/connections")

    # ---- Reconciliation (proxied to provision-api) ----

    async def reconcile(self) -> dict[str, Any]:
        """Run a full reconciliation pass (proxy to provision-api)."""
        return await self._request("POST", "/reconcile")

    async def reconciliation_status(self) -> dict[str, Any]:
        """Get last reconciliation status (proxy to provision-api)."""
        return await self._request("GET", "/reconcile/status")

    async def nginx_state(self) -> dict[str, Any]:
        """Get the full nginx state JSON (proxy to provision-api)."""
        return await self._request("GET", "/nginx-state")

    # ---- Docker / Host stats ----

    async def docker_ps(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/docker/ps")

    async def docker_stats(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/docker/stats")

    async def docker_info(self) -> dict[str, Any]:
        return await self._request("GET", "/docker/info")

    async def host_stats(self) -> dict[str, Any]:
        return await self._request("GET", "/host/stats")

    async def container_exists(self, container: str) -> bool:
        r = await self._request("GET", f"/docker/container/{container}/exists")
        return r.get("exists", False)

    async def container_running(self, container: str) -> bool:
        r = await self._request("GET", f"/docker/container/{container}/running")
        return r.get("running", False)

    async def network_connect(self, network: str, container: str) -> dict[str, Any]:
        return await self._request("POST", f"/docker/network/{network}/connect/{container}")

    async def nginx_reload(self, container: str = "provision-nginx") -> dict[str, Any]:
        return await self._request("POST", "/docker/nginx/reload", params={"container": container})

    # ---- Tasks ----

    async def list_tasks(self) -> dict[str, Any]:
        return await self._request("GET", "/tasks")

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/tasks/{task_id}")

    async def stream_task_log(self, task_id: str, tail: int = 200, follow: bool = True):
        """Stream task build log from provision-api via SSE.
        
        Returns an async generator yielding raw SSE lines from provision-api.
        """
        url = f"{self._base_url}/tasks/{task_id}/log"
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, params={"tail": tail, "follow": follow}) as resp:
                if resp.status_code >= 400:
                    detail = await resp.aread()
                    raise httpx.HTTPStatusError(
                        f"provision-api error: {detail.decode()}",
                        request=resp.request,
                        response=resp,
                    )
                async for line in resp.aiter_lines():
                    if line:
                        yield f"{line}\n\n"

    # ---- Container logs ----

    async def get_container_logs(
        self, user_name: str, service_name: str, label: str,
        container: str, tail: int = 100,
    ) -> dict[str, Any]:
        """Get container logs from provision-api."""
        return await self._request(
            "GET",
            f"/users/{user_name}/services/{service_name}/{label}/containers/{container}/logs",
            params={"tail": tail},
        )

    # ---- Health ----

    async def health(self) -> dict[str, Any]:
        try:
            return await self._request("GET", "/health", timeout=5.0)
        except Exception:
            return {"status": "unreachable"}


# Singleton
provision_service = ProvisionService()
