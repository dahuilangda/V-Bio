import os
import logging
from functools import wraps
from typing import Dict, Optional
from flask import Flask, request, jsonify, send_from_directory
import config
from celery_app import celery_app
from tasks import (
    predict_task,
    affinity_task,
    boltz2score_task,
    protenix2score_task,
    virtual_screening_task,
    lead_optimization_task,
    lead_optimization_mmp_query_task,
)
from gpu_manager import get_redis_client, get_gpu_status
from affinity_preview import AffinityPreviewError, build_affinity_preview
from lead_optimization.mmp_query_service import run_mmp_query as run_mmp_query_service
from api_server_admin_routes import register_admin_routes
from api_server_task_routes import register_task_routes
from api_server_affinity_routes import register_affinity_routes
from api_server_lead_opt_mmp_routes import register_lead_opt_mmp_routes
from api_server_mmp_lifecycle_routes import register_mmp_lifecycle_admin_routes
from api_server_lead_opt_routes import register_lead_opt_routes
from api_server_prediction_routes import register_prediction_routes
from api_server_result_archive import ResultArchiveService
from api_server_mmp_service import LeadOptMmpService
from api_server_common_utils import (
    extract_template_meta_from_yaml,
    has_worker_for_queue,
    infer_use_msa_server_from_yaml_text,
    load_progress,
    normalize_chain_id_list,
    parse_bool,
    parse_float,
    parse_int,
)
from task_monitor import TaskMonitor

# --- Configure Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s'
)
logger = logging.getLogger(__name__)

# 创建全局任务监控实例
task_monitor = TaskMonitor(logger=logger)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = config.RESULTS_BASE_DIR

# Browser clients (VBio) call this API directly, while legacy frontend uses server-side requests.
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
    # Fall back to first configured origin to keep browser behavior deterministic.
    return next(iter(_cors_origin_allowlist), "")

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

# MSA 缓存配置
MSA_CACHE_CONFIG = {
    'cache_dir': '/tmp/boltz_msa_cache',
    'max_age_days': 7,  # 缓存文件最大保存天数
    'max_size_gb': 5,   # 缓存目录最大大小（GB）
    'enable_cache': True
}

try:
    os.makedirs(config.RESULTS_BASE_DIR, exist_ok=True)
    logger.info(f"Results base directory ensured: {config.RESULTS_BASE_DIR}")
except OSError as e:
    logger.critical(f"Failed to create results directory {config.RESULTS_BASE_DIR}: {e}")

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
        if not token or not hasattr(config, 'BOLTZ_API_TOKEN') or token != config.BOLTZ_API_TOKEN:
            logger.warning(f"Unauthorized API access attempt from {request.remote_addr} to {request.path}")
            return jsonify({'error': 'Unauthorized. Invalid or missing API token.'}), 403
        logger.debug(f"API token validated for {request.path} from {request.remote_addr}")
        return f(*args, **kwargs)
    return decorated_function

_parse_bool = parse_bool
_parse_int = parse_int
_parse_float = parse_float
_normalize_chain_id_list = normalize_chain_id_list
_infer_use_msa_server_from_yaml_text = infer_use_msa_server_from_yaml_text
_extract_template_meta_from_yaml = extract_template_meta_from_yaml

lead_opt_mmp_service = LeadOptMmpService(
    get_redis_client_fn=get_redis_client,
    logger=logger,
    mmp_query_cache_dir=getattr(config, 'LEAD_OPT_MMP_QUERY_CACHE_DIR', ''),
)

def _load_progress(redis_key: str) -> Optional[Dict]:
    return load_progress(redis_key, get_redis_client_fn=get_redis_client, logger=logger)

def _has_worker_for_queue(queue_name: str) -> bool:
    return has_worker_for_queue(queue_name, celery_app=celery_app, logger=logger)

register_prediction_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    config_module=config,
    celery_app=celery_app,
    predict_task=predict_task,
    virtual_screening_task=virtual_screening_task,
    parse_bool=_parse_bool,
    parse_int=_parse_int,
    parse_float=_parse_float,
    infer_use_msa_server_from_yaml_text=_infer_use_msa_server_from_yaml_text,
    extract_template_meta_from_yaml=_extract_template_meta_from_yaml,
    normalize_chain_id_list=_normalize_chain_id_list,
    load_progress=_load_progress,
    download_results=download_results,
)


register_lead_opt_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    build_affinity_preview=build_affinity_preview,
    affinity_preview_error_cls=AffinityPreviewError,
    attachment_fragment_smiles_from_atom_indices=lead_opt_mmp_service.attachment_fragment_smiles_from_atom_indices,
    decode_smiles_atom_index_from_name=lead_opt_mmp_service.decode_smiles_atom_index_from_name,
)


register_lead_opt_mmp_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    config_module=config,
    celery_app=celery_app,
    predict_task=predict_task,
    lead_optimization_mmp_query_task=lead_optimization_mmp_query_task,
    parse_bool=_parse_bool,
    parse_int=_parse_int,
    load_progress=_load_progress,
    has_worker_for_queue=_has_worker_for_queue,
    run_mmp_query_service=run_mmp_query_service,
    materialize_mmp_query_result_cache=lead_opt_mmp_service.materialize_query_result_cache,
    get_cached_mmp_query_id_for_task=lead_opt_mmp_service.get_cached_query_id_for_task,
    get_cached_mmp_query_payload=lead_opt_mmp_service.get_cached_query_payload,
    get_cached_mmp_evidence_payload=lead_opt_mmp_service.get_cached_evidence_payload,
    build_mmp_clusters=lead_opt_mmp_service.build_mmp_clusters,
    safe_json_object=lead_opt_mmp_service.safe_json_object,
    compute_smiles_properties=lead_opt_mmp_service.compute_smiles_properties,
    passes_property_constraints_simple=lead_opt_mmp_service.passes_property_constraints_simple,
    build_lead_opt_prediction_yaml=lead_opt_mmp_service.build_lead_opt_prediction_yaml,
    download_results=download_results,
)


register_affinity_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
    config_module=config,
    affinity_task=affinity_task,
    boltz2score_task=boltz2score_task,
    protenix2score_task=protenix2score_task,
    build_affinity_preview=build_affinity_preview,
    affinity_preview_error_cls=AffinityPreviewError,
    parse_bool=_parse_bool,
    parse_int=_parse_int,
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

register_mmp_lifecycle_admin_routes(
    app,
    require_api_token=require_api_token,
    logger=logger,
)


if __name__ == '__main__':
    # For production, use a WSGI server like Gunicorn/uWSGI instead of app.run(debug=True).
    logger.info("Starting Flask API server...")
    app.run(host='0.0.0.0', port=5000, debug=True)
