"""Local testing only: the same HTTP API as `python -m sim serve`, built on the standard library.

Use it when fastapi/uvicorn aren't installed. It wraps the unmodified `sim.world.World`, so the controller
sees exactly the same contract. The controller never imports this file.

  python devtools/sim_server_stdlib.py --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01 --force
"""
import argparse
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sim.scenario import load_scenario  # noqa: E402
from sim.world import ApiError, World, _json_default  # noqa: E402


class _NotFound(Exception):
    pass


def make_handler(world: World):
    lock = threading.Lock()

    def route(method: str, parts: list, q: dict, body):
        w = world
        if method == "GET":
            if parts == ["health"]:
                return {"ok": True}
            if parts == ["sim", "state"]:
                return w.state()
            if parts == ["events", "chunks"]:
                kind = q.get("kind", "events")
                if kind not in ("events", "history"):
                    raise ValueError("kind must be events or history")
                after = int(q.get("after", -1))
                return [c for c in w.chunks if c["kind"] == kind and c["chunk"] > after]
            if len(parts) == 3 and parts[:2] == ["events", "chunks"]:
                n = int(parts[2])
                if not 0 <= n < len(w.chunks):
                    raise _NotFound()
                return (Path(w.run_dir) / w.chunks[n]["path"]).read_bytes()
            if parts == ["workflows"]:
                return [w.workflow_public(wid) for wid in w.wfs]
            if len(parts) == 2 and parts[0] == "workflows":
                return w.workflow_public(parts[1])
            if len(parts) == 3 and parts[0] == "workflows" and parts[2] == "config":
                return w.get_config(parts[1])
            if len(parts) == 4 and parts[0] == "workflows" and parts[2:] == ["config", "history"]:
                return w.config_history(parts[1])
            if parts == ["approvals"]:
                return w.list_approvals(q.get("workflow_id"), q.get("status"))
            if len(parts) == 2 and parts[0] == "approvals":
                return w.get_approval(parts[1])
        if method == "POST":
            if parts == ["sim", "advance"]:
                return w.advance(body.get("minutes"))
            if parts == ["approvals"]:
                return w.request_approval(body)
            if len(parts) == 3 and parts[0] == "workflows" and parts[2] == "apply":
                return w.apply_approval(parts[1], body.get("approval_id"))
            if parts == ["llm", "suggest-fixes"]:
                return w.suggest_fixes(body)
            if parts == ["validation", "runs"]:
                return w.run_validation(body)
        if method == "PUT" and len(parts) == 3 and parts[0] == "workflows" and parts[2] == "config":
            return w.auto_apply(parts[1], body)
        raise _NotFound()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, status: int, payload):
            if isinstance(payload, bytes):
                data, ctype = payload, "application/x-ndjson"
            else:
                data, ctype = json.dumps(payload, default=_json_default).encode(), "application/json"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str):
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}") if method != "GET" else None
                with lock:
                    result = route(method, parts, q, body)
            except ApiError as e:
                return self._send(e.status, {"error": e.code, "detail": e.detail})
            except _NotFound:
                return self._send(404, {"detail": "Not Found"})
            except (ValueError, KeyError, TypeError) as e:
                return self._send(422, {"detail": str(e)})
            except Exception as e:  # noqa: BLE001 - mirror a server's 500
                return self._send(500, {"detail": repr(e)})
            self._send(200, result)

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def do_PUT(self):
            self._handle("PUT")

    return Handler


def start(scenario: str, run_dir: str, host: str = "127.0.0.1", port: int = 0, force: bool = True):
    """Builds the world and serves it in a background thread. Returns (server, url)."""
    rd = Path(run_dir)
    if rd.exists() and any(rd.iterdir()):
        if not force:
            sys.exit(f"{rd} is not empty; use --force to wipe it")
        shutil.rmtree(rd)
    world = World(load_scenario(scenario), rd)
    server = ThreadingHTTPServer((host, port), make_handler(world))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    server, url = start(a.scenario, a.run_dir, a.host, a.port, a.force)
    print(f"ready: {url}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
