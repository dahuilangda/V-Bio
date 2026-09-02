import os
import hmac
import logging
import threading
from functools import wraps
from typing import Any, Dict, Optional
from flask import Flask, request, jsonify, send_from_directory

from backend.core import config
from backend.core.celery_app import celery_app
from backend.worker.tasks import (
    predict_task,
    boltz2score_task,
    export_tasks_excel_task,
)
from backend.worker.lead_opt_halo_task import lead_optimization_halo_task
from gpu_manager import get_redis_client, get_gpu_status
from backend.runtime.affinity_preview import AffinityPreviewError, build_affinity_preview
from backend.routes.admin import register_admin_routes
from backend.routes.task import register_task_routes
from backend.routes.affinity import register_affinity_routes
from backend.routes.lead_opt import register_lead_opt_routes
from backend.routes.lead_opt_halo import register_lead_opt_halo_routes
from backend.routes.prediction import register_prediction_routes
from backend.routes.export import register_export_routes
from backend.services.result_archive import ResultArchiveService
from backend.services.common_utils import (
    extract_template_meta_from_yaml,
    has_worker_for_queue,
    infer_use_msa_server_from_yaml_text,
    load_progress,
    normalize_chain_id_list,
    parse_bool,
    parse_int,
)
from backend.monitoring.task_monitor import TaskMonitor
from backend.monitoring.monitor_store import MonitorStore
from backend.scheduling.capability_router import (
    capability_from_prediction_backend,
    list_known_queues,
    resolve_queue_for_capability,
)

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

# 创建全局任务监控实例
task_monitor = TaskMonitor(logger=logger)

_monitor_store_lock = threading.Lock()
_monitor_store: MonitorStore | None = None
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.RESULTS_BASE_DIR
# Hard cap on request body size: structure uploads (PDB/CIF/SDF) are at most a few MB; a cap
# here stops an oversized/malicious upload from being fully buffered in memory before any
# handler-level validation runs.
app.config['MAX_CONTENT_LENGTH'] = config.MAX_UPLOAD_BYTES


@app.before_request
def _exempt_worker_result_uploads():
    # Result archives legitimately reach gigabytes (embedded MSA caches), and
    # /upload_result/<task_id> is called only by authenticated workers from inside the cluster
    # with files this very service asked them to produce. The external-upload cap must not
    # reject them at the final step of a completed GPU run. Werkzeug reads the limit lazily at
    # first body access, so clearing it here applies to this request only.
    if request.path.startswith('/upload_result/'):
        app.config['MAX_CONTENT_LENGTH'] = None
    else:
        app.config['MAX_CONTENT_LENGTH'] = config.MAX_UPLOAD_BYTES


@app.errorhandler(413)
def _request_entity_too_large(_error):
    # The default 413 body is an HTML page; API clients expect JSON with the limit named.
    return jsonify({
        'error': f'Request body exceeds the upload limit ({config.MAX_UPLOAD_BYTES} bytes).'
    }), 413

# Browser clients (V-Bio frontend) call this API directly.
# Enable permissive CORS by default so both localhost and remote host:port frontends can submit tasks.
_cors_origins_raw = os.environ.get("BOLTZ_CORS_ALLOW_ORIGINS", "*").strip()
if _cors_origins_raw == "*":
    _cors_origin_allowlist = None
else:
    _cors_origin_allowlist = {item.strip() for item in _cors_origins_raw.split(",") if item.strip()}

def _resolve_cors_origin() -> str:
    origin = request.headers.get("Origin")
    if _cors_origin_allowlist is None:
        return origin or "*"
    if origin and origin in _cors_origin_allowlist:
        return origin
    # Origin not allowlisted: emit no ACAO header so the browser blocks the response.
    return ""

def _apply_cors_headers(response):
    origin = _resolve_cors_origin()
    if origin:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Accept, X-API-Token, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Max-Age"] = "86400"
    vary = response.headers.get("Vary")
    response.headers["Vary"] = f"{vary}, Origin" if vary else "Origin"
    return response

@app.before_request
def handle_cors_preflight():
    if request.method == "OPTIONS":
        return _apply_cors_headers(app.make_response(("", 204)))
    return None

@app.after_request
def add_cors_headers(response):
    return _apply_cors_headers(response)

# MSA 缓存配置（与监控 GC 共用同一目录与保留期）
MSA_CACHE_CONFIG = {
    'cache_dir': config.BOLTZ_MSA_CACHE_DIR,
    'max_age_days': config.MSA_CACHE_RETENTION_DAYS,
    'max_size_gb': 5,
    'enable_cache': True
}

os.makedirs(config.RESULTS_BASE_DIR, exist_ok=True)
os.makedirs(config.EXPORTS_BASE_DIR, exist_ok=True)
logger.info(f"Results base directory ensured: {config.RESULTS_BASE_DIR}")
logger.info(f"Excel export directory ensured: {config.EXPORTS_BASE_DIR}")

if config.BOLTZ_API_TOKEN == "development-api-token":
    logger.warning(
        "BOLTZ_API_TOKEN is the built-in development default; set the BOLTZ_API_TOKEN env var in "
        "production. The API is currently protected only by a public token."
    )

result_archive_service = ResultArchiveService(
    app=app,
    celery_app=celery_app,
    logger=logger,
    get_redis_client_fn=get_redis_client,
)


