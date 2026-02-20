#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

# Load environment variables from .env if present
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    . ".env"
    set +a
fi

# --- Environment Setup ---
# Activate virtual environment if it exists. This is a good practice.
if [ -d "venv" ]; then
    echo "Activating Python virtual environment..."
    source venv/bin/activate
else
    echo "Warning: No virtual environment 'venv' found. Using system Python."
fi

# --- Function Definitions ---

# Function to initialize the GPU pool in Redis
initialize_pool() {
    echo "Initializing GPU pool in Redis..."
    python -m gpu_manager init
}

resolve_gunicorn_workers() {
    local workers
    workers="${GUNICORN_WORKERS:-${MAX_CONCURRENT_TASKS:-4}}"

    if ! [[ "$workers" =~ ^[0-9]+$ ]] || [ "$workers" -le 0 ]; then
        workers=1
    fi
    echo "$workers"
}

detect_cpu_cores() {
    local cores=""
    if command -v nproc >/dev/null 2>&1; then
        cores="$(nproc --all 2>/dev/null || nproc 2>/dev/null || true)"
    fi
    if ! [[ "$cores" =~ ^[0-9]+$ ]] || [ "$cores" -le 0 ]; then
        cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || true)"
    fi
    if ! [[ "$cores" =~ ^[0-9]+$ ]] || [ "$cores" -le 0 ]; then
        cores=1
    fi
    echo "$cores"
}

resolve_cpu_worker_concurrency() {
    local cli_concurrency="$1"
    local detected_cores
    detected_cores="$(detect_cpu_cores)"

    local raw_concurrency=""
    if [ -n "$cli_concurrency" ]; then
        raw_concurrency="$cli_concurrency"
    elif [ -n "${CPU_MAX_CONCURRENT_TASKS:-}" ]; then
        raw_concurrency="${CPU_MAX_CONCURRENT_TASKS}"
    elif [ -n "${MMP_CELERY_CONCURRENCY:-}" ]; then
        echo "Warning: MMP_CELERY_CONCURRENCY is deprecated. Please use CPU_MAX_CONCURRENT_TASKS." >&2
        raw_concurrency="${MMP_CELERY_CONCURRENCY}"
    else
        raw_concurrency="0"
    fi

    if ! [[ "$raw_concurrency" =~ ^[0-9]+$ ]]; then
        echo "Warning: Invalid CPU concurrency '${raw_concurrency}', fallback to auto (all CPU cores)." >&2
        raw_concurrency="0"
    fi

    local concurrency
    if [ "$raw_concurrency" -le 0 ]; then
        concurrency="$detected_cores"
    else
        concurrency="$raw_concurrency"
    fi

    if [ "$concurrency" -gt "$detected_cores" ]; then
        echo "Warning: Requested CPU concurrency ${concurrency} exceeds available cores ${detected_cores}; capping to ${detected_cores}." >&2
        concurrency="$detected_cores"
    fi
    if [ "$concurrency" -le 0 ]; then
        concurrency=1
    fi

    echo "$concurrency"
}

# Function to start the Flask API server
start_flask() {
    echo "Starting Flask API server with Gunicorn..."
    local workers
    workers="$(resolve_gunicorn_workers)"
    echo "Using ${workers} Gunicorn worker(s)."
    # The default Gunicorn timeout is 30s, which might be too short for some requests.
    gunicorn --workers "${workers}" --bind 0.0.0.0:5000 --timeout 120 "api_server:app"
}

# Function to start GPU-bound Celery worker
start_celery_gpu() {
    echo "Starting GPU Celery worker..."
    echo "Worker queues: 'high_priority,default' (GPU-bound prediction/scoring tasks)."

    # Determine concurrency based on the actual initialized pool size for clarity.
    CONCURRENCY=$(python -c "from gpu_manager import get_gpu_status; print(get_gpu_status()['available_count'])")
    
    if [ -z "$CONCURRENCY" ] || [ "$CONCURRENCY" -eq 0 ]; then
        echo "Warning: GPU pool is empty or not initialized. Starting Celery worker with concurrency 1 for CPU tasks."
        CONCURRENCY=1
    else
        echo "Found ${CONCURRENCY} GPUs in the pool. Starting worker with this concurrency."
    fi

    # The --max-tasks-per-child argument helps prevent long-lived memory growth.
    celery -A celery_app worker -n "gpu@%h" -l info --concurrency=${CONCURRENCY} -Q high_priority,default --max-tasks-per-child 1
}

