"""Small HTTP client for the simulator. It is the only way the controller learns anything about the world."""
import time

import httpx


class Api:
    def __init__(self, base_url: str, timeout: float = 120.0):
        self.http = httpx.Client(base_url=base_url, timeout=timeout)

    def call(self, method: str, path: str, raw: bool = False, **kw):
        """Returns (status_code, body). Retries network errors and 5xx; a 4xx is an answer, so it is returned."""
        delay = 0.5
        for _ in range(8):
            try:
                r = self.http.request(method, path, **kw)
            except httpx.TransportError:
                r = None
            if r is not None and r.status_code < 500:
                if raw:
                    return r.status_code, r.text
                try:
                    return r.status_code, r.json()
                except ValueError:
                    return r.status_code, r.text
            time.sleep(delay)
            delay = min(delay * 2, 10)
        raise RuntimeError(f"{method} {path} kept failing; is the simulator running?")

    def get(self, path: str, **params):
        return self.call("GET", path, params=params or None)

    def post(self, path: str, body: dict):
        return self.call("POST", path, json=body)

    def put(self, path: str, body: dict):
        return self.call("PUT", path, json=body)

    def get_ok(self, path: str, **params):
        status, body = self.get(path, **params)
        if status != 200:
            raise RuntimeError(f"GET {path} -> {status}: {body}")
        return body

    def chunk_lines(self, n: int):
        """Yields a chunk's NDJSON lines as they arrive, so a 250k-event chunk never sits in memory whole.
        A broken connection raises httpx.TransportError; the caller restarts the chunk from the top."""
        with self.http.stream("GET", f"/events/chunks/{n}") as r:
            if r.status_code >= 500:
                raise httpx.RemoteProtocolError(f"chunk {n} -> {r.status_code}")
            if r.status_code != 200:
                raise RuntimeError(f"chunk {n} -> {r.status_code}: {r.read()[:200]!r}")
            yield from r.iter_lines()
