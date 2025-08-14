#!/usr/bin/env python3
"""
进度跟踪器模块
实时记录优化进度和化合物生成状态
"""

import os
import time
import json
import logging
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional
import pandas as pd
from pathlib import Path

logger = logging.getLogger(__name__)

class ProgressTracker:
    """
    优化进度跟踪器
    实时记录化合物设计进度、保存CSV文件、提供进度日志
    """
    
    def __init__(self, 
                 output_dir: str,
                 total_candidates_target: int = 100,
                 csv_filename: str = "compounds_progress.csv"):
        """
        初始化进度跟踪器
        
        Args:
            output_dir: 输出目录
            total_candidates_target: 目标候选化合物总数
            csv_filename: CSV文件名
        """
        self.output_dir = output_dir
        self.total_candidates_target = total_candidates_target
        self.csv_path = os.path.join(output_dir, csv_filename)
        self.summary_path = os.path.join(output_dir, "progress_summary.json")
        
        # 确保输出目录存在
        os.makedirs(output_dir, exist_ok=True)
        
        # 进度状态
        self.start_time = time.time()
        self.current_compounds = 0
        self.completed_compounds = 0
        self.failed_compounds = 0
        self.total_generated = 0
        
        # 线程锁确保线程安全
        self._lock = threading.Lock()
        
        # 初始化CSV文件
        self._initialize_csv()
        
        # 创建进度摘要
        self._update_summary()
        
        logger.info(f"进度跟踪器初始化 - 目标: {total_candidates_target} 个化合物")
        logger.info(f"实时CSV记录: {self.csv_path}")
    
    def _initialize_csv(self):
        """初始化CSV文件头部"""
        try:
            # 检查CSV文件是否已存在
            if os.path.exists(self.csv_path):
                logger.info(f"发现已存在的CSV文件: {self.csv_path}")
                # 读取已有数据来恢复状态
                existing_df = pd.read_csv(self.csv_path)
                self.total_generated = len(existing_df)
                self.completed_compounds = len(existing_df[existing_df['status'] == 'completed'])
                self.failed_compounds = len(existing_df[existing_df['status'] == 'failed'])
                logger.info(f"从现有CSV恢复状态: {self.total_generated} 已生成, {self.completed_compounds} 已完成")
                return
            
            # 创建新的CSV文件
            columns = [
                'compound_id',
                'smiles',
                'generation_time',
                'generation_method',
                'parent_compound',
                'transformation_rule',
                'similarity_score',
                'status',
                'combined_score',
                'affinity_score',
                'confidence_score',
                'plddt_score',
                'binding_probability',
                'evaluation_time',
                'boltz_task_id',
                'error_message',
                'properties_mw',
                'properties_logp',
                'properties_hbd',
                'properties_hba'
            ]
            
            # 创建空的DataFrame并保存
            df = pd.DataFrame(columns=columns)
            df.to_csv(self.csv_path, index=False)
            
            logger.info(f"初始化CSV文件: {self.csv_path}")
            
        except Exception as e:
            logger.error(f"初始化CSV文件失败: {e}")
    
    def log_compound_generated(self, 
                              compound_data: Dict[str, Any],
                              parent_compound: str = "",
                              generation_method: str = "unknown") -> str:
        """
        记录新生成的化合物
        
        Args:
            compound_data: 化合物数据字典
            parent_compound: 父化合物SMILES
            generation_method: 生成方法
            
        Returns:
            compound_id: 化合物ID
        """
        with self._lock:
            self.total_generated += 1
            compound_id = f"compound_{self.total_generated:06d}"
            
            # 准备记录数据
            record = {
                'compound_id': compound_id,
                'smiles': compound_data.get('smiles', ''),
                'generation_time': datetime.now().isoformat(),
                'generation_method': generation_method,
                'parent_compound': parent_compound,
                'transformation_rule': compound_data.get('transformation_rule', ''),
                'similarity_score': compound_data.get('similarity', 0.0),
                'status': 'generated',  # 初始状态为生成，不是evaluating
                'combined_score': 0.0,
                'affinity_score': 0.0,
                'confidence_score': 0.0,
                'plddt_score': 0.0,
                'binding_probability': 0.0,
                'evaluation_time': '',
                'boltz_task_id': '',
                'error_message': '',
                'properties_mw': compound_data.get('properties', {}).get('molecular_weight', 0),
                'properties_logp': compound_data.get('properties', {}).get('logp', 0),
                'properties_hbd': compound_data.get('properties', {}).get('hbd', 0),
                'properties_hba': compound_data.get('properties', {}).get('hba', 0)
            }
            
            # 追加到CSV文件
            try:
                df = pd.DataFrame([record])
                df.to_csv(self.csv_path, mode='a', header=False, index=False)
                
                logger.info(f"📝 记录化合物 {compound_id}: {compound_data.get('smiles', '')[:30]}... "
                           f"方法: {generation_method}")
                
                # 更新进度日志
                self._log_progress()
                
            except Exception as e:
                logger.error(f"记录化合物失败 {compound_id}: {e}")
            
            return compound_id
    
    def update_compound_status(self, 
                              compound_id: str,
                              status: str,
                              scores: Optional[Dict[str, float]] = None,
                              boltz_task_id: str = "",
                              error_message: str = ""):
        """
        更新化合物状态和评分
        
        Args:
            compound_id: 化合物ID
            status: 状态 (evaluating, completed, failed)
            scores: 评分字典
            boltz_task_id: Boltz任务ID
            error_message: 错误信息
        """
        with self._lock:
            try:
                # 读取现有CSV
                df = pd.read_csv(self.csv_path)
                
                # 找到对应化合物行
                mask = df['compound_id'] == compound_id
                if not mask.any():
                    logger.warning(f"未找到化合物ID: {compound_id}")
                    return
                
                # 更新状态
                df.loc[mask, 'status'] = status
                df.loc[mask, 'evaluation_time'] = datetime.now().isoformat()
                
                if boltz_task_id:
                    df.loc[mask, 'boltz_task_id'] = boltz_task_id
                
                if error_message:
                    df.loc[mask, 'error_message'] = error_message
                
                # 更新评分
                if scores:
                    df.loc[mask, 'combined_score'] = scores.get('combined_score', 0.0)
                    df.loc[mask, 'affinity_score'] = scores.get('affinity', 0.0)
                    df.loc[mask, 'confidence_score'] = scores.get('confidence', 0.0)
                    df.loc[mask, 'plddt_score'] = scores.get('plddt', 0.0)
                    df.loc[mask, 'binding_probability'] = scores.get('binding_probability', 0.0)
                
                # 保存更新后的CSV
                df.to_csv(self.csv_path, index=False)
                
                # 更新计数器
                if status == 'completed' and df.loc[mask, 'status'].iloc[0] != 'completed':
                    self.completed_compounds += 1
                elif status == 'failed' and df.loc[mask, 'status'].iloc[0] != 'failed':
                    self.failed_compounds += 1
                
                # 获取SMILES用于日志
                smiles = df.loc[mask, 'smiles'].iloc[0]
                score_text = f"评分: {scores.get('combined_score', 0.0):.3f}" if scores else ""
                
                logger.info(f"✅ 更新 {compound_id} ({smiles[:20]}...): {status} {score_text}")
                
                # 更新进度日志
                self._log_progress()
                
            except Exception as e:
                logger.error(f"更新化合物状态失败 {compound_id}: {e}")
    
    def _log_progress(self):
        """记录当前进度到日志"""
        try:
            elapsed_time = time.time() - self.start_time
            progress_percentage = (self.total_generated / self.total_candidates_target * 100) if self.total_candidates_target > 0 else 0
            completion_percentage = (self.completed_compounds / self.total_generated * 100) if self.total_generated > 0 else 0
            
            # 估算剩余时间
            if self.completed_compounds > 0:
                avg_time_per_compound = elapsed_time / self.completed_compounds
                remaining_compounds = max(0, self.total_candidates_target - self.completed_compounds)
                estimated_remaining_time = remaining_compounds * avg_time_per_compound
                eta_text = f"预计剩余时间: {estimated_remaining_time/60:.1f}分钟"
            else:
                eta_text = "预计剩余时间: 计算中..."
            
            logger.info(f"📊 进度状态 - 已生成: {self.total_generated}/{self.total_candidates_target} "
                       f"({progress_percentage:.1f}%) | 已完成: {self.completed_compounds} "
                       f"({completion_percentage:.1f}%) | 失败: {self.failed_compounds} | {eta_text}")
            
            # 更新摘要文件
            self._update_summary()
            
        except Exception as e:
            logger.error(f"记录进度失败: {e}")
    
    def _update_summary(self):
        """更新进度摘要文件"""
        try:
            elapsed_time = time.time() - self.start_time
            
            summary = {
                'start_time': datetime.fromtimestamp(self.start_time).isoformat(),
                'current_time': datetime.now().isoformat(),
                'elapsed_time_seconds': elapsed_time,
                'elapsed_time_minutes': elapsed_time / 60,
                'target_compounds': self.total_candidates_target,
                'total_generated': self.total_generated,
                'completed_compounds': self.completed_compounds,
                'failed_compounds': self.failed_compounds,
                'in_progress': self.total_generated - self.completed_compounds - self.failed_compounds,
                'generation_progress_percent': (self.total_generated / self.total_candidates_target * 100) if self.total_candidates_target > 0 else 0,
                'completion_progress_percent': (self.completed_compounds / self.total_generated * 100) if self.total_generated > 0 else 0,
                'success_rate_percent': (self.completed_compounds / (self.completed_compounds + self.failed_compounds) * 100) if (self.completed_compounds + self.failed_compounds) > 0 else 0,
                'csv_file': self.csv_path,
                'output_dir': self.output_dir
            }
            
            # 计算性能指标
            if self.completed_compounds > 0:
                summary['avg_time_per_compound_seconds'] = elapsed_time / self.completed_compounds
                summary['compounds_per_minute'] = self.completed_compounds / (elapsed_time / 60)
                
                remaining_compounds = max(0, self.total_candidates_target - self.completed_compounds)
                estimated_remaining_time = remaining_compounds * summary['avg_time_per_compound_seconds']
                summary['estimated_remaining_time_minutes'] = estimated_remaining_time / 60
                summary['estimated_completion_time'] = (datetime.now().timestamp() + estimated_remaining_time)
            
            # 保存摘要
            with open(self.summary_path, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            
        except Exception as e:
            logger.error(f"更新进度摘要失败: {e}")
    
    def should_continue_generation(self) -> bool:
        """
        检查是否应该继续生成化合物
        
        Returns:
            bool: 是否继续生成
        """
        if self.total_generated >= self.total_candidates_target:
            logger.info(f"🎯 已达到目标数量 {self.total_candidates_target}，停止生成新化合物")
            return False
        
        return True
    
    def get_current_stats(self) -> Dict[str, Any]:
        """
        获取当前统计信息
        
        Returns:
            Dict: 当前统计信息
        """
        elapsed_time = time.time() - self.start_time
        
        return {
            'total_generated': self.total_generated,
            'completed_compounds': self.completed_compounds,
            'failed_compounds': self.failed_compounds,
            'target_compounds': self.total_candidates_target,
            'elapsed_time_minutes': elapsed_time / 60,
            'success_rate': (self.completed_compounds / (self.completed_compounds + self.failed_compounds)) if (self.completed_compounds + self.failed_compounds) > 0 else 0,
            'progress_percent': (self.total_generated / self.total_candidates_target * 100) if self.total_candidates_target > 0 else 0,
            'csv_path': self.csv_path,
            'summary_path': self.summary_path
        }
    
    def get_top_compounds(self, n: int = 10) -> List[Dict[str, Any]]:
        """
        获取评分最高的N个化合物
        
        Args:
            n: 返回的化合物数量
            
        Returns:
            List[Dict]: Top化合物列表
        """
        try:
            df = pd.read_csv(self.csv_path)
            
            # 过滤已完成的化合物
            completed_df = df[df['status'] == 'completed'].copy()
            
            if len(completed_df) == 0:
                return []
            
            # 按combined_score排序
            top_compounds = completed_df.nlargest(n, 'combined_score')
            
            return top_compounds.to_dict('records')
            
        except Exception as e:
            logger.error(f"获取Top化合物失败: {e}")
            return []
    
    def generate_progress_report(self) -> str:
        """
        生成进度报告
        
        Returns:
            str: 格式化的进度报告
        """
        try:
            stats = self.get_current_stats()
            elapsed_minutes = stats['elapsed_time_minutes']
            
            report = f"""
📊 化合物优化进度报告
{'='*50}
🎯 目标数量: {stats['target_compounds']} 个化合物
📝 已生成: {stats['total_generated']} 个 ({stats['progress_percent']:.1f}%)
✅ 已完成: {stats['completed_compounds']} 个
❌ 失败: {stats['failed_compounds']} 个
⚡ 成功率: {stats['success_rate']:.1%}
⏱️  运行时间: {elapsed_minutes:.1f} 分钟
💾 实时数据: {self.csv_path}
"""
            
            # 添加Top化合物信息
            top_compounds = self.get_top_compounds(3)
            if top_compounds:
                report += "\n🏆 当前最优化合物:\n"
                for i, compound in enumerate(top_compounds, 1):
                    smiles = compound['smiles'][:40] + "..." if len(compound['smiles']) > 40 else compound['smiles']
                    report += f"   {i}. {smiles} (评分: {compound['combined_score']:.3f})\n"
            
            return report
            
        except Exception as e:
            logger.error(f"生成进度报告失败: {e}")
            return f"进度报告生成失败: {e}"
    
    def finalize(self):
        """完成跟踪，生成最终报告"""
        try:
            logger.info("🏁 优化过程完成，生成最终报告...")
            
            # 生成最终统计
            final_stats = self.get_current_stats()
            
            # 保存最终报告
            final_report_path = os.path.join(self.output_dir, "final_progress_report.txt")
            with open(final_report_path, 'w', encoding='utf-8') as f:
                f.write(self.generate_progress_report())
                f.write("\n\n最终统计信息:\n")
                f.write(json.dumps(final_stats, indent=2, ensure_ascii=False))
            
            logger.info(f"✅ 最终报告已保存: {final_report_path}")
            logger.info(f"📊 总计生成: {final_stats['total_generated']} 个化合物")
            logger.info(f"✅ 成功完成: {final_stats['completed_compounds']} 个")
            logger.info(f"📈 成功率: {final_stats['success_rate']:.1%}")
            logger.info(f"💾 详细数据: {self.csv_path}")
            
        except Exception as e:
            logger.error(f"最终化处理失败: {e}")

class CompoundLimitController:
    """化合物数量限制控制器"""
    
    def __init__(self, max_total_compounds: int = 100):
        """
        初始化限制控制器
        
        Args:
            max_total_compounds: 最大化合物总数
        """
        self.max_total_compounds = max_total_compounds
        self.generated_count = 0
        self._lock = threading.Lock()
        
        logger.info(f"化合物数量控制器初始化 - 最大数量: {max_total_compounds}")
    
    def can_generate_more(self) -> bool:
        """检查是否还能生成更多化合物"""
        with self._lock:
            return self.generated_count < self.max_total_compounds
    
    def increment_count(self) -> int:
        """增加生成计数，返回当前数量"""
        with self._lock:
            if self.generated_count < self.max_total_compounds:
                self.generated_count += 1
            return self.generated_count
    
    def get_remaining_count(self) -> int:
        """获取剩余可生成数量"""
        with self._lock:
            return max(0, self.max_total_compounds - self.generated_count)
    
    def get_progress(self) -> Dict[str, Any]:
        """获取进度信息"""
        with self._lock:
            return {
                'generated': self.generated_count,
                'max_total': self.max_total_compounds,
                'remaining': self.get_remaining_count(),
                'progress_percent': (self.generated_count / self.max_total_compounds * 100) if self.max_total_compounds > 0 else 0
            }