# Function to start CPU-only worker
start_celery_cpu() {
    echo "Starting dedicated CPU Celery worker..."
    echo "Worker queue: '${CPU_QUEUE:-cpu_queue}' (CPU-only tasks like MMP queries)."

    local cli_concurrency="$1"
    local detected_cores
    detected_cores="$(detect_cpu_cores)"
    local concurrency
    concurrency="$(resolve_cpu_worker_concurrency "$cli_concurrency")"

    echo "Detected CPU cores: ${detected_cores}"
    echo "CPU worker concurrency: ${concurrency} (1 task per process, no GPU)"
    # Explicitly hide GPUs for this process to avoid any accidental GPU usage.
    CUDA_VISIBLE_DEVICES="" celery -A celery_app worker -l info \
      -n "cpu@%h" \
      --pool=prefork \
      --concurrency="${concurrency}" \
      -Q "${CPU_QUEUE:-cpu_queue}" \
      --prefetch-multiplier=1 \
      --max-tasks-per-child 20
}

restart_services() {
    local mode="${1:-all}"
    echo "Restarting services in '${mode}' mode..."
    bash "$0" stop || true
    sleep 1
    case "$mode" in
        all)
            bash "$0" all
            ;;
        dev)
            bash "$0" dev
            ;;
        *)
            echo "Error: restart mode must be 'all' or 'dev'."
            exit 1
            ;;
    esac
}

# Function to start the Streamlit frontend
start_frontend() {
    echo "Starting Streamlit frontend..."
    local streamlit_addr="0.0.0.0"
    local streamlit_port="8501"
    local host_ip
    host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    cd frontend
    echo "Streamlit bind address: ${streamlit_addr}:${streamlit_port}"
    if [ -n "$host_ip" ]; then
        echo "Access URL: http://${host_ip}:${streamlit_port}"
    fi
    echo "Local URL: http://localhost:${streamlit_port}"
    streamlit run app.py --server.port="${streamlit_port}" --server.address="${streamlit_addr}"
}

# Function to start automatic task monitoring and cleanup
start_monitor() {
    echo "Starting automatic task monitoring and cleanup..."
    
    # 创建监控脚本
    cat > monitor_daemon.py << 'EOF'
#!/usr/bin/env python3
import time
import logging
import requests
import json
from datetime import datetime

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - Monitor - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 监控配置
MONITOR_INTERVAL = 300  # 5分钟检查一次
API_URL = "http://localhost:5000"
API_TOKEN = None

# 尝试读取API密钥
try:
    import config
    if hasattr(config, 'BOLTZ_API_TOKEN'):
        API_TOKEN = config.BOLTZ_API_TOKEN
except ImportError:
    logger.warning("无法导入config模块，将使用无认证模式")

def make_api_request(endpoint, method='GET', data=None):
    """发送API请求"""
    headers = {}
    if API_TOKEN:
        headers['X-API-Token'] = API_TOKEN
    
    url = f"{API_URL}{endpoint}"
    try:
        if method == 'GET':
            response = requests.get(url, headers=headers, timeout=10)
        elif method == 'POST':
            headers['Content-Type'] = 'application/json'
            response = requests.post(url, headers=headers, json=data or {}, timeout=30)
        
        return response.status_code == 200, response.json() if response.content else {}
    except Exception as e:
        logger.error(f"API请求失败 {endpoint}: {e}")
        return False, {}

def monitor_and_clean():
    """监控和清理任务"""
    logger.info("开始监控检查...")
    
    # 检查健康状态
    success, health_data = make_api_request('/monitor/health')
    if not success:
        logger.error("无法连接到API服务器")
        return
    
    if not health_data.get('healthy', False):
        stuck_count = health_data.get('stuck_tasks_count', 0)
        logger.warning(f"检测到系统不健康，有 {stuck_count} 个卡死任务")
        
        # 自动清理
        success, clean_result = make_api_request('/monitor/clean', 'POST', {'force': False})
        if success:
            data = clean_result.get('data', {})
            logger.info(f"自动清理完成: 清理了 {data.get('total_cleaned_gpus', 0)} 个GPU, "
                       f"终止了 {data.get('total_killed_tasks', 0)} 个任务")
        else:
            logger.error("自动清理失败")
    else:
        logger.info("系统状态正常")

def main():
    logger.info("任务监控守护进程启动")
    
    while True:
        try:
            monitor_and_clean()
        except Exception as e:
            logger.exception(f"监控过程中出错: {e}")
        
        logger.debug(f"等待 {MONITOR_INTERVAL} 秒后进行下次检查...")
        time.sleep(MONITOR_INTERVAL)

if __name__ == '__main__':
    main()
EOF

    # 启动监控守护进程
    python monitor_daemon.py &
    MONITOR_PID=$!
    echo "Task monitor started with PID: $MONITOR_PID"
    echo $MONITOR_PID > monitor.pid
}

