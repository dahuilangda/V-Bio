# api_server.py

import os
from functools import wraps
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename
from celery.result import AsyncResult
import config
from celery_app import celery_app
from tasks import predict_task

app = Flask(__name__)

os.makedirs(config.RESULTS_BASE_DIR, exist_ok=True)
app.config['UPLOAD_FOLDER'] = config.RESULTS_BASE_DIR

def require_api_token(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('X-API-Token')
        if not token or token != config.API_SECRET_TOKEN:
            return jsonify({'error': 'Unauthorized. Invalid or missing API token.'}), 403
        return f(*args, **kwargs)
    return decorated_function


@app.route('/predict', methods=['POST'])
@require_api_token
def handle_predict():
    """
    接收来自客户端的预测请求，现在需要 API 令牌验证。
    """
    if 'yaml_file' not in request.files:
        return jsonify({'error': "Request form must contain a 'yaml_file' part"}), 400

    yaml_file = request.files['yaml_file']
    
    try:
        yaml_content = yaml_file.read().decode('utf-8')
    except Exception as e:
        return jsonify({'error': f"Failed to read or decode yaml_file: {e}"}), 400

    use_msa_server = request.form.get('use_msa_server', 'false').lower() == 'true'
    
    priority = request.form.get('priority', 'default')
    target_queue = 'high_priority' if priority == 'high' else 'default'

    predict_args = {
        'yaml_content': yaml_content,
        'use_msa_server': use_msa_server
    }

    task = predict_task.apply_async(args=[predict_args], queue=target_queue)
    print(f"Task {task.id} dispatched to queue: '{target_queue}'")
    
    return jsonify({'task_id': task.id}), 202

@app.route('/status/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """
    查询指定 task_id 的 Celery 任务状态 (Public Endpoint)。
    """
    task_result = AsyncResult(task_id, app=celery_app)
    
    response = {
        'task_id': task_id,
        'state': task_result.state
    }
    
    info = task_result.info
    if isinstance(info, dict):
        response['info'] = info
    elif isinstance(info, Exception):
        response['info'] = {'exc_type': type(info).__name__, 'exc_message': str(info)}
    else:
        response['info'] = {'message': str(info)}

    if task_result.state == 'PENDING':
        response['info'] = {'status': 'Task is waiting in the queue.'}
    elif task_result.state == 'SUCCESS' and isinstance(response.get('info'), dict):
        response['info']['status'] = 'Task completed successfully.'
    
    return jsonify(response)


@app.route('/tasks', methods=['GET'])
@require_api_token
def list_tasks():
    """
    列出所有活跃 (running) 和排队中 (queued/reserved) 的任务。
    """
    inspector = celery_app.control.inspect()
    
    try:
        active = inspector.active() or {}
        reserved = inspector.reserved() or {}
        scheduled = inspector.scheduled() or {}

        all_tasks = {
            'active': [task for worker_tasks in active.values() for task in worker_tasks],
            'reserved': [task for worker_tasks in reserved.values() for task in worker_tasks],
            'scheduled': [task for worker_tasks in scheduled.values() for task in worker_tasks],
        }
        return jsonify(all_tasks)
    except Exception as e:
        return jsonify({'error': 'Could not inspect Celery workers.', 'details': str(e)}), 500

@app.route('/tasks/<task_id>', methods=['DELETE'])
@require_api_token
def terminate_task(task_id):
    """
    终止一个正在运行或在队列中的任务。
    """
    try:
        # terminate=True sends a SIGTERM to the running process.
        celery_app.control.revoke(task_id, terminate=True)
        return jsonify({'status': 'revoked', 'task_id': task_id})
    except Exception as e:
        return jsonify({'error': 'Failed to revoke task.', 'details': str(e)}), 500

@app.route('/tasks/<task_id>/move', methods=['POST'])
@require_api_token
def move_task(task_id):
    """
    将一个排队中的任务移动到另一个队列以改变其优先级。
    注意：此操作只对尚未开始执行 (reserved/queued) 的任务有效。
    """
    data = request.get_json()
    target_queue = data.get('target_queue')

    if not target_queue or target_queue not in ['high_priority', 'default']:
        return jsonify({'error': "Invalid 'target_queue'. Must be 'high_priority' or 'default'."}), 400

    inspector = celery_app.control.inspect()
    try:
        reserved_tasks = inspector.reserved() or {}
        task_info = None
        
        # 查找任务的详细信息
        for worker, tasks in reserved_tasks.items():
            for task in tasks:
                if task['id'] == task_id:
                    task_info = task
                    break
            if task_info:
                break
        
        if not task_info:
            return jsonify({'error': 'Task not found in reserved queue. It might be already running or completed.'}), 404

        # 1. 撤销旧任务，防止它被执行
        celery_app.control.revoke(task_id)

        # 2. 用相同的参数重新提交到新队列
        original_args = task_info.get('args', [])
        new_task = predict_task.apply_async(
            args=original_args,
            queue=target_queue
        )

        return jsonify({
            'status': 'moved',
            'original_task_id': task_id,
            'new_task_id': new_task.id,
            'target_queue': target_queue
        }), 200

    except Exception as e:
        return jsonify({'error': 'Failed to move task.', 'details': str(e)}), 500


@app.route('/upload_result/<task_id>', methods=['POST'])
def upload_result_from_worker(task_id):
    """
    接收来自 Celery worker 的文件上传 (Internal Endpoint)。
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file part in the request'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    if file:
        filename = secure_filename(file.filename)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(save_path)
        print(f"Result for task {task_id} saved to {save_path}")
        return jsonify({'message': 'File uploaded successfully'}), 200
    return jsonify({'error': 'File upload failed'}), 500

@app.route('/results/<task_id>', methods=['GET'])
def download_results(task_id):
    """
    允许前端根据 task_id 下载最终的结果文件 (Public Endpoint)。
    """
    filename = f"{task_id}_results.zip"
    directory = app.config['UPLOAD_FOLDER']
    if not os.path.exists(os.path.join(directory, filename)):
        return jsonify({'error': 'Result file not found.'}), 404
    return send_from_directory(directory, filename, as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)