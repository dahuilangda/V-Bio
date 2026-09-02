from __future__ import annotations

from typing import Callable, Tuple

from flask import Flask, Response


def register_lead_opt_routes(
    app: Flask,
    *,
    forward_task_read: Callable[[str, str, str], Tuple[Response, int]],
    forward_quick_json: Callable[..., Tuple[Response, int]],
    forward_quick_multipart: Callable[..., Tuple[Response, int]],
    forward_quick_get: Callable[[str, str], Tuple[Response, int]],
    pocket_overlay_handler: Callable[[], Tuple[Response, int]],
) -> None:
    @app.post("/vbio-api/api/lead_optimization/fragment_preview")
    def lead_optimization_fragment_preview() -> Tuple[Response, int]:
        return forward_quick_json("/api/lead_optimization/fragment_preview", "lead_opt_fragment_preview", require_submit=True)

    @app.post("/vbio-api/api/lead_optimization/reference_preview")
    def lead_optimization_reference_preview() -> Tuple[Response, int]:
        return forward_quick_multipart("/api/lead_optimization/reference_preview", "lead_opt_reference_preview", require_submit=True)

    @app.post("/vbio-api/api/lead_optimization/pocket_overlay")
    def lead_optimization_pocket_overlay() -> Tuple[Response, int]:
        return pocket_overlay_handler()

    # HALO generative optimization (mmpdb retrieval pipeline retired).
    @app.post("/vbio-api/api/lead_optimization/halo_optimize")
    def lead_optimization_halo_optimize() -> Tuple[Response, int]:
        return forward_quick_json("/api/lead_optimization/halo_optimize", "lead_opt_halo_optimize", require_submit=True)

    @app.get("/vbio-api/api/lead_optimization/halo_backends")
    def lead_optimization_halo_backends() -> Tuple[Response, int]:
        return forward_quick_get("/api/lead_optimization/halo_backends", "lead_opt_halo_backends")

    @app.get("/vbio-api/api/lead_optimization/halo_status/<task_id>")
    def lead_optimization_halo_status(task_id: str) -> Tuple[Response, int]:
        # Task-scoped read: the same task-in-project ownership check as every
        # other task read, not the bare quick-get.
        return forward_task_read(
            task_id,
            "/api/lead_optimization/halo_status",
            "lead_opt_halo_status",
        )
