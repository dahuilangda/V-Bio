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
    all)
        echo "Starting all services in the background for development..."
        echo "Use 'pkill -f gunicorn' and 'pkill -f celery' to stop them."
        
        # 1. Initialize the pool
        initialize_pool
        
        # 2. Start Flask in the background
        echo "Starting Flask API server in background..."
        nohup gunicorn --workers 4 --bind 0.0.0.0:5000 --timeout 120 "api_server:app" > flask.log 2>&1 &
        
        # 3. Start Celery in the background
        echo "Starting Celery worker in background..."
        nohup bash "$0" celery > celery.log 2>&1 &
        
        echo "All services started. Check flask.log and celery.log for output."
        echo "Run 'tail -f celery.log' or 'tail -f flask.log' to monitor."
        ;;
    *)
        echo "Usage: $0 {init|celery|flask|all}"
        echo "  init    - Initializes the Redis GPU pool. Run this once before starting workers."
        echo "  celery  - Starts the Celery workers. Requires 'init' to be run first."
        echo "  flask   - Starts the Flask API server."
        echo "  all     - Starts all services in the background (for development only)."
        exit 1
esac