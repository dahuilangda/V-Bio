# /Boltz-WebUI/virtual_screening/api_client.py

import requests
import time
import os
import zipfile
from typing import Optional, Dict, List
import logging

logger = logging.getLogger(__name__)

class BoltzApiClient:
    """
    与 Boltz-WebUI 预测 API 交互的客户端，专门为虚拟筛选优化。
    """
    def __init__(self, server_url: str, api_token: str):
        if server_url.endswith('/'):
            server_url = server_url[:-1]
        self.server_url = server_url
        self.headers = {"X-API-Token": api_token}
        logger.info(f"虚拟筛选 API 客户端已初始化，服务器: {self.server_url}")

    def submit_screening_job(self, yaml_path: str, job_name: str = None, 
                           use_msa_server: bool = False) -> Optional[str]:
        """提交虚拟筛选任务
        
        Args:
            yaml_path: YAML配置文件路径
            job_name: 任务名称，用于识别筛选任务
            use_msa_server: 当序列找不到MSA缓存时是否使用MSA服务器
        
        Returns:
            任务ID，如果提交失败则返回None
        """
        predict_url = f"{self.server_url}/predict"
        try:
            with open(yaml_path, 'rb') as f:
                files = {'yaml_file': (os.path.basename(yaml_path), f)}
                data = {
                    'use_msa_server': str(use_msa_server).lower(),
                    'job_type': 'virtual_screening'
                }
                if job_name:
                    data['job_name'] = job_name
                    
                response = requests.post(predict_url, headers=self.headers, 
                                       files=files, data=data, timeout=30)

            if response.status_code == 202:
                task_id = response.json().get('task_id')
                logger.info(f"虚拟筛选任务提交成功。任务ID: {task_id}")
                if job_name:
                    logger.info(f"任务名称: {job_name}")
                if use_msa_server:
                    logger.info(f"MSA服务器已启用: 将为无缓存序列生成MSA")
                return task_id
            else:
                logger.error(f"任务提交失败: {response.status_code} - {response.text}")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"任务提交过程中发生错误: {e}")
            return None

    def poll_status(self, task_id: str, poll_interval: int = 30, max_wait_time: int = 3600) -> Optional[Dict]:
        """轮询任务状态直到完成或失败
        
        Args:
            task_id: 任务ID
            poll_interval: 轮询间隔（秒）
            max_wait_time: 最大等待时间（秒），默认1小时
        
        Returns:
            任务状态信息字典，如果轮询失败则返回None
        """
        status_url = f"{self.server_url}/status/{task_id}"
        start_time = time.time()
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while True:
            elapsed_time = time.time() - start_time
            
            # 检查超时
            if elapsed_time > max_wait_time:
                logger.error(f"任务 {task_id} 等待超时 ({max_wait_time} 秒)")
                return {"status": "timeout", "task_id": task_id}
            
            try:
                response = requests.get(status_url, headers=self.headers, timeout=30)
                consecutive_errors = 0  # 重置错误计数
                
                if response.status_code == 200:
                    status_data = response.json()
                    # 检查Celery任务状态格式
                    state = status_data.get('state', '')
                    status = status_data.get('status', '')
                    
                    # 统一状态格式并大写显示
                    if state == 'SUCCESS':
                        final_status = 'completed'
                        display_status = 'COMPLETED'
                    elif state == 'FAILURE':
                        final_status = 'failed'
                        display_status = 'FAILED'
                    elif state == 'REVOKED':
                        final_status = 'cancelled'
                        display_status = 'CANCELLED'
                    elif state == 'PENDING':
                        final_status = 'pending'
                        display_status = 'PENDING'
                    elif state == 'PROGRESS':
                        final_status = 'progress'
                        display_status = 'PROGRESS'
                    elif status in ['completed', 'failed', 'cancelled']:
                        final_status = status
                        display_status = status.upper()
                    else:
                        final_status = state.lower() if state else (status.lower() if status else 'unknown')
                        display_status = final_status.upper()
                    
                    logger.info(f"任务 {task_id} 状态: {display_status} (已等待 {elapsed_time:.0f}s)")
                    
                    if final_status in ['completed', 'failed', 'cancelled']:
                        logger.info(f"任务 {task_id} 最终状态: {display_status}")
                        # 将统一后的状态添加到返回数据中
                        status_data['status'] = final_status
                        return status_data
                    else:
                        logger.debug(f"任务 {task_id} 状态: {display_status}，继续等待...")
                        time.sleep(poll_interval)
                else:
                    logger.error(f"状态查询失败: {response.status_code} - {response.text}")
                    consecutive_errors += 1
                    if consecutive_errors >= max_consecutive_errors:
                        logger.error(f"连续状态查询失败次数过多，放弃任务 {task_id}")
                        return {"status": "query_failed", "task_id": task_id}
                    time.sleep(poll_interval)
                    
            except requests.exceptions.RequestException as e:
                consecutive_errors += 1
                logger.error(f"状态查询过程中发生错误 ({consecutive_errors}/{max_consecutive_errors}): {e}")
                
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(f"连续网络错误次数过多，放弃任务 {task_id}")
                    return {"status": "network_error", "task_id": task_id}
                
                time.sleep(poll_interval)

    def download_results(self, task_id: str, output_dir: str) -> bool:
        """下载任务结果
        
        Args:
            task_id: 任务ID
            output_dir: 输出目录
        
        Returns:
            下载是否成功
        """
        download_url = f"{self.server_url}/results/{task_id}"
        try:
            response = requests.get(download_url, headers=self.headers, 
                                  stream=True, timeout=120)
            if response.status_code == 200:
                zip_path = os.path.join(output_dir, f"{task_id}_results.zip")
                os.makedirs(output_dir, exist_ok=True)
                
                with open(zip_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                
                # 解压结果文件
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(output_dir)
                
                # 删除压缩文件
                os.remove(zip_path)
                logger.info(f"结果下载完成，保存到: {output_dir}")
                return True
            else:
                logger.error(f"结果下载失败: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.error(f"结果下载过程中发生错误: {e}")
            return False

    def get_server_status(self) -> Dict:
        """获取服务器状态信息"""
        try:
            response = requests.get(f"{self.server_url}/monitor/health", 
                                  headers=self.headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                return {"status": "error", "message": f"HTTP {response.status_code}"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def cancel_job(self, task_id: str) -> bool:
        """取消正在运行的任务
        
        Args:
            task_id: 任务ID
        
        Returns:
            取消是否成功
        """
        try:
            response = requests.post(f"{self.server_url}/cancel/{task_id}", 
                                   headers=self.headers, timeout=30)
            if response.status_code == 200:
                logger.info(f"任务 {task_id} 已成功取消")
                return True
            else:
                logger.error(f"任务取消失败: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logger.error(f"任务取消过程中发生错误: {e}")
            return False

    def batch_submit_jobs(self, yaml_paths: List[str], job_prefix: str = "screening", 
                         use_msa_server: bool = False) -> List[str]:
        """批量提交筛选任务
        
        Args:
            yaml_paths: YAML文件路径列表
            job_prefix: 任务名称前缀
            use_msa_server: 是否使用MSA服务器
        
        Returns:
            成功提交的任务ID列表
        """
        task_ids = []
        for i, yaml_path in enumerate(yaml_paths):
            job_name = f"{job_prefix}_{i+1:04d}"
            task_id = self.submit_screening_job(
                yaml_path=yaml_path,
                job_name=job_name,
                use_msa_server=use_msa_server
            )
            if task_id:
                task_ids.append(task_id)
                logger.info(f"批量任务 {i+1}/{len(yaml_paths)} 提交成功: {task_id}")
            else:
                logger.error(f"批量任务 {i+1}/{len(yaml_paths)} 提交失败")
            
            # 添加短暂延迟，避免服务器过载
            time.sleep(1)
        
        logger.info(f"批量提交完成，成功提交 {len(task_ids)}/{len(yaml_paths)} 个任务")
        return task_ids

    def batch_poll_status(self, task_ids: List[str], poll_interval: int = 30) -> Dict[str, Dict]:
        """批量轮询多个任务状态
        
        Args:
            task_ids: 任务ID列表
            poll_interval: 轮询间隔（秒）
        
        Returns:
            任务状态字典，键为任务ID，值为状态信息
        """
        results = {}
        pending_tasks = set(task_ids)
        
        while pending_tasks:
            completed_tasks = set()
            
            for task_id in pending_tasks:
                try:
                    response = requests.get(f"{self.server_url}/status/{task_id}", 
                                          headers=self.headers, timeout=30)
                    if response.status_code == 200:
                        status_data = response.json()
                        # 检查Celery任务状态格式
                        state = status_data.get('state', '')
                        status = status_data.get('status', 'unknown')
                        
                        # 统一状态格式
                        if state == 'SUCCESS':
                            final_status = 'completed'
                        elif state == 'FAILURE':
                            final_status = 'failed'
                        elif state == 'REVOKED':
                            final_status = 'cancelled'
                        elif status in ['completed', 'failed', 'cancelled']:
                            final_status = status
                        else:
                            final_status = state.lower() if state else status
                        
                        if final_status in ['completed', 'failed', 'cancelled']:
                            status_data['status'] = final_status  # 统一状态格式
                            results[task_id] = status_data
                            completed_tasks.add(task_id)
                            logger.info(f"任务 {task_id} 完成，状态: {final_status}")
                        else:
                            logger.debug(f"任务 {task_id} 状态: {final_status}")
                except Exception as e:
                    logger.error(f"查询任务 {task_id} 状态时发生错误: {e}")
            
            # 移除已完成的任务
            pending_tasks -= completed_tasks
            
            if pending_tasks:
                logger.info(f"还有 {len(pending_tasks)} 个任务未完成，等待 {poll_interval} 秒后继续轮询...")
                time.sleep(poll_interval)
        
        logger.info(f"所有 {len(task_ids)} 个任务已完成")
        return results
