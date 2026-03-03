from __future__ import annotations

from typing import Any, Callable, Tuple
from urllib.parse import quote

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
    @app.get("/vbio-api/api/lead_optimization/status/<task_id>")
    def get_lead_optimization_status(task_id: str) -> Tuple[Response, int]:
        return forward_task_read(task_id, "/api/lead_optimization/status", "read_lead_optimization_status")

    @app.get("/vbio-api/api/lead_optimization/results/<task_id>")
    def get_lead_optimization_results(task_id: str) -> Tuple[Response, int]:
        return forward_task_read(task_id, "/api/lead_optimization/results", "read_lead_optimization_results")

    @app.post("/vbio-api/api/lead_optimization/fragment_preview")
    def lead_optimization_fragment_preview() -> Tuple[Response, int]:
        return forward_quick_json("/api/lead_optimization/fragment_preview", "lead_opt_fragment_preview", require_submit=True)

    @app.post("/vbio-api/api/lead_optimization/reference_preview")
    def lead_optimization_reference_preview() -> Tuple[Response, int]:
        return forward_quick_multipart("/api/lead_optimization/reference_preview", "lead_opt_reference_preview", require_submit=True)

    @app.post("/vbio-api/api/lead_optimization/pocket_overlay")
    def lead_optimization_pocket_overlay() -> Tuple[Response, int]:
        return pocket_overlay_handler()

    @app.post("/vbio-api/api/lead_optimization/mmp_query")
    def lead_optimization_mmp_query() -> Tuple[Response, int]:
        return forward_quick_json("/api/lead_optimization/mmp_query", "lead_opt_mmp_query", require_submit=True)

    @app.get("/vbio-api/api/lead_optimization/mmp_databases")
    def lead_optimization_mmp_databases() -> Tuple[Response, int]:
        return forward_quick_get("/api/lead_optimization/mmp_databases", "lead_opt_mmp_databases")

    @app.get("/vbio-api/api/lead_optimization/mmp_query_status/<task_id>")
    def lead_optimization_mmp_query_status(task_id: str) -> Tuple[Response, int]:
        return forward_quick_get(
            f"/api/lead_optimization/mmp_query_status/{quote(task_id, safe='')}",
            "lead_opt_mmp_query_status",
        )

    @app.post("/vbio-api/api/lead_optimization/mmp_cluster")
    def lead_optimization_mmp_cluster() -> Tuple[Response, int]:
        return forward_quick_json("/api/lead_optimization/mmp_cluster", "lead_opt_mmp_cluster", require_submit=True)

    @app.get("/vbio-api/api/lead_optimization/mmp_evidence/<transform_id>")
    def lead_optimization_mmp_evidence(transform_id: str) -> Tuple[Response, int]:
        return forward_quick_get(
            f"/api/lead_optimization/mmp_evidence/{quote(transform_id, safe='')}",
            "lead_opt_mmp_evidence",
        )

    @app.post("/vbio-api/api/lead_optimization/mmp_enumerate")
    def lead_optimization_mmp_enumerate() -> Tuple[Response, int]:
        return forward_quick_json("/api/lead_optimization/mmp_enumerate", "lead_opt_mmp_enumerate", require_submit=True)

    @app.post("/vbio-api/api/lead_optimization/predict_candidate")
    def lead_optimization_predict_candidate() -> Tuple[Response, int]:
        return forward_quick_json("/api/lead_optimization/predict_candidate", "lead_opt_predict_candidate", require_submit=True)
