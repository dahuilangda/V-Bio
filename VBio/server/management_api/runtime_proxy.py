from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Tuple

import requests
import yaml
from flask import Response


class RuntimeProxyBusyError(RuntimeError):
    """Raised when management runtime forwarder is saturated."""


def read_upload_text(upload: Any) -> str:
    if upload is None:
        return ""
    stream = getattr(upload, "stream", None)
    try:
        if stream is not None:
            stream.seek(0)
        raw = upload.read()
        if isinstance(raw, str):
            return raw
        try:
            return raw.decode("utf-8")
        except Exception:
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""
    finally:
        try:
            if stream is not None:
                stream.seek(0)
        except Exception:
            pass


def infer_use_msa_server_from_yaml_text(yaml_text: str) -> bool:
    if not yaml_text.strip():
        return False

    try:
        yaml_data = yaml.safe_load(yaml_text) or {}
    except Exception:
        return False
    if not isinstance(yaml_data, dict):
        return False

    sequences = yaml_data.get("sequences")
    if not isinstance(sequences, list):
        return False

    has_protein = False
    needs_external_msa = False
    for item in sequences:
        if not isinstance(item, dict):
            continue
        protein = item.get("protein")
        if not isinstance(protein, dict):
            continue
        has_protein = True
        msa_value = protein.get("msa")
        if msa_value is None:
            needs_external_msa = True
            continue
        if isinstance(msa_value, str):
            normalized = msa_value.strip().lower()
            if not normalized:
                needs_external_msa = True
                continue
            if normalized in {"empty", "none", "null"}:
                continue
            continue

    return has_protein and needs_external_msa


