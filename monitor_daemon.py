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
