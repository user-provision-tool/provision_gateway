"""The -api's ``api_users`` interface (one-way gateway → -api HTTP call).

The gateway never inserts a registry row itself. Account creation calls
``ensure`` (mode=account) before its own ``users`` insert, then ``claim`` after
the commit; a direct/CLI deploy is the -api's own business (mode=owner).
"""

from __future__ import annotations

import httpx

from ..config import settings


class ApiUsersUnavailable(RuntimeError):
    pass


class ApiUsersConflict(ValueError):
    pass


class ApiUsersClient:
    def __init__(self, base_url: str | None = None, timeout: float = 10.0):
        self.base_url = (base_url or settings.PROVISION_API_URL).rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, timeout: float | None = None, **kwargs):
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                resp = client.request(method, f"{self.base_url}{path}", **kwargs)
        except httpx.HTTPError as exc:
            raise ApiUsersUnavailable(f"provision-api unreachable: {exc}") from exc
        if resp.status_code == 409:
            raise ApiUsersConflict(resp.json().get("detail") or "api_users conflict")
        if resp.status_code >= 400:
            raise ApiUsersUnavailable(
                f"provision-api {method} {path} -> {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()

    def ensure(self, username: str, mode: str = "account") -> dict:
        return self._call("POST", "/api-users", json={"username": username, "mode": mode})

    def claim(self, api_user_id: str) -> dict:
        return self._call("POST", f"/api-users/{api_user_id}/claim")

    def release_pending(self, api_user_id: str) -> bool:
        """DELETE ?scope=pending; a 409 means a gateway row exists — adopt."""
        try:
            self._call("DELETE", f"/api-users/{api_user_id}?scope=pending")
            return True
        except ApiUsersConflict:
            return False

    def get(self, api_user_id: str) -> dict:
        return self._call("GET", f"/api-users/{api_user_id}")

    def soft_delete(self, api_user_id: str) -> dict:
        return self._call("DELETE", f"/api-users/{api_user_id}")

    def stop_services(self, api_user_id: str) -> dict:
        # Stopping a running service is a Docker operation; 10s is too short and
        # made the account-deletion saga look failed on its first attempt.
        return self._call("POST", f"/api-users/{api_user_id}/stop-services", timeout=120)


api_users = ApiUsersClient()
