"""ESM3 HTTP service — runs on the HOST (Boltz2Score venv).

Serves propose/refill/perplexity/learn over HTTP on :9333. The CUDA model
does NOT live in this process: requests are proxied to a
`_esm3_gpu_worker.py` subprocess that owns the CUDA context on a GPU leased
from the shared Redis pool. The pool's admission gate rejects any card with
>200MiB compute residue, and a torch context in a resident process holds
~400-500MB that only dies with the process — so on idle this parent kills
the subprocess (context gone, memory truly 0) and releases the card. The
next request acquires again (any free card) and reloads.

Lifecycle knobs (env):
  ESM3_IDLE_RELEASE_SECONDS  idle window before unload+release (default 600)
  ESM3_GPU_ACQUIRE_TIMEOUT   pool acquire budget per load (default 900)
  ESM3_GPU_ID                UNPOOLED emergency card if the pool is unreachable
  ESM3_ADAPTER_ROOT          LoRA adapter storage root
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PEPLM_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PEPLM_ROOT))

from peplm.integrate.ss_modes import normalize_mode  # noqa: F401  (re-export)

_GPU_IDLE_RELEASE_SECONDS = float(os.environ.get("ESM3_IDLE_RELEASE_SECONDS", "600"))
_GPU_ACQUIRE_TIMEOUT_SECONDS = int(os.environ.get("ESM3_GPU_ACQUIRE_TIMEOUT", "900"))
_FALLBACK_GPU_ID = int(os.environ.get("ESM3_GPU_ID", "0"))
_LEASE_ID = f"esm3-service-{os.getpid()}"
_REPO_ROOT = Path(__file__).resolve().parents[4]
_GPU_WORKER_SCRIPT = str(Path(__file__).resolve().parent / "_esm3_gpu_worker.py")

_gpu_manager_module = None


def _import_gpu_manager():
    """Import the backend GPU pool client (host process: the repo root is not
    on sys.path for a script launched by absolute path)."""
    global _gpu_manager_module
    if _gpu_manager_module is None:
        repo = str(_REPO_ROOT)
        if repo not in sys.path:
            sys.path.insert(0, repo)
        import gpu_manager as module
        # This venv's redis-py defaults socket_timeout=5s; acquire_gpu blocks
        # in BLPOP(30s) cycles, which die at 5s with "Timeout reading from
        # socket" and would push the service into the unpooled fallback.
        # Rebuild the pool without a read timeout — blocking commands manage
        # their own deadlines.
        try:
            import redis as _redis
            module.REDIS_CONNECTION_POOL = _redis.ConnectionPool.from_url(
                module.config.REDIS_URL, decode_responses=True,
                socket_timeout=None)
        except Exception as pool_exc:
            print(f"[esm3-http] WARNING: redis pool rebuild failed ({pool_exc})",
                  flush=True)
        _gpu_manager_module = module
    return _gpu_manager_module


class _GpuSubprocessManager:
    """One leased card + one CUDA subprocess; killed and released on idle."""

    def __init__(self):
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._gpu_id: int | None = None
        self._pooled = False
        self._last_request = 0.0
        self._heartbeat_stop = threading.Event()

    # ── request path ──────────────────────────────────────────────────────
    def request(self, payload: dict) -> dict:
        """Ensure the worker subprocess is up, forward one request."""
        with self._lock:
            self._last_request = time.time()
            self._ensure_worker()
            return self._roundtrip(payload)

    def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        device_id = self._acquire_device()
        env = dict(os.environ)
        env["CUDA_VISIBLE_DEVICES"] = str(device_id)
        env.setdefault("PYTHONUNBUFFERED", "1")
        self._proc = subprocess.Popen(
            [sys.executable, _GPU_WORKER_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, text=True, env=env)
        # Consume the worker's startup line: it doubles as the readiness
        # barrier (checkpoint load takes ~1 min) and keeps the response
        # stream aligned — the first request's readline() must see the
        # response, not this line.
        ready = self._proc.stdout.readline()
        try:
            ready_info = json.loads(ready)
            loaded_device = str(ready_info.get("device") or "?")
        except (json.JSONDecodeError, TypeError):
            self._discard_worker()
            raise RuntimeError(f"gpu worker failed to start: {ready[:200]!r}")
        print(f"[esm3-http] worker ready on {loaded_device} "
              f"({'pool lease' if self._pooled else 'UNPOOLED fallback'})",
              flush=True)

    def _acquire_device(self) -> int:
        if self._gpu_id is not None:
            return self._gpu_id
        try:
            manager = _import_gpu_manager()
            gpu_id = manager.acquire_gpu(_LEASE_ID, timeout=_GPU_ACQUIRE_TIMEOUT_SECONDS)
            self._pooled = True
            self._gpu_id = gpu_id
            # Heartbeat MUST start before the slow checkpoint load: reconcile
            # keeps only live-celery or heartbeating leases, and AsyncResult
            # reads an unknown id as PENDING — without an immediate beat the
            # lease is reclaimed mid-load and a candidate claims the card.
            self._start_heartbeat()
            self._beat_now()
            return gpu_id
        except Exception as exc:  # Redis down / pool drained / import failure
            print(f"[esm3-http] CRITICAL: pool acquire failed ({exc}); "
                  f"falling back to UNPOOLED cuda:{_FALLBACK_GPU_ID}", flush=True)
            self._pooled = False
            self._gpu_id = _FALLBACK_GPU_ID
            return _FALLBACK_GPU_ID

    def _roundtrip(self, payload: dict) -> dict:
        proc = self._proc
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
        except (BrokenPipeError, ValueError) as exc:
            self._discard_worker()
            return {"ok": False, "error": f"gpu worker died: {exc}"}
        if not line:
            self._discard_worker()
            return {"ok": False, "error": "gpu worker closed output (crashed?)"}
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"bad worker response: {exc}"}

    # ── lease upkeep ──────────────────────────────────────────────────────
    def _beat_now(self) -> None:
        try:
            client = _import_gpu_manager().get_redis_client()
            client.set(f"task_heartbeat:{_LEASE_ID}", "alive", ex=60)
        except Exception:
            pass

    def _start_heartbeat(self) -> None:
        self._heartbeat_stop.clear()
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

    def _heartbeat_loop(self) -> None:
        try:
            client = _import_gpu_manager().get_redis_client()
        except Exception:
            return
        while not self._heartbeat_stop.wait(30.0):
            if self._gpu_id is None or not self._pooled:
                continue
            try:
                client.set(f"task_heartbeat:{_LEASE_ID}", "alive", ex=60)
            except Exception:
                pass

    # ── idle release ──────────────────────────────────────────────────────
    def idle_unload_if_due(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            if time.time() - self._last_request < _GPU_IDLE_RELEASE_SECONDS:
                return
            self._drop_worker()

    def _discard_worker(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=10)
            except Exception:
                pass
            self._proc = None

    def _drop_worker(self) -> None:
        """Kill the CUDA subprocess (context dies → memory truly 0), then
        return the card to the pool."""
        self._discard_worker()
        gpu_id, pooled = self._gpu_id, self._pooled
        self._gpu_id, self._pooled = None, False
        self._heartbeat_stop.set()
        if gpu_id is None:
            return
        if not pooled:
            print(f"[esm3-http] idle unload done (unpooled cuda:{gpu_id})",
                  flush=True)
            return
        try:
            time.sleep(2.0)  # driver teardown lag before the reclaim probe
            _import_gpu_manager().release_gpu(gpu_id, _LEASE_ID)
            print(f"[esm3-http] idle unload done; GPU {gpu_id} released to pool",
                  flush=True)
        except Exception as exc:
            print(f"[esm3-http] WARNING: release_gpu({gpu_id}) failed: {exc}",
                  flush=True)

    def snapshot(self) -> dict:
        alive = self._proc is not None and self._proc.poll() is None
        return {
            "state": ("loaded" if alive else "idle"),
            "pooled": self._pooled,
            "gpu_id": self._gpu_id,
            "device": (f"cuda:{self._gpu_id}" if alive and self._gpu_id is not None
                       else "unloaded"),
        }


_MANAGER = _GpuSubprocessManager()


def _idle_reaper_loop() -> None:
    while True:
        time.sleep(15.0)
        try:
            _MANAGER.idle_unload_if_due()
        except Exception as exc:
            print(f"[esm3-http] idle reaper error: {exc}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = min(int(self.headers.get("Content-Length", 0)), 10 * 1024 * 1024)
            req = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError) as exc:
            body = json.dumps({"ok": False, "error": f"bad request: {exc}"}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            # Pool-citizen lifecycle: may block on pool acquire (bounded by
            # ESM3_GPU_ACQUIRE_TIMEOUT) and reload after an idle unload.
            resp = _MANAGER.request(req)
        except Exception as exc:
            resp = {"ok": False, "error": str(exc)}
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            snap = _MANAGER.snapshot()
            body = json.dumps({
                "ok": True, "model": "esm3",
                "device": snap["device"], "state": snap["state"],
                "pooled": snap["pooled"],
                "idle_release_seconds": _GPU_IDLE_RELEASE_SECONDS,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        print(f"[esm3-http] {self.address_string()} {fmt % args}", file=sys.stderr,
              flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9333
    threading.Thread(target=_idle_reaper_loop, daemon=True).start()
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[esm3-http] listening on :{port} "
          f"(pool-citizen GPU lifecycle, idle release {_GPU_IDLE_RELEASE_SECONDS:.0f}s)",
          flush=True)
    server.serve_forever()
