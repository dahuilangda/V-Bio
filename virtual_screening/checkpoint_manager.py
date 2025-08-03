# /Boltz-WebUI/virtual_screening/checkpoint_manager.py

"""
checkpoint_manager.py

该模块提供虚拟筛选的断点续算功能：
1. CheckpointManager: 断点管理器
2. ScreeningCheckpoint: 筛选检查点数据结构  
3. 进度保存和恢复逻辑
"""

import os
import json
import time
import pickle
import logging
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, asdict, field
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class ScreeningCheckpoint:
    """筛选检查点数据"""
    
    # 基本信息
    session_id: str
    start_time: float
    last_update: float
    
    # 配置信息
    config_hash: str  # 配置文件的哈希值，用于验证配置一致性
    total_molecules: int
    
    # 进度信息
    submitted_tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # task_id -> {molecule_idx, mol_name, status, submit_time}
    completed_tasks: List[str] = field(default_factory=list)  # 已完成的task_id列表
    failed_tasks: List[str] = field(default_factory=list)  # 失败的task_id列表
    processed_molecules: List[str] = field(default_factory=list)  # 已处理的分子ID列表
    
    # 结果信息
    results_count: int = 0
    best_score: float = 0.0
    
    # 文件路径
    config_files: List[str] = field(default_factory=list)  # 分子配置文件路径列表
    output_dir: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ScreeningCheckpoint':
        """从字典创建检查点"""
        return cls(**data)

