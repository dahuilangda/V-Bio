#!/bin/bash

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    echo "Activating Python virtual environment..."
    source venv/bin/activate
fi

# Function to initialize the GPU pool in Redis
initialize_pool() {
    echo "Initializing GPU pool in Redis..."
    python -c "from gpu_manager import initialize_gpu_pool; initialize_gpu_pool()"
}

# Function to start the Flask API server
start_flask() {
    echo "Starting Flask API server with Gunicorn..."
    gunicorn --workers 4 --bind 0.0.0.0:5000 "api_server:app"
}

# Function to start the Celery workers
start_celery() {
    echo "Starting Celery workers..."
    echo "Workers will prioritize tasks from 'high_priority' queue over 'default' queue."

    CONCURRENCY=$(python -c "import config; print(config.MAX_CONCURRENT_TASKS)")

    celery -A celery_app worker -l info --concurrency=${CONCURRENCY} -Q high_priority,default --max-tasks-per-child 1
}


case "$1" in
    init)
        initialize_pool
        ;;
    flask)
        start_flask
        ;;
    celery)
        start_celery
        ;;
    all)
        echo "This command is not recommended for production."
        echo "Please run 'init', 'celery', and 'flask' in separate terminals."
        ;;
    *)
        echo "Usage: $0 {init|celery|flask}"
        echo "  init    - Initializes the Redis GPU pool. Run this once before starting workers."
        echo "  celery  - Starts the Celery workers in a new terminal."
        echo "  flask   - Starts the Flask API server in another terminal."
        exit 1
esac
