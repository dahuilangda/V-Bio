from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import requests
from flask import Response, request

from management_api.auth_service import AuthService, TokenContext
from management_api.gateway_quick_forward import forward_quick_get, forward_quick_json, forward_quick_multipart
from management_api.gateway_quick_overlay import handle_lead_optimization_pocket_overlay
from management_api.gateway_submit import forward_submit
from management_api.gateway_task import cancel_or_delete_task, forward_task_read
from management_api.lead_opt_overlay import LeadOptOverlayService
from management_api.runtime_proxy import RuntimeProxy
from management_api.task_store import ProjectTaskStore
from management_api.usage_tracker import UsageTracker


class GatewayHandlers:
    def __init__(
        self,
        *,
        auth_service: AuthService,
        usage_tracker: UsageTracker,
        runtime_proxy: RuntimeProxy,
        task_store: ProjectTaskStore,
        lead_opt_overlay_service: LeadOptOverlayService,
        logger: Any,
        default_protenix_predict_seed: int,
    ) -> None:
        self.auth_service = auth_service
        self.usage_tracker = usage_tracker
        self.runtime_proxy = runtime_proxy
        self.task_store = task_store
        self.lead_opt_overlay_service = lead_opt_overlay_service
        self.logger = logger
        self.default_protenix_predict_seed = int(default_protenix_predict_seed)

    def _record_usage(
        self,
        token: Optional[TokenContext],
        *,
        action: str,
        status_code: int,
        succeeded: bool,
        started_at: float,
        project_id: Optional[str],
        task_id: Optional[str],
    ) -> None:
        self.usage_tracker.record_usage(
            token=token,
            action=action,
            status_code=status_code,
            succeeded=succeeded,
            started_at=started_at,
            project_id=project_id,
            task_id=task_id,
        )

    def _forbidden(
        self,
        message: str,
        token: Optional[TokenContext],
        action: str,
        started_at: float,
        project_id: Optional[str],
    ) -> Tuple[Response, int]:
        return self.usage_tracker.forbidden(
            message=message,
            token=token,
            action=action,
            started_at=started_at,
            project_id=project_id,
        )

    @staticmethod
    def _task_backend_label(path: str, form_backend: str) -> str:
        backend = (form_backend or "").strip().lower()
        if backend:
            return backend
        if path.endswith("/api/protenix2score"):
            return "protenix"
        return "boltz"

    @staticmethod
    def _read_project_id_from_form() -> str:
        project_id = (request.form.get("project_id") or "").strip()
        if not project_id:
            raise PermissionError("project_id is required")
        return project_id

    @staticmethod
    def _read_project_id_from_query() -> str:
        project_id = (request.args.get("project_id") or "").strip()
        if not project_id:
            raise PermissionError("project_id query is required")
        return project_id

    def _authorize_submit(self, project_id: str, token_plain: str) -> TokenContext:
        return self.auth_service.authorize_submit(project_id, token_plain)

    def _authorize_task_action(
        self,
        project_id: str,
        token_plain: str,
        *,
        require_delete: bool,
    ) -> TokenContext:
        return self.auth_service.authorize_task_action(project_id, token_plain, require_delete=require_delete)

    def _authorize_project_read(self, project_id: str, token_plain: str) -> TokenContext:
        return self.auth_service.authorize_project_read(project_id, token_plain)

    def _authorize_quick_project_action(
        self,
        token_plain: str,
        project_id: Optional[str],
        *,
        require_submit: bool,
    ) -> TokenContext:
        return self.auth_service.authorize_quick_project_action(
            token_plain=token_plain,
            project_id=project_id,
            require_submit=require_submit,
        )

    def _proxy_multipart(self, upstream_path: str) -> requests.Response:
        return self.runtime_proxy.proxy_multipart(request, upstream_path)

    def _proxy_delete(self, upstream_path: str, passthrough_query: Dict[str, str]) -> requests.Response:
        return self.runtime_proxy.proxy_delete(upstream_path, passthrough_query)

    def _proxy_get(self, upstream_path: str, passthrough_query: Dict[str, str]) -> requests.Response:
        return self.runtime_proxy.proxy_get(upstream_path, passthrough_query)

    def _proxy_post_json(self, upstream_path: str, payload: Dict[str, Any]) -> requests.Response:
        return self.runtime_proxy.proxy_post_json(upstream_path, payload)

    def _build_flask_response(self, upstream: requests.Response) -> Tuple[Response, int]:
        return self.runtime_proxy.build_flask_response(upstream)

    def forward_submit(self, upstream_path: str, action: str) -> Tuple[Response, int]:
        return forward_submit(self, upstream_path, action)

    def forward_quick_json(
        self,
        upstream_path: str,
        action: str,
        *,
        require_submit: bool = False,
    ) -> Tuple[Response, int]:
        return forward_quick_json(self, upstream_path, action, require_submit=require_submit)

    def forward_quick_multipart(
        self,
        upstream_path: str,
        action: str,
        *,
        require_submit: bool = False,
    ) -> Tuple[Response, int]:
        return forward_quick_multipart(self, upstream_path, action, require_submit=require_submit)

    def forward_quick_get(self, upstream_path: str, action: str) -> Tuple[Response, int]:
        return forward_quick_get(self, upstream_path, action)

    def forward_task_read(self, task_id: str, upstream_prefix: str, action: str) -> Tuple[Response, int]:
        return forward_task_read(self, task_id, upstream_prefix, action)

    def handle_lead_optimization_pocket_overlay(self) -> Tuple[Response, int]:
        return handle_lead_optimization_pocket_overlay(self)

    def cancel_or_delete_task(self, task_id: str) -> Tuple[Response, int]:
        return cancel_or_delete_task(self, task_id)
