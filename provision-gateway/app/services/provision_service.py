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

    # ---- Tasks ----

    async def list_tasks(self) -> dict[str, Any]:
        return await self._request("GET", "/tasks")

    async def get_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/tasks/{task_id}")

    async def cancel_task(self, task_id: str) -> dict[str, Any]:
        return await self._request("DELETE", f"/tasks/{task_id}")

    # ---- Health ----

    async def health(self) -> dict[str, Any]:
        try:
            return await self._request("GET", "/health", timeout=5.0)
        except Exception:
            return {"status": "unreachable"}


# Singleton
provision_service = ProvisionService()
