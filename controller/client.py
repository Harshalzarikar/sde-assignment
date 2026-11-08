"""Stdlib-only HTTP client for the outcome-loop simulator."""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class SimError(Exception):
    """An HTTP error returned by the simulator."""

    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"simulator returned HTTP {status}: {body!r}")


class ScenarioFinished(SimError):
    """The simulator has reached the end of the scenario (HTTP 409)."""


class CreditsExhausted(SimError):
    """The simulator credit budget is exhausted (HTTP 402)."""


class SimClient:
    """Small HTTP wrapper exposing the simulator endpoints used by the controller."""

    def __init__(self, base_url: str, timeout: float = 30.0, retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(0, retries)
        self._opener = urllib.request.build_opener()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = self._url(path)
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    raw = response.read().decode("utf-8")
                    return self._parse(raw)
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                parsed = self._parse(raw)
                if exc.code == 402:
                    raise CreditsExhausted(exc.code, parsed) from exc
                if exc.code == 409 and "finished" in raw.lower():
                    raise ScenarioFinished(exc.code, parsed) from exc
                raise SimError(exc.code, parsed) from exc
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break
                time.sleep(min(0.5 * (2**attempt), 8.0))

        raise SimError(0, f"connection failed after {self.retries + 1} attempts: {last_error}")

    @staticmethod
    def _parse(raw: str) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def health(self) -> Any:
        return self._request("GET", "/health")

    def get_state(self) -> dict[str, Any]:
        return self._request("GET", "/sim/state")

    def get_workflows(self) -> list[dict[str, Any]]:
        return self._request("GET", "/workflows")

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        wid = urllib.parse.quote(workflow_id, safe="")
        return self._request("GET", f"/workflows/{wid}")

    def get_config(self, workflow_id: str) -> dict[str, Any]:
        wid = urllib.parse.quote(workflow_id, safe="")
        return self._request("GET", f"/workflows/{wid}/config")

    def get_config_history(self, workflow_id: str) -> list[dict[str, Any]]:
        wid = urllib.parse.quote(workflow_id, safe="")
        return self._request("GET", f"/workflows/{wid}/config/history")

    def list_chunks(self, kind: str = "events", after: int = -1) -> list[dict[str, Any]]:
        return self._request("GET", "/events/chunks", params={"kind": kind, "after": after})

    def get_chunk(self, n: int) -> list[dict[str, Any]]:
        raw = self._request("GET", f"/events/chunks/{int(n)}")
        if not isinstance(raw, str):
            return raw if isinstance(raw, list) else []
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def list_approvals(
        self, workflow_id: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        return self._request(
            "GET",
            "/approvals",
            params={"workflow_id": workflow_id, "status": status},
        )

    def request_approval(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/approvals", body=body)

    def get_approval(self, approval_id: str) -> dict[str, Any]:
        aid = urllib.parse.quote(approval_id, safe="")
        return self._request("GET", f"/approvals/{aid}")

    def apply_approval(self, workflow_id: str, approval_id: str) -> dict[str, Any]:
        wid = urllib.parse.quote(workflow_id, safe="")
        return self._request(
            "POST",
            f"/workflows/{wid}/apply",
            body={"approval_id": approval_id},
        )

    def put_config(self, workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
        wid = urllib.parse.quote(workflow_id, safe="")
        return self._request("PUT", f"/workflows/{wid}/config", body=body)

    def advance(self, minutes: int) -> dict[str, Any]:
        return self._request("POST", "/sim/advance", body={"minutes": int(minutes)})