def download_results(task_id: str):
    """Shared download handler used by prediction/lead-opt route modules."""
    logger.info('Received shared download request for task ID: %s', task_id)
    try:
        filename, filepath = result_archive_service.resolve_result_archive_path(task_id)
    except FileNotFoundError as exc:
        logger.warning('Failed to resolve results for task %s: %s', task_id, exc)
        return jsonify({'error': str(exc)}), 404
    except PermissionError as exc:
        logger.error('Invalid result path for task %s: %s', task_id, exc)
        return jsonify({'error': 'Invalid file path detected.'}), 400
    except Exception as exc:
        logger.exception('Unexpected error while resolving results for task %s: %s', task_id, exc)
        return jsonify({'error': f'Failed to resolve result archive: {exc}'}), 500

    directory = app.config['UPLOAD_FOLDER']
    logger.info('Serving full result file %s for task %s from %s.', filename, task_id, filepath)
    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
        conditional=False,
        etag=False,
        max_age=0,
    )

# --- Authentication Decorator ---
def require_api_token(f):
    """
    Decorator to validate API token from request headers.
    Logs unauthorized access attempts.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('X-API-Token')
        if not token or not hasattr(config, 'BOLTZ_API_TOKEN') or not hmac.compare_digest(token, config.BOLTZ_API_TOKEN):
            logger.warning(f"Unauthorized API access attempt from {request.remote_addr} to {request.path}")
            return jsonify({'error': 'Unauthorized. Invalid or missing API token.'}), 403
        logger.debug(f"API token validated for {request.path} from {request.remote_addr}")
        return f(*args, **kwargs)
    return decorated_function

def _load_progress(redis_key: str) -> Optional[Dict]:
    return load_progress(redis_key, get_redis_client_fn=get_redis_client, logger=logger)

def _has_worker_for_queue(queue_name: str) -> bool:
    return has_worker_for_queue(queue_name, celery_app=celery_app, logger=logger)


def _select_queue_for_capability(capability: str, priority: str = 'default') -> Dict[str, Any]:
    return resolve_queue_for_capability(
        capability=capability,
        priority=priority,
        has_worker_for_queue_fn=_has_worker_for_queue,
    )


def _capability_from_prediction_backend(backend: str) -> str:
    return capability_from_prediction_backend(backend)


def _list_known_queues() -> list[str]:
    return list_known_queues()


def _get_worker_capability_snapshot() -> Dict[str, Any]:
    global _monitor_store
    if _monitor_store is None:
        database_url = os.environ.get("VBIO_MONITOR_DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("VBIO_MONITOR_DATABASE_URL is required for worker snapshots")
        with _monitor_store_lock:
            if _monitor_store is None:
                _monitor_store = MonitorStore(database_url, min_connections=1, max_connections=4)
    return _monitor_store.get_overview(window_hours=24, recent_limit=1)["cluster"]

register_prediction_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    config_module=config,
    predict_task=predict_task,
    parse_int=parse_int,
    parse_bool=parse_bool,
    infer_use_msa_server_from_yaml_text=infer_use_msa_server_from_yaml_text,
    extract_template_meta_from_yaml=extract_template_meta_from_yaml,
    normalize_chain_id_list=normalize_chain_id_list,
    select_queue_for_capability=_select_queue_for_capability,
    capability_from_prediction_backend=_capability_from_prediction_backend,
)


from backend.routes.lead_opt_helpers import (
    attachment_fragment_smiles_from_atom_indices,
    decode_smiles_atom_index_from_name,
)

register_lead_opt_halo_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    celery_app=celery_app,
    lead_optimization_halo_task=lead_optimization_halo_task,
    select_queue_for_capability=_select_queue_for_capability,
    has_worker_for_queue=_has_worker_for_queue,
    load_progress=_load_progress,
)

register_lead_opt_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    build_affinity_preview=build_affinity_preview,
    affinity_preview_error_cls=AffinityPreviewError,
    attachment_fragment_smiles_from_atom_indices=attachment_fragment_smiles_from_atom_indices,
    decode_smiles_atom_index_from_name=decode_smiles_atom_index_from_name,
)


register_affinity_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    config_module=config,
    boltz2score_task=boltz2score_task,
    build_affinity_preview=build_affinity_preview,
    affinity_preview_error_cls=AffinityPreviewError,
    parse_bool=parse_bool,
    parse_int=parse_int,
    select_queue_for_capability=_select_queue_for_capability,
)


register_task_routes(
    app,
    require_api_token=require_api_token,
    celery_app=celery_app,
    task_monitor=task_monitor,
    predict_task=predict_task,
    config_module=config,
    logger=logger,
    find_result_archive=result_archive_service.find_result_archive,
    resolve_result_archive_path=result_archive_service.resolve_result_archive_path,
    build_or_get_view_archive=result_archive_service.build_or_get_view_archive,
    get_tracker_status=result_archive_service.get_tracker_status,
    get_compact_prediction_metrics=result_archive_service.get_compact_prediction_metrics,
    list_known_queues=_list_known_queues,
    get_worker_capability_snapshot=_get_worker_capability_snapshot,
)

register_export_routes(
    app,
    require_api_token=require_api_token,
    celery_app=celery_app,
    logger=logger,
    config_module=config,
    export_tasks_excel_task=export_tasks_excel_task,
    select_queue_for_capability=_select_queue_for_capability,
    get_redis_client_fn=get_redis_client,
)


register_admin_routes(
    app,
    require_api_token=require_api_token,
    msa_cache_config=MSA_CACHE_CONFIG,
    colabfold_jobs_dir=config.COLABFOLD_JOBS_DIR,
    logger=logger,
    task_monitor=task_monitor,
    get_gpu_status_fn=get_gpu_status,
)

if __name__ == '__main__':
    # For production, use a WSGI server like Gunicorn/uWSGI instead of app.run().
    debug = os.environ.get('FLASK_DEBUG', '').lower() in {'1', 'true', 'yes'}
    logger.info("Starting Flask API server (debug=%s)...", debug)
    app.run(host='0.0.0.0', port=5000, debug=debug)