class CheckpointManager:
    """断点续算管理器"""
    
    def __init__(self, output_dir: str, session_id: str = None):
        self.output_dir = output_dir
        self.session_id = session_id or f"screening_{int(time.time())}"
        
        # 检查点文件路径
        self.checkpoint_dir = os.path.join(output_dir, "checkpoints")
        os.makedirs(self.checkpoint_dir, exist_ok=True)
        
        self.checkpoint_file = os.path.join(self.checkpoint_dir, f"{self.session_id}.json")
        self.backup_file = os.path.join(self.checkpoint_dir, f"{self.session_id}.backup")
        
        # 当前检查点
        self.current_checkpoint: Optional[ScreeningCheckpoint] = None
        
        logger.info(f"检查点管理器初始化: {self.checkpoint_file}")
    
    def create_checkpoint(self, config_hash: str, total_molecules: int, 
                         config_files: List[str], output_dir: str) -> ScreeningCheckpoint:
        """创建新的检查点"""
        
        checkpoint = ScreeningCheckpoint(
            session_id=self.session_id,
            start_time=time.time(),
            last_update=time.time(),
            config_hash=config_hash,
            total_molecules=total_molecules,
            config_files=config_files,
            output_dir=output_dir
        )
        
        self.current_checkpoint = checkpoint
        self.save_checkpoint()
        
        logger.info(f"创建新检查点: {total_molecules} 个分子")
        return checkpoint
    
    def load_checkpoint(self) -> Optional[ScreeningCheckpoint]:
        """加载检查点"""
        try:
            if os.path.exists(self.checkpoint_file):
                with open(self.checkpoint_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                checkpoint = ScreeningCheckpoint.from_dict(data)
                self.current_checkpoint = checkpoint
                
                elapsed = time.time() - checkpoint.start_time
                logger.info(f"加载检查点成功: {checkpoint.session_id}, "
                          f"已运行 {elapsed/3600:.1f} 小时, "
                          f"进度 {len(checkpoint.completed_tasks)}/{checkpoint.total_molecules}")
                
                return checkpoint
            
        except Exception as e:
            logger.error(f"加载检查点失败: {e}")
            
            # 尝试加载备份文件
            try:
                if os.path.exists(self.backup_file):
                    logger.info("尝试从备份文件恢复...")
                    with open(self.backup_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    
                    checkpoint = ScreeningCheckpoint.from_dict(data)
                    self.current_checkpoint = checkpoint
                    logger.info("从备份文件恢复检查点成功")
                    return checkpoint
                    
            except Exception as backup_e:
                logger.error(f"从备份文件恢复也失败: {backup_e}")
        
        return None
    
    def save_checkpoint(self):
        """保存检查点"""
        if not self.current_checkpoint:
            return
        
        try:
            # 更新时间戳
            self.current_checkpoint.last_update = time.time()
            
            # 先备份当前文件
            if os.path.exists(self.checkpoint_file):
                import shutil
                shutil.copy2(self.checkpoint_file, self.backup_file)
            
            # 保存新检查点
            with open(self.checkpoint_file, 'w', encoding='utf-8') as f:
                json.dump(self.current_checkpoint.to_dict(), f, indent=2, ensure_ascii=False)
            
            logger.debug(f"检查点已保存: {len(self.current_checkpoint.completed_tasks)}/{self.current_checkpoint.total_molecules}")
            
        except Exception as e:
            logger.error(f"保存检查点失败: {e}")
    
    def update_task_submitted(self, task_id: str, molecule_idx: int, mol_name: str):
        """更新任务提交状态"""
        if not self.current_checkpoint:
            return
        
        self.current_checkpoint.submitted_tasks[task_id] = {
            'molecule_idx': molecule_idx,
            'mol_name': mol_name,
            'status': 'submitted',
            'submit_time': time.time()
        }
        
        self.save_checkpoint()
        logger.debug(f"任务 {task_id} ({mol_name}) 已提交")
    
    def update_task_completed(self, task_id: str, success: bool = True):
        """更新任务完成状态"""
        if not self.current_checkpoint:
            return
        
        if task_id in self.current_checkpoint.submitted_tasks:
            if success:
                self.current_checkpoint.completed_tasks.append(task_id)
                self.current_checkpoint.submitted_tasks[task_id]['status'] = 'completed'
                logger.debug(f"任务 {task_id} 完成")
            else:
                self.current_checkpoint.failed_tasks.append(task_id)
                self.current_checkpoint.submitted_tasks[task_id]['status'] = 'failed'
                logger.debug(f"任务 {task_id} 失败")
        
        self.save_checkpoint()
    
    def update_molecule_processed(self, molecule_id: str, result_score: float = 0.0):
        """更新分子处理状态"""
        if not self.current_checkpoint:
            return
        
        if molecule_id not in self.current_checkpoint.processed_molecules:
            self.current_checkpoint.processed_molecules.append(molecule_id)
            self.current_checkpoint.results_count += 1
            
            if result_score > self.current_checkpoint.best_score:
                self.current_checkpoint.best_score = result_score
        
        self.save_checkpoint()
        logger.debug(f"分子 {molecule_id} 处理完成, 得分: {result_score:.4f}")
    
    def is_molecule_processed(self, molecule_id: str) -> bool:
        """检查分子是否已经被处理过"""
        if not self.current_checkpoint:
            return False
        return molecule_id in self.current_checkpoint.processed_molecules
    
    def get_processed_molecules(self) -> List[str]:
        """获取已处理的分子ID列表"""
        if not self.current_checkpoint:
            return []
        return self.current_checkpoint.processed_molecules.copy()
    
    def get_resume_info(self) -> Tuple[List[str], List[str], List[str]]:
        """获取恢复信息
        
        Returns:
            (pending_config_files, completed_task_ids, failed_task_ids)
        """
        if not self.current_checkpoint:
            return [], [], []
        
        # 找出还需要处理的配置文件
        pending_configs = []
        completed_configs = set()
        
        # 找出已完成的配置文件索引
        for task_id in self.current_checkpoint.completed_tasks:
            if task_id in self.current_checkpoint.submitted_tasks:
                mol_idx = self.current_checkpoint.submitted_tasks[task_id]['molecule_idx']
                completed_configs.add(mol_idx)
        
        # 生成待处理配置文件列表
        for i, config_file in enumerate(self.current_checkpoint.config_files):
            if i not in completed_configs:
                pending_configs.append(config_file)
        
        logger.info(f"恢复信息: 待处理 {len(pending_configs)} 个分子, "
                   f"已完成 {len(self.current_checkpoint.completed_tasks)} 个, "
                   f"失败 {len(self.current_checkpoint.failed_tasks)} 个")
        
        return (pending_configs, 
                self.current_checkpoint.completed_tasks, 
                self.current_checkpoint.failed_tasks)
    
    def get_progress_summary(self) -> Dict[str, Any]:
        """获取进度摘要"""
        if not self.current_checkpoint:
            return {}
        
        elapsed_time = time.time() - self.current_checkpoint.start_time
        completed = len(self.current_checkpoint.completed_tasks)
        total = self.current_checkpoint.total_molecules
        
        progress_percent = (completed / total * 100) if total > 0 else 0
        
        # 估算剩余时间
        if completed > 0:
            avg_time_per_mol = elapsed_time / completed
            remaining_molecules = total - completed
            estimated_remaining_time = avg_time_per_mol * remaining_molecules
        else:
            estimated_remaining_time = 0
        
        return {
            'session_id': self.current_checkpoint.session_id,
            'start_time': self.current_checkpoint.start_time,
            'elapsed_time': elapsed_time,
            'completed_molecules': completed,
            'total_molecules': total,
            'failed_molecules': len(self.current_checkpoint.failed_tasks),
            'progress_percent': progress_percent,
            'estimated_remaining_time': estimated_remaining_time,
            'best_score': self.current_checkpoint.best_score,
            'results_count': self.current_checkpoint.results_count
        }
    
    def cleanup_old_checkpoints(self, max_age_days: int = 7):
        """清理旧的检查点文件"""
        try:
            current_time = time.time()
            max_age_seconds = max_age_days * 24 * 3600
            
            for filename in os.listdir(self.checkpoint_dir):
                if filename.endswith('.json') or filename.endswith('.backup'):
                    filepath = os.path.join(self.checkpoint_dir, filename)
                    
                    # 检查文件年龄
                    file_age = current_time - os.path.getmtime(filepath)
                    
                    if file_age > max_age_seconds:
                        os.remove(filepath)
                        logger.info(f"清理旧检查点文件: {filename}")
                        
        except Exception as e:
            logger.warning(f"清理旧检查点文件失败: {e}")
    
    def validate_config_consistency(self, config_hash: str) -> bool:
        """验证配置一致性"""
        if not self.current_checkpoint:
            return True
        
        return self.current_checkpoint.config_hash == config_hash
    
    def calculate_config_hash(self, config_dict: Dict[str, Any]) -> str:
        """计算配置哈希值"""
        import hashlib
        import json
        
        # 将配置转换为排序的JSON字符串
        config_str = json.dumps(config_dict, sort_keys=True)
        return hashlib.md5(config_str.encode()).hexdigest()