# --- Command-line Argument Parsing ---
case "$1" in
    init)
        initialize_pool
        ;;
    flask)
        start_flask
        ;;
    celery)
        POOL_SIZE=$(python -c "from gpu_manager import get_gpu_status; print(get_gpu_status()['available_count'])" || echo "0")
        if [ "$POOL_SIZE" -eq 0 ]; then
            echo "Error: The GPU pool is empty. Please run '$0 init' before starting Celery workers."
            exit 1
        fi
        start_celery_gpu
        ;;
    _cpu-worker-internal)
        start_celery_cpu "$2"
        ;;
    frontend)
        start_frontend
        ;;
    monitor)
        start_monitor
        ;;
    all)
        CPU_CONCURRENCY_ARG="$2"
        echo "Starting all services in the background..."
        echo "Use 'bash run.sh stop' to stop all services."
        
        # 1. Initialize the pool
        initialize_pool
        
        # 2. Start Flask in the background
        echo "Starting Flask API server in background..."
        WORKERS="$(resolve_gunicorn_workers)"
        echo "Using ${WORKERS} Gunicorn worker(s) for background API server."
        nohup gunicorn --workers "${WORKERS}" --bind 0.0.0.0:5000 --timeout 120 "api_server:app" > flask.log 2>&1 &
        
        # 3. Start GPU Celery in the background
        echo "Starting GPU Celery worker in background..."
        : > celery.log
        nohup bash "$0" celery >> celery.log 2>&1 &

        # 4. Start CPU Celery in the background
        CPU_CORES="$(detect_cpu_cores)"
        CPU_CONCURRENCY="$(resolve_cpu_worker_concurrency "${CPU_CONCURRENCY_ARG}")"
        echo "Starting CPU Celery worker in background..."
        echo "Detected ${CPU_CORES} CPU cores; CPU worker concurrency=${CPU_CONCURRENCY}."
        nohup bash "$0" _cpu-worker-internal "${CPU_CONCURRENCY}" >> celery.log 2>&1 &

        # 5. Start monitoring daemon in the background
        echo "Starting task monitor in background..."
        nohup bash "$0" monitor > monitor.log 2>&1 &

        # 6. Start frontend in the background
        echo "Starting Streamlit frontend in background..."
        STREAMLIT_BIND_ADDRESS="0.0.0.0"
        STREAMLIT_PORT="8501"
        cd frontend
        nohup streamlit run app.py --server.port="${STREAMLIT_PORT}" --server.address="${STREAMLIT_BIND_ADDRESS}" > ../streamlit.log 2>&1 &
        cd ..

        echo "All services started. Check flask.log, celery.log, monitor.log, and streamlit.log for output."
        HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
        [ -n "$HOST_IP" ] && echo "Access the web interface at: http://${HOST_IP}:${STREAMLIT_PORT}"
        echo "Access the web interface at: http://localhost:${STREAMLIT_PORT}"
        echo "Run 'tail -f monitor.log' to monitor task cleanup activities."
        ;;
    dev)
        CPU_CONCURRENCY_ARG="$2"
        echo "Starting all services with frontend for development..."
        echo "This will start backend services in background and frontend in foreground."
        
        # 1. Initialize the pool
        initialize_pool
        
        # 2. Start Flask in the background
        echo "Starting Flask API server in background..."
        WORKERS="$(resolve_gunicorn_workers)"
        echo "Using ${WORKERS} Gunicorn worker(s) for background API server."
        nohup gunicorn --workers "${WORKERS}" --bind 0.0.0.0:5000 --timeout 120 "api_server:app" > flask.log 2>&1 &
        
        # 3. Start GPU Celery in the background
        echo "Starting GPU Celery worker in background..."
        : > celery.log
        nohup bash "$0" celery >> celery.log 2>&1 &

        # 4. Start CPU Celery in the background
        CPU_CORES="$(detect_cpu_cores)"
        CPU_CONCURRENCY="$(resolve_cpu_worker_concurrency "${CPU_CONCURRENCY_ARG}")"
        echo "Starting CPU Celery worker in background..."
        echo "Detected ${CPU_CORES} CPU cores; CPU worker concurrency=${CPU_CONCURRENCY}."
        nohup bash "$0" _cpu-worker-internal "${CPU_CONCURRENCY}" >> celery.log 2>&1 &

        # 5. Start monitoring daemon in the background
        echo "Starting task monitor in background..."
        nohup bash "$0" monitor > monitor.log 2>&1 &
        
        # 给后端服务一点启动时间
        echo "Waiting for backend services to start..."
        sleep 3
        
        # 5. Start frontend in foreground
        STREAMLIT_PORT="8501"
        HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
        echo "Starting Streamlit frontend..."
        [ -n "$HOST_IP" ] && echo "Access the web interface at: http://${HOST_IP}:${STREAMLIT_PORT}"
        echo "Access the web interface at: http://localhost:${STREAMLIT_PORT}"
        start_frontend
        ;;
    status)
        echo "Checking system status..."
        
        # 检查进程状态
        echo "=== Process Status ==="
        if pgrep -f "gunicorn.*api_server" > /dev/null; then
            echo "✅ Flask API server is running"
        else
            echo "❌ Flask API server is not running"
        fi
        
        if pgrep -f "celery.*worker.*-Q high_priority,default" > /dev/null; then
            echo "✅ GPU Celery worker is running"
        else
            echo "❌ GPU Celery worker is not running"
        fi

        if pgrep -f "celery.*worker.*-Q ${CPU_QUEUE:-cpu_queue}" > /dev/null; then
            echo "✅ CPU Celery worker is running"
        else
            echo "❌ CPU Celery worker is not running"
        fi
        
        if pgrep -f "monitor_daemon" > /dev/null; then
            echo "✅ Task monitor is running"
        else
            echo "❌ Task monitor is not running"
        fi
        
        if pgrep -f "streamlit.*app.py" > /dev/null; then
            echo "✅ Streamlit frontend is running"
        else
            echo "❌ Streamlit frontend is not running"
        fi
        
        echo ""
        echo "=== GPU and Task Status ==="
        CPU_CORES="$(detect_cpu_cores)"
        CPU_CONCURRENCY="$(resolve_cpu_worker_concurrency "")"
        echo "CPU cores detected: ${CPU_CORES}"
        echo "CPU worker target concurrency: ${CPU_CONCURRENCY}"
        if command -v curl > /dev/null; then
            curl -s http://localhost:5000/monitor/health 2>/dev/null | python -m json.tool 2>/dev/null || echo "API server not responding"
        else
            python -c "