class RuntimeProxy:
    def __init__(
        self,
        *,
        runtime_api_base_url: str,
        runtime_api_token: str,
        runtime_timeout_seconds: float,
        session: requests.Session,
        logger: Any,
        form_fields_internal: set[str],
        default_protenix_predict_seed: int,
        max_inflight_requests: int = 128,
        status_history_size: int = 200,
    ) -> None:
        self.runtime_api_base_url = runtime_api_base_url.rstrip("/")
        self.runtime_api_token = runtime_api_token.strip()
        self.runtime_timeout_seconds = float(runtime_timeout_seconds)
        self.session = session
        self.logger = logger
        self.form_fields_internal = set(form_fields_internal)
        self.default_protenix_predict_seed = int(default_protenix_predict_seed)
        self.max_inflight_requests = max(1, int(max_inflight_requests))
        self._inflight_slots = threading.BoundedSemaphore(self.max_inflight_requests)
        self._state_lock = threading.Lock()
        self._active_requests: Dict[str, Dict[str, Any]] = {}
        self._history = deque(maxlen=max(10, int(status_history_size)))
        self._rejected_requests = 0
        self._completed_requests = 0

    def _enter_request(self, method: str, upstream_path: str) -> str:
        if not self._inflight_slots.acquire(blocking=False):
            with self._state_lock:
                self._rejected_requests += 1
            raise RuntimeProxyBusyError(
                f"management runtime gateway busy ({self.max_inflight_requests} in-flight); retry shortly."
            )
        request_id = uuid.uuid4().hex
        now = time.time()
        with self._state_lock:
            self._active_requests[request_id] = {
                "request_id": request_id,
                "method": method,
                "path": upstream_path,
                "started_at": now,
            }
        return request_id

    def _exit_request(self, request_id: str, *, status_code: int, error: str = "") -> None:
        now = time.time()
        with self._state_lock:
            payload = self._active_requests.pop(request_id, None)
            if payload is not None:
                elapsed = max(0.0, now - float(payload.get("started_at") or now))
                payload["elapsed_seconds"] = elapsed
                payload["status_code"] = int(status_code or 0)
                if error:
                    payload["error"] = str(error)[:240]
                self._history.appendleft(payload)
            self._completed_requests += 1
        self._inflight_slots.release()

    def get_runtime_status(self) -> Dict[str, Any]:
        now = time.time()
        with self._state_lock:
            active = []
            for payload in self._active_requests.values():
                started_at = float(payload.get("started_at") or now)
                active.append(
                    {
                        "request_id": str(payload.get("request_id") or ""),
                        "method": str(payload.get("method") or ""),
                        "path": str(payload.get("path") or ""),
                        "age_seconds": max(0.0, now - started_at),
                    }
                )
            active.sort(key=lambda item: item["age_seconds"], reverse=True)
            history = list(self._history)[:50]
            return {
                "max_inflight_requests": self.max_inflight_requests,
                "inflight_requests": len(self._active_requests),
                "available_slots": max(0, self.max_inflight_requests - len(self._active_requests)),
                "rejected_requests": int(self._rejected_requests),
                "completed_requests": int(self._completed_requests),
                "active_requests": active,
                "recent_requests": history,
            }

    def _ensure_token(self) -> None:
        if not self.runtime_api_token:
            raise RuntimeError("VBIO_RUNTIME_API_TOKEN (or BOLTZ_API_TOKEN) is not configured")

    def proxy_multipart(self, request_obj: Any, upstream_path: str) -> requests.Response:
        data: List[Tuple[str, str]] = []
        for key in request_obj.form.keys():
            if key in self.form_fields_internal:
                continue
            for value in request_obj.form.getlist(key):
                data.append((key, value))

        if upstream_path == "/predict":
            raw_use_msa = request_obj.form.get("use_msa_server")
            if raw_use_msa is None or not str(raw_use_msa).strip():
                yaml_upload = request_obj.files.get("yaml_file")
                yaml_text = read_upload_text(yaml_upload)
                inferred_use_msa = infer_use_msa_server_from_yaml_text(yaml_text)
                data.append(("use_msa_server", "true" if inferred_use_msa else "false"))
                self.logger.info(
                    "Auto-filled use_msa_server=%s for /predict because the form field was missing.",
                    inferred_use_msa,
                )

            raw_seed = request_obj.form.get("seed")
            form_backend = str(request_obj.form.get("backend") or "boltz").strip().lower()
            if (raw_seed is None or not str(raw_seed).strip()) and form_backend == "protenix":
                data.append(("seed", str(self.default_protenix_predict_seed)))
                self.logger.info(
                    "Auto-filled seed=%s for /predict because backend=protenix and the form field was missing.",
                    self.default_protenix_predict_seed,
                )

        files: List[Tuple[str, Tuple[str, Any, str]]] = []
        for key in request_obj.files.keys():
            for fs in request_obj.files.getlist(key):
                filename = fs.filename or "upload.bin"
                mimetype = fs.mimetype or "application/octet-stream"
                fs.stream.seek(0)
                files.append((key, (filename, fs.stream, mimetype)))

        self._ensure_token()
        request_id = self._enter_request("POST", upstream_path)
        status_code = 0
        error_text = ""
        try:
            response = self.session.post(
                f"{self.runtime_api_base_url}{upstream_path}",
                headers={"X-API-Token": self.runtime_api_token, "Accept": "application/json"},
                data=data,
                files=files,
                timeout=self.runtime_timeout_seconds,
            )
            status_code = int(response.status_code or 0)
            return response
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            self._exit_request(request_id, status_code=status_code, error=error_text)

    def proxy_delete(self, upstream_path: str, passthrough_query: Dict[str, str]) -> requests.Response:
        self._ensure_token()
        request_id = self._enter_request("DELETE", upstream_path)
        status_code = 0
        error_text = ""
        try:
            response = self.session.delete(
                f"{self.runtime_api_base_url}{upstream_path}",
                params=passthrough_query,
                headers={"X-API-Token": self.runtime_api_token, "Accept": "application/json"},
                timeout=self.runtime_timeout_seconds,
            )
            status_code = int(response.status_code or 0)
            return response
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            self._exit_request(request_id, status_code=status_code, error=error_text)

    def proxy_get(self, upstream_path: str, passthrough_query: Dict[str, str]) -> requests.Response:
        self._ensure_token()
        request_id = self._enter_request("GET", upstream_path)
        status_code = 0
        error_text = ""
        try:
            response = self.session.get(
                f"{self.runtime_api_base_url}{upstream_path}",
                params=passthrough_query,
                headers={"X-API-Token": self.runtime_api_token, "Accept": "application/json"},
                timeout=self.runtime_timeout_seconds,
            )
            status_code = int(response.status_code or 0)
            return response
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            self._exit_request(request_id, status_code=status_code, error=error_text)

    def proxy_post_json(self, upstream_path: str, payload: Dict[str, Any]) -> requests.Response:
        self._ensure_token()
        request_id = self._enter_request("POST", upstream_path)
        status_code = 0
        error_text = ""
        try:
            response = self.session.post(
                f"{self.runtime_api_base_url}{upstream_path}",
                headers={
                    "X-API-Token": self.runtime_api_token,
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.runtime_timeout_seconds,
            )
            status_code = int(response.status_code or 0)
            return response
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            self._exit_request(request_id, status_code=status_code, error=error_text)

    @staticmethod
    def build_flask_response(upstream: requests.Response) -> Tuple[Response, int]:
        content_type = upstream.headers.get("Content-Type", "application/json")
        response = Response(upstream.content, status=upstream.status_code, content_type=content_type)
        return response, upstream.status_code
