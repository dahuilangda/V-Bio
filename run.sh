#!/bin/bash

# Exit immediately if a command exits with a non-zero status.
set -e

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

# Function to start the Flask API server
start_flask() {
    echo "Starting Flask API server with Gunicorn..."
    # The default is 30s, which might be too short for some requests.
    gunicorn --workers 4 --bind 0.0.0.0:5000 --timeout 120 "api_server:app"
}

# Function to start the Celery workers
start_celery() {
    echo "Starting Celery workers..."
    echo "Workers will prioritize tasks from 'high_priority' queue over 'default' queue."

    # Determine concurrency based on the actual initialized pool size for clarity.
    CONCURRENCY=$(python -c "from gpu_manager import get_gpu_status; print(get_gpu_status()['available_count'])")
    
    if [ -z "$CONCURRENCY" ] || [ "$CONCURRENCY" -eq 0 ]; then
        echo "Warning: GPU pool is empty or not initialized. Starting Celery worker with concurrency 1 for CPU tasks."
        CONCURRENCY=1
    else
        echo "Found ${CONCURRENCY} GPUs in the pool. Starting worker with this concurrency."
    fi

    # The --max-tasks-per-child argument is excellent for preventing memory leaks.
    celery -A celery_app worker -l info --concurrency=${CONCURRENCY} -Q high_priority,default --max-tasks-per-child 1
}

# Function to start the Streamlit frontend
start_frontend() {
    echo "Starting Streamlit frontend..."
    cd frontend
    streamlit run app.py --server.port=8501 --server.address=0.0.0.0
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
        start_celery
        ;;
    frontend)
        start_frontend
        ;;
    monitor)
        start_monitor
        ;;
    all)
        echo "Starting all services in the background for development..."
        echo "Use 'bash run.sh stop' to stop all services."
        
        # 1. Initialize the pool
        initialize_pool
        
        # 2. Start Flask in the background
        echo "Starting Flask API server in background..."
        nohup gunicorn --workers 4 --bind 0.0.0.0:5000 --timeout 120 "api_server:app" > flask.log 2>&1 &
        
        # 3. Start Celery in the background
        echo "Starting Celery worker in background..."
        nohup bash "$0" celery > celery.log 2>&1 &
        
        # 4. Start monitoring daemon in the background
        echo "Starting task monitor in background..."
        nohup bash "$0" monitor > monitor.log 2>&1 &
        
        # 5. Start frontend in the background
        echo "Starting Streamlit frontend in background..."
        cd frontend
        nohup streamlit run app.py --server.port=8501 --server.address=0.0.0.0 > ../streamlit.log 2>&1 &
        cd ..
        
        echo "All services started. Check flask.log, celery.log, monitor.log, and streamlit.log for output."
        echo "Access the web interface at: http://localhost:8501"
        echo "Run 'tail -f monitor.log' to monitor task cleanup activities."
        ;;
    dev)
        echo "Starting all services with frontend for development..."
        echo "This will start backend services in background and frontend in foreground."
        
        # 1. Initialize the pool
        initialize_pool
        
        # 2. Start Flask in the background
        echo "Starting Flask API server in background..."
        nohup gunicorn --workers 4 --bind 0.0.0.0:5000 --timeout 120 "api_server:app" > flask.log 2>&1 &
        
        # 3. Start Celery in the background
        echo "Starting Celery worker in background..."
        nohup bash "$0" celery > celery.log 2>&1 &
        
        # 4. Start monitoring daemon in the background
        echo "Starting task monitor in background..."
        nohup bash "$0" monitor > monitor.log 2>&1 &
        
        # 给后端服务一点启动时间
        echo "Waiting for backend services to start..."
        sleep 3
        
        # 5. Start frontend in foreground
        echo "Starting Streamlit frontend..."
        echo "Access the web interface at: http://localhost:8501"
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
        
        if pgrep -f "celery.*worker" > /dev/null; then
            echo "✅ Celery worker is running"
        else
            echo "❌ Celery worker is not running"
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
    *)
        echo "Usage: $0 {init|celery|flask|frontend|monitor|all|dev|status|clean|stop}"
        echo "  init     - Initializes the Redis GPU pool. Run this once before starting workers."
        echo "  celery   - Starts the Celery workers. Requires 'init' to be run first."
        echo "  flask    - Starts the Flask API server."
        echo "  frontend - Starts the Streamlit web interface."
        echo "  monitor  - Starts the automatic task monitoring daemon."
        echo "  all      - Starts all services in the background (for production)."
        echo "  dev      - Starts backend services in background and frontend in foreground (for development)."
        echo "  status   - Shows the status of all services and system health."
        echo "  clean    - Manually trigger task cleanup via API."
        echo "  stop     - Stops all running services."
        exit 1
esac