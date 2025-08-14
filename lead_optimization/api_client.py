# /data/boltz_webui/lead_optimization/api_client.py

"""
Boltz-WebUI API client for lead optimization
Extends the virtual screening client with optimization-specific features
"""

import requests
import time
import os
import json
import tempfile
import logging
from typing import Optional, Dict, List, Any, Tuple
from pathlib import Path

from exceptions import BoltzAPIError
from config import BoltzAPIConfig

logger = logging.getLogger(__name__)

class BoltzOptimizationClient:
    """
    Enhanced Boltz-WebUI API client for compound optimization
    """
    
    def __init__(self, config: BoltzAPIConfig):
        self.config = config
        self.server_url = config.server_url.rstrip('/')
        self.headers = {"X-API-Token": config.api_token}
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # Don't validate connection during initialization - validate when actually needed
        logger.info(f"优化 API 客户端已初始化，服务器: {self.server_url}")
        logger.info("注意: API连接将在首次使用时验证")
    
    def _validate_connection(self):
        """Validate API connection using actual Boltz-WebUI endpoints"""
        try:
            # Test the /predict endpoint with a HEAD request
            # This is the main endpoint we'll use, so it's the best test
            response = self.session.head(
                f"{self.server_url}/predict", 
                timeout=10
            )
            
            # Accept common responses that indicate server is running
            if response.status_code in [200, 405, 422]:  # 405=Method not allowed, 422=Validation error
                logger.info("API服务器连接验证成功")
                return True
            elif response.status_code == 401:
                raise BoltzAPIError("API令牌无效或缺失")
            elif response.status_code == 404:
                raise BoltzAPIError("Boltz-WebUI API服务器可能未正确启动 - /predict端点不存在")
            else:
                raise BoltzAPIError(f"API服务器响应异常: {response.status_code}")
                
        except requests.exceptions.ConnectionError as e:
            raise BoltzAPIError(f"无法连接到Boltz-WebUI服务器 {self.server_url}: 请确认服务器已启动")
        except requests.exceptions.Timeout as e:
            raise BoltzAPIError(f"连接Boltz-WebUI服务器超时: {e}")
        except requests.exceptions.RequestException as e:
            raise BoltzAPIError(f"连接Boltz-WebUI服务器时发生错误: {e}")
    
    def submit_optimization_job(self, 
                               yaml_content: str,
                               job_name: str,
                               compound_smiles: str,
                               job_type: str = "lead_optimization",
                               use_msa_server: bool = True) -> str:
        """
        Submit optimization job to Boltz-WebUI
        
        Args:
            yaml_content: YAML configuration content
            job_name: Unique job name for tracking
            compound_smiles: SMILES string of the compound
            job_type: Type of optimization job
            use_msa_server: Whether to use MSA server
            
        Returns:
            Task ID string
            
        Raises:
            BoltzAPIError: If submission fails
        """
        # Validate connection on first use
        self._validate_connection()
        
        predict_url = f"{self.server_url}/predict"
        
        # Create temporary YAML file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write(yaml_content)
            yaml_path = f.name
        
        try:
            with open(yaml_path, 'rb') as f:
                files = {'yaml_file': (f'{job_name}.yaml', f)}
                data = {
                    'use_msa_server': str(use_msa_server).lower(),
                    'job_type': job_type,
                    'job_name': job_name,
                    'compound_smiles': compound_smiles,
                    'optimization_context': 'true'
                }
                
                response = self.session.post(
                    predict_url,
                    files=files,
                    data=data,
                    timeout=self.config.timeout
                )
            
            if response.status_code == 202:
                task_id = response.json().get('task_id')
                if not task_id:
                    raise BoltzAPIError("服务器返回了无效的任务ID")
                
                logger.info(f"优化任务提交成功 - ID: {task_id}, 化合物: {compound_smiles[:50]}...")
                return task_id
            else:
                error_msg = f"任务提交失败: {response.status_code} - {response.text}"
                logger.error(error_msg)
                raise BoltzAPIError(error_msg)
                
        except requests.exceptions.RequestException as e:
            raise BoltzAPIError(f"网络请求失败: {e}")
        finally:
            # Clean up temporary file
            if os.path.exists(yaml_path):
                os.unlink(yaml_path)
    
    def poll_job_status(self, task_id: str) -> Dict[str, Any]:
        """
        Poll job status until completion
        
        Args:
            task_id: Task ID to poll
            
        Returns:
            Status information dictionary
            
        Raises:
            BoltzAPIError: If polling fails
        """
        status_url = f"{self.server_url}/status/{task_id}"
        start_time = time.time()
        consecutive_errors = 0
        max_consecutive_errors = 5
        
        while True:
            elapsed_time = time.time() - start_time
            
            # Check timeout
            if elapsed_time > self.config.timeout:
                raise BoltzAPIError(f"任务 {task_id} 等待超时 ({self.config.timeout}秒)")
            
            try:
                response = self.session.get(status_url, timeout=30)
                consecutive_errors = 0  # Reset error count on success
                
                if response.status_code == 200:
                    status_data = response.json()
                    state = status_data.get('state', '').upper()
                    
                    if state == 'SUCCESS':
                        logger.info(f"任务 {task_id} 已完成")
                        return {'status': 'completed', 'data': status_data}
                    elif state in ['FAILURE', 'REVOKED']:
                        error_info = status_data.get('result', {})
                        error_msg = f"任务失败: {error_info}"
                        logger.error(error_msg)
                        return {'status': 'failed', 'error': error_msg, 'data': status_data}
                    elif state in ['PENDING', 'STARTED', 'RUNNING', 'PROGRESS']:
                        # Log progress if available
                        result = status_data.get('result', {})
                        if isinstance(result, dict) and 'progress' in result:
                            progress = result['progress']
                            logger.info(f"任务 {task_id} 进行中: {progress}")
                        
                        # Continue polling
                        time.sleep(self.config.poll_interval)
                        continue
                    else:
                        logger.warning(f"任务 {task_id} 状态未知: {state}")
                        time.sleep(self.config.poll_interval)
                        continue
                        
                elif response.status_code == 404:
                    raise BoltzAPIError(f"任务 {task_id} 未找到")
                else:
                    logger.warning(f"状态查询失败: {response.status_code} - {response.text}")
                    consecutive_errors += 1
                    
            except requests.exceptions.RequestException as e:
                consecutive_errors += 1
                logger.warning(f"状态查询网络错误 (#{consecutive_errors}): {e}")
            
            # Check if too many consecutive errors
            if consecutive_errors >= max_consecutive_errors:
                raise BoltzAPIError(f"连续 {consecutive_errors} 次状态查询失败")
            
            time.sleep(self.config.poll_interval)
    
    def download_results(self, task_id: str, output_dir: str) -> Dict[str, str]:
        """
        Download optimization results
        
        Args:
            task_id: Task ID
            output_dir: Directory to save results
            
        Returns:
            Dictionary mapping file types to local paths
            
        Raises:
            BoltzAPIError: If download fails
        """
        download_url = f"{self.server_url}/results/{task_id}"
        
        # 添加短暂等待，确保结果文件完全准备就绪
        time.sleep(2)
        
        max_retries = 3
        retry_delay = 5
        
        for attempt in range(max_retries):
            try:
                response = self.session.get(download_url, timeout=300)  # 5 min timeout for download
                
                if response.status_code == 200:
                    # Save zip file
                    os.makedirs(output_dir, exist_ok=True)
                    zip_path = os.path.join(output_dir, f"{task_id}_results.zip")
                    
                    with open(zip_path, 'wb') as f:
                        f.write(response.content)
                    
                    # Extract zip file
                    import zipfile
                    extracted_files = {}
                    
                    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                        zip_ref.extractall(output_dir)
                        
                        for file_info in zip_ref.filelist:
                            file_path = os.path.join(output_dir, file_info.filename)
                            if file_info.filename.endswith('.cif'):
                                extracted_files['structure'] = file_path
                            elif file_info.filename.endswith('.json'):
                                extracted_files['metrics'] = file_path
                            elif file_info.filename.endswith('.log'):
                                extracted_files['log'] = file_path
                    
                    logger.info(f"结果下载完成: {output_dir}")
                    return extracted_files
                    
                elif response.status_code == 404:
                    if attempt < max_retries - 1:
                        logger.warning(f"任务 {task_id} 结果暂时不可用，{retry_delay}秒后重试... (尝试 {attempt + 1}/{max_retries})")
                        time.sleep(retry_delay)
                        continue
                    else:
                        raise BoltzAPIError(f"任务 {task_id} 的结果不存在")
                else:
                    if attempt < max_retries - 1:
                        logger.warning(f"结果下载失败 (状态码 {response.status_code})，{retry_delay}秒后重试... (尝试 {attempt + 1}/{max_retries})")
                        time.sleep(retry_delay)
                        continue
                    else:
                        raise BoltzAPIError(f"结果下载失败: {response.status_code} - {response.text}")
                        
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    logger.warning(f"结果下载网络错误，{retry_delay}秒后重试... (尝试 {attempt + 1}/{max_retries}): {e}")
                    time.sleep(retry_delay)
                    continue
                else:
                    raise BoltzAPIError(f"结果下载网络错误: {e}")
        
        # 如果所有重试都失败了
        raise BoltzAPIError(f"任务 {task_id} 结果下载失败，已重试 {max_retries} 次")
    
    def batch_submit_jobs(self, 
                         job_configs: List[Dict[str, Any]],
                         max_concurrent: Optional[int] = None) -> List[Tuple[Dict, str]]:
        """
        Submit multiple optimization jobs with concurrency control
        
        Args:
            job_configs: List of job configuration dictionaries
            max_concurrent: Maximum concurrent submissions
            
        Returns:
            List of tuples (job_config, task_id)
        """
        if max_concurrent is None:
            max_concurrent = self.config.max_concurrent_jobs
        
        submitted_jobs = []
        semaphore = None
        
        if max_concurrent > 1:
            import threading
            semaphore = threading.Semaphore(max_concurrent)
        
        def submit_single_job(job_config):
            if semaphore:
                semaphore.acquire()
            
            try:
                task_id = self.submit_optimization_job(
                    yaml_content=job_config['yaml_content'],
                    job_name=job_config['job_name'],
                    compound_smiles=job_config['compound_smiles'],
                    job_type=job_config.get('job_type', 'lead_optimization'),
                    use_msa_server=job_config.get('use_msa_server', True)
                )
                return (job_config, task_id)
            finally:
                if semaphore:
                    semaphore.release()
        
        # Use thread pool for concurrent submissions
        import concurrent.futures
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            future_to_config = {
                executor.submit(submit_single_job, config): config 
                for config in job_configs
            }
            
            for future in concurrent.futures.as_completed(future_to_config):
                config = future_to_config[future]
                try:
                    result = future.result()
                    submitted_jobs.append(result)
                except Exception as e:
                    logger.error(f"作业提交失败 {config.get('job_name', 'unknown')}: {e}")
                    submitted_jobs.append((config, None))
        
        logger.info(f"批量提交完成: {len([j for j in submitted_jobs if j[1] is not None])}/{len(job_configs)} 成功")
        return submitted_jobs
    
    def get_job_metrics(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        Get job metrics from completed task
        
        Args:
            task_id: Task ID
            
        Returns:
            Metrics dictionary or None if not available
        """
        try:
            # First check if job is completed
            status_response = self.poll_job_status(task_id)
            
            if status_response['status'] != 'completed':
                return None
            
            # Extract metrics from results
            temp_dir = tempfile.mkdtemp()
            try:
                extracted_files = self.download_results(task_id, temp_dir)
                
                if 'metrics' in extracted_files:
                    with open(extracted_files['metrics'], 'r') as f:
                        metrics = json.load(f)
                    return metrics
                
            finally:
                # Clean up temp directory
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
                
        except Exception as e:
            logger.warning(f"获取任务指标失败 {task_id}: {e}")
        
        return None
    
    def health_check(self) -> bool:
        """Check if API server is healthy using Boltz-WebUI endpoints"""
        try:
            # Use the monitor/health endpoint which is public (no auth needed)
            response = self.session.get(f"{self.server_url}/monitor/health", timeout=5)
            if response.status_code == 200:
                return True
                
            # Fallback: try the predict endpoint with HEAD
            response = self.session.head(f"{self.server_url}/predict", timeout=5)
            return response.status_code in [200, 405, 422]  # Server is responding
            
        except:
            return False
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.session.close()