import requests
try:
    resp = requests.get('http://localhost:5000/monitor/health', timeout=5)
    import json
    print(json.dumps(resp.json(), indent=2))
except:
    print('API server not responding')
"
        fi
        ;;
    clean)
        echo "Cleaning stuck tasks via API..."
        python -c "
import requests
try:
    # 尝试读取API密钥
    try:
        import config
        headers = {'X-API-Token': config.BOLTZ_API_TOKEN} if hasattr(config, 'BOLTZ_API_TOKEN') else {}
    except:
        headers = {}
    
    headers['Content-Type'] = 'application/json'
    resp = requests.post('http://localhost:5000/monitor/clean', headers=headers, json={'force': False}, timeout=30)
    import json
    print('清理结果:')
    print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
except Exception as e:
    print(f'清理失败: {e}')
"
        ;;
    stop)
        echo "Stopping all services..."
        
        # 停止监控守护进程
        if [ -f "monitor.pid" ]; then
            MONITOR_PID=$(cat monitor.pid)
            if kill -0 $MONITOR_PID 2>/dev/null; then
                kill $MONITOR_PID
                echo "Stopped task monitor (PID: $MONITOR_PID)"
            fi
            rm -f monitor.pid
        fi
        
        # 停止其他进程
        pkill -f "gunicorn.*api_server" && echo "Stopped Flask API server" || echo "Flask API server was not running"
        pkill -f "celery.*worker" && echo "Stopped Celery worker" || echo "Celery worker was not running"
        pkill -f "monitor_daemon" && echo "Stopped task monitor" || echo "Task monitor was not running"
        pkill -f "streamlit.*app.py" && echo "Stopped Streamlit frontend" || echo "Streamlit frontend was not running"
        
        # 清理临时文件
        rm -f monitor_daemon.py
        ;;
    restart)
        restart_services "$2"
        ;;
    *)
        echo "Usage: $0 {init|celery|flask|frontend|monitor|all [CPU_N]|dev [CPU_N]|restart [all|dev]|status|clean|stop}"
        echo "  init     - Initializes the Redis GPU pool. Run this once before starting workers."
        echo "  celery   - Starts GPU Celery worker for prediction/scoring. Requires 'init'."
        echo "  flask    - Starts the Flask API server."
        echo "  frontend - Starts the Streamlit web interface."
        echo "  monitor  - Starts the automatic task monitoring daemon."
        echo "  all [CPU_N] - Starts all services (API + GPU worker + CPU worker + monitor + frontend) in background."
        echo "                CPU_N not provided (or 0) => auto use all CPU cores."
        echo "  dev [CPU_N] - Starts backend services in background and frontend in foreground (for development)."
        echo "                CPU_N not provided (or 0) => auto use all CPU cores."
        echo "  restart [all|dev] - Stops then starts services again. Default mode: all."
        echo "  status   - Shows the status of all services and system health."
        echo "  clean    - Manually trigger task cleanup via API."
        echo "  stop     - Stops all running services."
        exit 1
esac
