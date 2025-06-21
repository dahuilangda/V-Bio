# gpu_manager.py
import redis
import config
import time

redis_client = None

def get_redis_client():
    """
    Creates and returns a new Redis client instance.
    This ensures each process gets a fresh connection.
    """
    global redis_client
    if redis_client is None:
        redis_client = redis.Redis.from_url(config.REDIS_URL, decode_responses=True)
    return redis_client

def initialize_gpu_pool():
    """
    Initializes or resets the GPU pool in Redis.
    """
    client = get_redis_client()
    print("--- GPU Pool Initialization ---")
    pool_key = config.GPU_POOL_KEY
    devices_to_use = config.DEVICES_TO_USE
    
    print(f"Target devices for pool '{pool_key}': {devices_to_use}")
    print(f"Flushing existing pool by deleting key: '{pool_key}'...")
    client.delete(pool_key)
    time.sleep(0.1)

    if devices_to_use:
        print(f"Pushing {len(devices_to_use)} GPU IDs to the pool...")
        client.rpush(pool_key, *devices_to_use)
    
    time.sleep(0.1)
    current_pool_size = client.llen(pool_key)
    print(f"Verification: Found {current_pool_size} items in the pool.")

    if current_pool_size == len(devices_to_use):
        print(f"✅ GPU pool '{pool_key}' is ready and verified.")
    else:
        print(f"⚠️ WARNING: GPU pool initialization failed. Expected {len(devices_to_use)} GPUs, but found {current_pool_size}.")
    print("-----------------------------")


def acquire_gpu(timeout: int = 60) -> int:
    """
    Blocks until a GPU is available, then returns its ID.
    Includes enhanced logging for debugging connection issues.
    """
    client = get_redis_client()
    pool_key = config.GPU_POOL_KEY
    
    print("\n--- Acquiring GPU: Pre-check ---")
    try:
        client.ping()
        print("[DEBUG] Redis connection is alive.")
        
        current_pool_size = client.llen(pool_key)
        print(f"[DEBUG] Pool '{pool_key}' size reported by this worker: {current_pool_size}")

        if current_pool_size > 0:
            current_gpus = client.lrange(pool_key, 0, -1)
            print(f"[DEBUG] Worker sees available GPU IDs: {current_gpus}")
        else:
            print("[DEBUG] Worker sees an empty pool. Will now wait.")
            
    except Exception as e:
        print(f"[DEBUG] ERROR during pre-check: {e}")
        raise
    
    print("--- Now attempting to block and pop from the list... ---")
    result = client.blpop(pool_key, timeout=timeout)
    
    if result is None:
        raise TimeoutError(f"Could not acquire a GPU within the {timeout}s timeout. The GPU pool might be empty or all GPUs are in use by other tasks.")
        
    _, gpu_id = result
    print(f"✅ Acquired GPU {gpu_id}.")
    return int(gpu_id)

def release_gpu(gpu_id: int):
    """
    Returns a GPU ID back to the pool.
    """
    client = get_redis_client()
    client.rpush(config.GPU_POOL_KEY, gpu_id)
    print(f"Released GPU {gpu_id} back to the pool.")
