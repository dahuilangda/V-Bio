#!/usr/bin/env python3
"""
批次评估模块 - 支持批次提交和并发处理
"""

import os
import time
import yaml
import logging
from typing import List, Dict, Tuple, Optional, Any
from .optimization_engine import OptimizationCandidate

logger = logging.getLogger(__name__)

class BatchEvaluator:
    """批次评估器，支持批量提交和管理Boltz任务"""
    
    def __init__(self, boltz_client, scoring_system, batch_size: int = 4):
        self.boltz_client = boltz_client
        self.scoring_system = scoring_system
        self.batch_size = batch_size
        
    def evaluate_candidates_batch(self, 
                                candidates: List[OptimizationCandidate],
                                target_protein_yaml: str,
                                output_dir: str,
                                original_compound: str = None) -> List[OptimizationCandidate]:
        """
        批次评估候选化合物
        
        Args:
            candidates: 待评估的候选化合物列表
            target_protein_yaml: 目标蛋白质配置YAML路径
            output_dir: 输出目录
            original_compound: 原始化合物SMILES
            
        Returns:
            成功评估的候选化合物列表
        """
        evaluated_candidates = []
        
        # 读取目标蛋白质配置
        with open(target_protein_yaml, 'r') as f:
            target_config = yaml.safe_load(f)
        
        # 创建临时目录和CSV文件
        temp_config_dir = os.path.join(output_dir, "temp_configs")
        os.makedirs(temp_config_dir, exist_ok=True)
        
        csv_file = os.path.join(output_dir, "optimization_results.csv")
        self._initialize_csv_file(csv_file)
        
        logger.info(f"批次评估 {len(candidates)} 个候选化合物")
        logger.info(f"批次大小: {self.batch_size}")
        logger.info(f"实时结果保存到: {csv_file}")
        
        # 按批次处理候选化合物
        for batch_idx in range(0, len(candidates), self.batch_size):
            batch_end = min(batch_idx + self.batch_size, len(candidates))
            batch_candidates = candidates[batch_idx:batch_end]
            batch_num = batch_idx // self.batch_size + 1
            
            logger.info(f"🔄 处理批次 {batch_num}: 候选化合物 {batch_idx + 1}-{batch_end}")
            
            # 第一阶段：提交整个批次
            batch_tasks = self._submit_batch(batch_candidates, target_config, csv_file, original_compound)
            
            if not batch_tasks:
                logger.warning(f"批次 {batch_num} 没有成功提交的任务")
                continue
            
            logger.info(f"批次 {batch_num} 提交了 {len(batch_tasks)} 个任务")
            
            # 第二阶段：等待批次完成并处理结果
            batch_results = self._process_batch_results(batch_tasks, output_dir, csv_file, original_compound)
            evaluated_candidates.extend(batch_results)
            
            logger.info(f"✅ 批次 {batch_num} 完成，成功评估 {len(batch_results)} 个候选化合物")
        
        logger.info(f"🎉 总共成功评估 {len(evaluated_candidates)} 个候选化合物")
        return evaluated_candidates
    
    def _submit_batch(self, 
                     batch_candidates: List[OptimizationCandidate],
                     target_config: Dict,
                     csv_file: str,
                     original_compound: str) -> List[Tuple[OptimizationCandidate, str]]:
        """提交一个批次的候选化合物"""
        batch_tasks = []
        
        for candidate in batch_candidates:
            try:
                logger.info(f"📤 提交候选化合物: {candidate.compound_id}")
                
                # 创建候选化合物的配置
                config_yaml = self._create_candidate_config_yaml(candidate, target_config)
                
                if not config_yaml:
                    logger.warning(f"❌ 创建配置失败: {candidate.compound_id}")
                    self._write_csv_row(csv_file, candidate, original_compound, status="config_failed")
                    continue
                
                # 提交到Boltz-WebUI
                task_id = self.boltz_client.submit_optimization_job(
                    yaml_content=config_yaml,
                    job_name=f"opt_{candidate.compound_id}",
                    compound_smiles=candidate.smiles
                )
                
                if not task_id:
                    logger.warning(f"❌ 提交失败: {candidate.compound_id}")
                    self._write_csv_row(csv_file, candidate, original_compound, status="submit_failed")
                    continue
                
                logger.info(f"✅ 提交成功: {candidate.compound_id} -> 任务 {task_id}")
                batch_tasks.append((candidate, task_id))
                
            except Exception as e:
                logger.error(f"❌ 提交错误 {candidate.compound_id}: {e}")
                self._write_csv_row(csv_file, candidate, original_compound, status="submit_error")
        
        return batch_tasks
    
    def _process_batch_results(self, 
                              batch_tasks: List[Tuple[OptimizationCandidate, str]],
                              output_dir: str,
                              csv_file: str,
                              original_compound: str) -> List[OptimizationCandidate]:
        """处理批次结果"""
        evaluated_candidates = []
        
        logger.info(f"⏳ 等待 {len(batch_tasks)} 个任务完成...")
        
        for candidate, task_id in batch_tasks:
            try:
                logger.info(f"🔍 监控任务: {candidate.compound_id} ({task_id})")
                
                # 等待任务完成
                result = self.boltz_client.poll_job_status(task_id)
                
                if result and result.get('status') == 'completed':
                    logger.info(f"✅ 任务完成: {candidate.compound_id}")
                    
                    # 下载结果
                    result_dir = os.path.join(output_dir, "results", candidate.compound_id)
                    try:
                        result_files = self.boltz_client.download_results(task_id, result_dir)
                        
                        if result_files:
                            # 解析预测结果
                            prediction_results = self._parse_prediction_results(result_dir)
                            candidate.prediction_results = prediction_results
                            
                            # 立即评分
                            try:
                                score = self.scoring_system.score_compound(
                                    smiles=candidate.smiles,
                                    boltz_results=candidate.prediction_results,
                                    reference_smiles=original_compound
                                )
                                candidate.scores = score
                                
                                # 立即写入CSV
                                self._write_csv_row(csv_file, candidate, original_compound, 
                                                  status="completed", score=score, task_id=task_id)
                                
                                evaluated_candidates.append(candidate)
                                logger.info(f"🎯 {candidate.compound_id} 评分完成 - 分数: {score.combined_score:.4f}")
                                
                            except Exception as e:
                                logger.error(f"❌ 评分失败 {candidate.compound_id}: {e}")
                                self._write_csv_row(csv_file, candidate, original_compound, 
                                                  status="scoring_failed", task_id=task_id)
                        else:
                            logger.warning(f"❌ 结果下载失败: {candidate.compound_id}")
                            self._write_csv_row(csv_file, candidate, original_compound, 
                                              status="download_failed", task_id=task_id)
                    
                    except Exception as e:
                        logger.warning(f"❌ 结果处理错误 {candidate.compound_id}: {e}")
                        self._write_csv_row(csv_file, candidate, original_compound, 
                                          status="download_error", task_id=task_id)
                else:
                    logger.warning(f"❌ 任务失败或超时: {candidate.compound_id}")
                    error_msg = result.get('error', '未知错误') if result else '超时'
                    self._write_csv_row(csv_file, candidate, original_compound, 
                                      status=f"task_failed_{error_msg}", task_id=task_id)
                    
            except Exception as e:
                logger.error(f"❌ 任务处理错误 {task_id}: {e}")
                self._write_csv_row(csv_file, candidate, original_compound, 
                                  status="task_error", task_id=task_id)
        
        return evaluated_candidates
    
    def _create_candidate_config_yaml(self, candidate: OptimizationCandidate, target_config: Dict) -> Optional[str]:
        """为候选化合物创建YAML配置"""
        try:
            import copy
            config = copy.deepcopy(target_config)
            
            # 添加候选化合物作为配体
            ligand_id = "B"  # 使用Boltz要求的简单字母ID
            ligand_entry = {
                "ligand": {
                    "id": ligand_id,
                    "smiles": candidate.smiles
                }
            }
            
            # 将配体添加到序列中
            config["sequences"].append(ligand_entry)
            
            # 设置亲和力属性
            if "properties" not in config:
                config["properties"] = []
            
            config["properties"].append({
                "affinity": {
                    "binder": ligand_id
                }
            })
            
            # 转换为YAML字符串
            yaml_content = yaml.dump(config, default_flow_style=False, allow_unicode=True)
            return yaml_content
            
        except Exception as e:
            logger.error(f"创建候选配置失败 {candidate.compound_id}: {e}")
            return None
    
    def _parse_prediction_results(self, result_dir: str) -> Dict[str, Any]:
        """解析预测结果"""
        # 这里使用与optimization_engine相同的解析逻辑
        results = {}
        
        # 解析亲和力数据
        affinity_file = os.path.join(result_dir, 'affinity_data.json')
        if os.path.exists(affinity_file):
            import json
            with open(affinity_file, 'r') as f:
                affinity_data = json.load(f)
                results.update(affinity_data)
        
        # 解析置信度数据
        confidence_file = os.path.join(result_dir, 'confidence_data_model_0.json')
        if os.path.exists(confidence_file):
            import json
            with open(confidence_file, 'r') as f:
                confidence_data = json.load(f)
                results.update(confidence_data)
        
        return results
    
    def _initialize_csv_file(self, csv_file: str):
        """初始化CSV文件"""
        import csv
        with open(csv_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'compound_id', 'original_smiles', 'optimized_smiles',
                'mmp_transformation', 'status', 'task_id', 'combined_score',
                'binding_affinity', 'drug_likeness', 'synthetic_accessibility',
                'novelty', 'stability', 'plddt', 'iptm', 'binding_probability',
                'ic50_um', 'molecular_weight', 'logp', 'lipinski_violations', 'qed_score'
            ])
    
    def _write_csv_row(self, csv_file: str, candidate: OptimizationCandidate, 
                      original_compound: str, status: str, 
                      score: Optional[Any] = None, task_id: str = None):
        """写入CSV行"""
        import csv
        from datetime import datetime
        
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 基础数据
        row_data = {
            'timestamp': timestamp,
            'compound_id': candidate.compound_id,
            'original_smiles': original_compound or '',
            'optimized_smiles': candidate.smiles,
            'mmp_transformation': getattr(candidate, 'transformation', ''),
            'status': status,
            'task_id': task_id or '',
        }
        
        # 评分数据
        if score:
            row_data.update({
                'combined_score': f"{score.combined_score:.4f}",
                'binding_affinity': f"{score.binding_affinity:.4f}",
                'drug_likeness': f"{score.drug_likeness:.4f}",
                'synthetic_accessibility': f"{score.synthetic_accessibility:.4f}",
                'novelty': f"{score.novelty:.4f}",
                'stability': f"{score.stability:.4f}",
                'plddt': f"{getattr(score, 'plddt', 0):.4f}",
                'iptm': f"{getattr(score, 'iptm', 0):.4f}",
                'binding_probability': f"{getattr(score, 'binding_probability', 0):.4f}",
                'ic50_um': f"{getattr(score, 'ic50_um', 0):.4f}",
                'molecular_weight': f"{getattr(score, 'molecular_weight', 0):.2f}",
                'logp': f"{getattr(score, 'logp', 0):.2f}",
                'lipinski_violations': f"{getattr(score, 'lipinski_violations', 0)}",
                'qed_score': f"{getattr(score, 'qed_score', 0):.4f}",
            })
        else:
            # 空值填充
            empty_fields = ['combined_score', 'binding_affinity', 'drug_likeness', 
                          'synthetic_accessibility', 'novelty', 'stability', 'plddt', 
                          'iptm', 'binding_probability', 'ic50_um', 'molecular_weight', 
                          'logp', 'lipinski_violations', 'qed_score']
            for field in empty_fields:
                row_data[field] = ''
        
        # 写入CSV
        with open(csv_file, 'a', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=row_data.keys())
            writer.writerow(row_data)
