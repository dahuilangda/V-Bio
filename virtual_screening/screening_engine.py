# /Boltz-WebUI/virtual_screening/screening_engine.py

"""
screening_engine_simplified.py

虚拟筛选引擎：
1. ScreeningEngine: 主要筛选逻辑控制器
2. ScoringSystem: 评分系统  
3. BatchManager: 批量作业管理
4. ResultProcessor: 结果处理器
"""

import os
import yaml
import time
import json
import shutil
import logging
import tempfile
import concurrent.futures
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict
from pathlib import Path
import pandas as pd
import numpy as np

from api_client import BoltzApiClient
from molecule_library import MoleculeLibrary, Molecule, LibraryProcessor
from affinity_calculator import SmallMoleculeAffinityEnhancer
from html_reporter import HTMLReporter

logger = logging.getLogger(__name__)

@dataclass
class ScreeningResult:
    """筛选结果数据类"""
    molecule_id: str
    molecule_name: str
    sequence: str
    mol_type: str
    binding_score: float
    confidence_score: float
    structural_score: float
    combined_score: float
    rank: int = 0
    properties: Dict[str, Any] = None
    structure_path: str = ""
    
    def __post_init__(self):
        if self.properties is None:
            self.properties = {}

@dataclass
class ScreeningConfig:
    """筛选配置参数"""
    # 基本参数
    target_yaml: str
    library_path: str
    library_type: str
    output_dir: str
    
    # 筛选参数
    max_molecules: int = -1  # -1表示全部
    batch_size: int = 50
    max_workers: int = 4
    timeout: int = 1800
    retry_attempts: int = 3
    
    # 评分参数
    scoring_weights: Dict[str, float] = None
    min_binding_score: float = 0.0
    top_n: int = 100
    
    # 高级参数
    use_msa_server: bool = False
    save_structures: bool = True
    generate_plots: bool = True
    
    # 新增：亲和力计算参数
    auto_enable_affinity: bool = True  # 自动启用亲和力计算
    enable_affinity: bool = False
    target_sequence: str = ""
    
    def __post_init__(self):
        if self.scoring_weights is None:
            self.scoring_weights = {
                "binding_affinity": 0.6,
                "structural_stability": 0.2,
                "confidence": 0.2
            }

class ScoringSystem:
    """评分系统"""
    
    def __init__(self, weights: Dict[str, float] = None):
        self.weights = weights or {
            "binding_affinity": 0.6,
            "structural_stability": 0.2,
            "confidence": 0.2
        }
    
    def calculate_binding_score(self, metrics: Dict[str, Any]) -> float:
        """计算结合亲和力评分"""
        # 基于ipTM分数计算结合亲和力
        iptm = metrics.get('iptm', 0.0)
        return float(iptm)
    
    def calculate_structural_score(self, metrics: Dict[str, Any]) -> float:
        """计算结构稳定性评分"""
        # 基于pLDDT分数计算结构稳定性
        plddt = metrics.get('plddt', 0.0)
        
        # pLDDT值通常在0-1范围内（已经是归一化的），不需要除以100
        # 如果pLDDT值大于1，说明是0-100范围的，需要归一化
        if plddt > 1.0:
            return float(plddt) / 100.0
        else:
            return float(plddt)  # 已经是0-1范围
    
    def calculate_confidence_score(self, metrics: Dict[str, Any]) -> float:
        """计算预测置信度评分"""
        # 综合多个置信度指标
        confidence_scores = []
        
        if 'plddt' in metrics:
            confidence_scores.append(metrics['plddt'] / 100.0)
        
        if 'ptm' in metrics:
            confidence_scores.append(metrics['ptm'])
        
        if 'iptm' in metrics:
            confidence_scores.append(metrics['iptm'])
        
        return float(np.mean(confidence_scores)) if confidence_scores else 0.0
    
    def calculate_combined_score(self, binding_score: float, structural_score: float, 
                               confidence_score: float) -> float:
        """计算综合评分"""
        combined = (
            binding_score * self.weights.get("binding_affinity", 0.6) +
            structural_score * self.weights.get("structural_stability", 0.2) +
            confidence_score * self.weights.get("confidence", 0.2)
        )
        return float(combined)
    
    def score_molecule(self, molecule: Molecule, prediction_results: Dict[str, Any]) -> ScreeningResult:
        """为单个分子计算所有评分"""
        # 解析预测结果中的指标
        metrics = self._parse_prediction_metrics(prediction_results)
        
        # 计算各项评分
        binding_score = self.calculate_binding_score(metrics)
        structural_score = self.calculate_structural_score(metrics)
        confidence_score = self.calculate_confidence_score(metrics)
        combined_score = self.calculate_combined_score(binding_score, structural_score, confidence_score)
        
        # 创建筛选结果
        result = ScreeningResult(
            molecule_id=molecule.id,
            molecule_name=molecule.name,
            sequence=molecule.sequence,
            mol_type=molecule.mol_type,
            binding_score=binding_score,
            confidence_score=confidence_score,
            structural_score=structural_score,
            combined_score=combined_score,
            properties=molecule.properties.copy()
        )
        
        # 添加预测指标到属性中
        result.properties.update(metrics)
        
        return result
    
    def _parse_prediction_metrics(self, prediction_results: Dict[str, Any]) -> Dict[str, float]:
        """解析预测结果中的指标"""
        metrics = {}
        
        # 尝试从不同可能的位置提取指标
        if 'confidence_metrics' in prediction_results:
            conf_metrics = prediction_results['confidence_metrics']
            if isinstance(conf_metrics, dict):
                metrics.update(conf_metrics)
        
        # 直接从结果中提取所有数值字段
        numeric_keys = [
            'iptm', 'ptm', 'plddt', 'confidence', 
            'ligand_iptm', 'protein_iptm', 'complex_iplddt',
            'affinity_pred_value', 'ic50_uM', 'pIC50', 'delta_g_kcal_mol', 
            'binding_probability', 'affinity'
        ]
        
        for key in numeric_keys:
            if key in prediction_results:
                try:
                    metrics[key] = float(prediction_results[key])
                except (ValueError, TypeError):
                    logger.warning(f"无法转换字段 {key} 为数值: {prediction_results[key]}")
        
        # 如果有ranking结果，使用ranking中的指标
        if 'ranking' in prediction_results:
            ranking = prediction_results['ranking']
            if isinstance(ranking, list) and ranking:
                best_model = ranking[0]
                if isinstance(best_model, dict):
                    for key in ['iptm', 'ptm', 'plddt']:
                        if key in best_model:
                            try:
                                metrics[key] = float(best_model[key])
                            except (ValueError, TypeError):
                                pass
        
        return metrics

class SimpleBatchManager:
    """简化的批量作业管理器"""
    
    def __init__(self, client: BoltzApiClient, config: ScreeningConfig, affinity_enhancer=None):
        self.client = client
        self.config = config
        
        # 在输出目录创建临时文件夹，而不是在/tmp
        self.temp_dir = os.path.join(config.output_dir, "temp_configs")
        os.makedirs(self.temp_dir, exist_ok=True)
        
        # 创建任务目录
        self.task_dir = os.path.join(config.output_dir, "tasks")
        os.makedirs(self.task_dir, exist_ok=True)
        
        self.active_jobs: Dict[str, Dict] = {}
        
        # 亲和力增强器引用
        self.affinity_enhancer = affinity_enhancer
        
        logger.info(f"临时配置目录: {self.temp_dir}")
        logger.info(f"任务目录: {self.task_dir}")
    
    def prepare_molecule_configs(self, molecules: List[Molecule], target_config: Dict) -> List[str]:
        """为分子列表准备配置文件"""
        config_files = []
        
        for i, molecule in enumerate(molecules):
            mol_id = f"mol_{i+1:04d}_{molecule.id}"
            config_file = self._create_molecule_config(molecule, target_config, mol_id)
            if config_file:
                config_files.append(config_file)
        
        logger.info(f"准备了 {len(config_files)} 个分子配置文件")
        return config_files
    
    def _create_molecule_config(self, molecule: Molecule, target_config: Dict, mol_id: str) -> str:
        """为单个分子创建配置文件"""
        try:
            # 深拷贝目标配置
            import copy
            mol_config = copy.deepcopy(target_config)
            
            # 重新构建sequences列表，只保留蛋白质序列
            mol_config['sequences'] = []
            
            # 从原始配置中提取蛋白质序列，并收集已使用的ID
            original_sequences = target_config.get('sequences', [])
            used_ids = set()
            
            for seq in original_sequences:
                if 'protein' in seq:
                    mol_config['sequences'].append(seq)
                    used_ids.add(seq['protein']['id'])
                elif 'ligand' in seq:
                    # 跳过原始配体，我们将替换为新的候选分子
                    used_ids.add(seq['ligand']['id'])
            
            # 为候选分子自动分配ID
            candidate_id = self._assign_molecule_id(used_ids, molecule.mol_type)
            
            # 添加候选分子序列
            if molecule.mol_type == "peptide":
                # 多肽类型
                sequence_entry = {
                    "protein": {
                        "id": candidate_id,
                        "sequence": molecule.sequence,
                        "msa": "empty"
                    }
                }
            elif molecule.mol_type == "small_molecule":
                # 小分子类型 - 使用ligand格式
                sequence_entry = {
                    "ligand": {
                        "id": candidate_id,
                        "smiles": molecule.sequence  # 确保SMILES格式正确
                    }
                }
            else:
                logger.warning(f"不支持的分子类型: {molecule.mol_type}")
                return None
            
            # 将候选分子添加到配置中
            mol_config["sequences"].append(sequence_entry)
            
            # 如果是小分子且启用了亲和力计算，添加properties部分
            if molecule.mol_type == "small_molecule" and self.affinity_enhancer:
                mol_config["properties"] = [
                    {
                        "affinity": {
                            "binder": candidate_id  # 使用动态分配的配体ID
                        }
                    }
                ]
                logger.info(f"为小分子 {mol_id} 添加亲和力计算配置，binder ID: {candidate_id}")
            
            # 确保version字段存在
            if 'version' not in mol_config:
                mol_config['version'] = 1
            
            # 保存配置文件
            config_path = os.path.join(self.temp_dir, f"{mol_id}.yaml")
            with open(config_path, 'w') as f:
                yaml.dump(mol_config, f, default_flow_style=False, allow_unicode=True)
            
            # 验证生成的配置文件
            if not self._validate_config_file(config_path):
                logger.error(f"生成的配置文件验证失败: {config_path}")
                return None
            
            logger.debug(f"创建分子配置文件: {config_path}")
            return config_path
            
        except Exception as e:
            logger.error(f"创建分子配置失败: {e}")
            return None
    
    def _validate_config_file(self, config_path: str) -> bool:
        """验证配置文件格式"""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            
            # 检查必需字段
            if 'sequences' not in config:
                logger.error("配置文件缺少sequences字段")
                return False
            
            if not isinstance(config['sequences'], list):
                logger.error("sequences字段必须是列表")
                return False
            
            if len(config['sequences']) == 0:
                logger.error("sequences列表不能为空")
                return False
            
            # 检查是否有有效的序列
            has_protein = False
            has_ligand = False
            
            for seq in config['sequences']:
                if 'protein' in seq:
                    has_protein = True
                    protein = seq['protein']
                    if 'id' not in protein or 'sequence' not in protein:
                        logger.error("蛋白质序列缺少必需字段")
                        return False
                elif 'ligand' in seq:
                    has_ligand = True
                    ligand = seq['ligand']
                    if 'id' not in ligand or 'smiles' not in ligand:
                        logger.error("配体序列缺少必需字段")
                        return False
            
            if not has_protein:
                logger.error("配置文件必须包含至少一个蛋白质序列")
                return False
            
            logger.debug(f"配置文件验证通过: {config_path}")
            return True
            
        except Exception as e:
            logger.error(f"验证配置文件时发生错误: {e}")
            return False
    
    def _assign_molecule_id(self, used_ids: set, mol_type: str) -> str:
        """为分子自动分配唯一ID"""
        # 常用的ID字母序列
        candidate_letters = ['B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z']
        
        # 尝试从B开始分配（B通常是配体的默认ID）
        for letter in candidate_letters:
            if letter not in used_ids:
                return letter
        
        # 如果字母用完了，使用数字后缀
        for i in range(1, 100):
            candidate_id = f"B{i}"
            if candidate_id not in used_ids:
                return candidate_id
        
        # 最后的备选方案
        return f"MOL_{mol_type}_{len(used_ids)}"
    
    def submit_and_monitor_molecules(self, config_files: List[str], molecules: List[Molecule]) -> Dict[str, Dict]:
        """提交并监控分子"""
        all_results = {}
        
        if self.config.max_workers > 1 and len(config_files) > 1:
            logger.info(f"并行处理 {len(config_files)} 个分子")
            all_results = self._process_parallel(config_files, molecules)
        else:
            logger.info(f"串行处理 {len(config_files)} 个分子")
            all_results = self._process_sequential(config_files, molecules)
        
        return all_results
    
    def _process_sequential(self, config_files: List[str], molecules: List[Molecule]) -> Dict[str, Dict]:
        """串行处理分子"""
        all_results = {}
        
        for i, (config_file, molecule) in enumerate(zip(config_files, molecules)):
            mol_name = f"mol_{i+1:04d}_{molecule.id}"
            
            logger.info(f"正在处理分子 {i+1}/{len(config_files)}: {mol_name}")
            
            # 提交单个分子
            task_id = self.client.submit_screening_job(
                yaml_path=config_file,
                job_name=mol_name,
                use_msa_server=self.config.use_msa_server
            )
            
            if not task_id:
                logger.error(f"分子 {mol_name} 提交失败")
                continue
            
            logger.info(f"分子 {mol_name} 提交成功: {task_id}")
            
            # 监控这个分子直到完成
            result = self.client.poll_status(task_id, poll_interval=15, max_wait_time=self.config.timeout)
            
            if result:
                all_results[task_id] = result
                status = result.get('status', 'unknown')
                logger.info(f"分子 {mol_name} 完成，状态: {status}")
                
                # 立即处理完成的任务
                if status == 'completed':
                    logger.info(f"任务 {task_id} 已完成，开始立即处理...")
                    self._process_completed_task_immediately(task_id, molecule, result)
                
                # 保存任务记录
                self._save_task_record(task_id, mol_name, molecule, status)
            else:
                logger.error(f"分子 {mol_name} 监控失败")
        
        return all_results
    
    def _process_parallel(self, config_files: List[str], molecules: List[Molecule]) -> Dict[str, Dict]:
        """并行处理分子"""
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_workers) as executor:
            # 提交所有任务
            future_to_info = {}
            for i, (config_file, molecule) in enumerate(zip(config_files, molecules)):
                mol_name = f"mol_{i+1:04d}_{molecule.id}"
                future = executor.submit(self._process_single_molecule, config_file, mol_name, molecule)
                future_to_info[future] = (mol_name, molecule)
            
            # 收集结果
            all_results = {}
            for future in concurrent.futures.as_completed(future_to_info):
                mol_name, molecule = future_to_info[future]
                try:
                    task_id, result = future.result()
                    if result:
                        all_results[task_id] = result
                        status = result.get('status', 'unknown')
                        logger.info(f"分子 {mol_name} 完成，状态: {status}")
                        
                        # 保存任务记录
                        self._save_task_record(task_id, mol_name, molecule, status)
                except Exception as e:
                    logger.error(f"分子 {mol_name} 处理失败: {e}")
        
        return all_results
    
    def _process_single_molecule(self, config_file: str, mol_name: str, molecule: Molecule) -> Tuple[str, Dict]:
        """处理单个分子"""
        logger.info(f"开始处理分子: {mol_name}")
        logger.debug(f"配置文件路径: {config_file}")
        
        # 验证配置文件存在
        if not os.path.exists(config_file):
            logger.error(f"配置文件不存在: {config_file}")
            return None, None
        
        # 读取并验证配置文件内容
        try:
            with open(config_file, 'r') as f:
                config_content = f.read()
            logger.debug(f"配置文件内容预览 ({mol_name}):\n{config_content[:500]}...")
        except Exception as e:
            logger.error(f"读取配置文件失败: {e}")
            return None, None
        
        # 提交任务
        task_id = self.client.submit_screening_job(
            yaml_path=config_file,
            job_name=mol_name,
            use_msa_server=self.config.use_msa_server
        )
        
        if not task_id:
            logger.error(f"分子 {mol_name} 提交失败")
            return None, None
        
        logger.info(f"分子 {mol_name} 提交成功: {task_id}")
        logger.info(f"开始监控分子 {mol_name}...")
        
        # 监控任务直到完成
        result = self.client.poll_status(task_id, poll_interval=15, max_wait_time=self.config.timeout)
        
        if result:
            status = result.get('status', 'unknown')
            logger.info(f"分子 {mol_name} 监控完成，最终状态: {status}")
            logger.info(f"状态检查: status == 'completed' 结果为 {status == 'completed'}")
            
            # 立即处理完成的任务
            if status == 'completed':
                logger.info(f"任务 {task_id} 已完成，开始立即处理...")
                # 立即下载和处理结果
                self._process_completed_task_immediately(task_id, molecule, result)
            elif status == 'failed':
                # 处理失败任务
                error_info = result.get('result', {})
                if isinstance(error_info, dict) and 'error' in error_info:
                    logger.error(f"分子 {mol_name} 失败原因: {error_info['error']}")
                elif 'traceback' in result:
                    logger.error(f"分子 {mol_name} 错误堆栈: {result['traceback']}")
                else:
                    logger.error(f"分子 {mol_name} 失败，但未获取到详细错误信息")
        else:
            logger.error(f"分子 {mol_name} 监控失败或超时")
        
        return task_id, result
    
    def _save_task_record(self, task_id: str, mol_name: str, molecule: Molecule, status: str):
        """保存任务记录，包含完整的分子信息"""
        try:
            record = {
                'task_id': task_id,
                'molecule_name': mol_name,
                'molecule_id': molecule.id,
                'sequence': molecule.sequence.strip() if molecule.sequence else "",  # 确保SMILES规范化
                'mol_type': molecule.mol_type,
                'status': status,
                'timestamp': time.time(),
                'human_time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'properties': molecule.properties if hasattr(molecule, 'properties') else {}
            }
            
            record_file = os.path.join(self.task_dir, f"task_{task_id}.json")
            with open(record_file, 'w') as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
            
            logger.debug(f"任务记录已保存: {record_file}")
                
        except Exception as e:
            logger.warning(f"保存任务记录失败: {e}")
    
    def _process_completed_task_immediately(self, task_id: str, molecule: Molecule, result: Dict):
        """立即处理完成的任务（下载结果并添加到筛选结果中）"""
        try:
            logger.info(f"开始立即处理任务 {task_id}")
            mol_name = f"mol_{molecule.id}_{molecule.name}" if hasattr(molecule, 'name') else f"mol_{molecule.id}"
            
            # 下载结果到tasks文件夹下的单独任务目录
            result_dir = os.path.join(self.config.output_dir, "tasks", f"task_{task_id}")
            logger.info(f"开始下载任务 {task_id} 结果到: {result_dir}")
            success = self.client.download_results(task_id, result_dir)
            if not success:
                logger.error(f"任务 {task_id} 结果下载失败")
                self._save_task_record(task_id, mol_name, molecule, 'failed')
                return False
            
            logger.info(f"任务 {task_id} 结果下载完成: {result_dir}")
            
            # 解析结果
            logger.info(f"开始解析任务 {task_id} 结果")
            prediction_results = self._parse_result_dir(result_dir)
            if not prediction_results:
                logger.error(f"任务 {task_id} 结果解析失败")
                self._save_task_record(task_id, mol_name, molecule, 'failed')
                return False
            
            logger.info(f"任务 {task_id} 结果解析成功: {prediction_results}")
            
            # 计算评分并创建筛选结果
            if hasattr(self, 'engine') and self.engine:
                logger.info(f"开始为任务 {task_id} 计算评分")
                screening_result = self.engine.scoring_system.score_molecule(molecule, prediction_results)
                screening_result.structure_path = result_dir
                
                # 直接添加到引擎的结果列表中
                self.engine.screening_results.append(screening_result)
                logger.info(f"任务 {task_id} 处理成功，评分: {screening_result.combined_score:.4f}")
            else:
                logger.warning(f"无法访问engine或scoring_system，任务 {task_id} 结果未评分")
                logger.warning(f"hasattr(self, 'engine'): {hasattr(self, 'engine')}")
                if hasattr(self, 'engine'):
                    logger.warning(f"self.engine: {self.engine}")
            
            # 保存成功的任务记录
            self._save_task_record(task_id, mol_name, molecule, 'completed')
            return True
            
        except Exception as e:
            logger.error(f"立即处理任务 {task_id} 失败: {e}")
            import traceback
            logger.error(f"错误堆栈: {traceback.format_exc()}")
            self._save_task_record(task_id, mol_name, molecule, 'failed')
            return False
    
    def _parse_result_dir(self, result_dir: str) -> Dict[str, Any]:
        """解析结果目录，提取预测指标"""
        try:
            result_data = {}
            
            # 1. 查找confidence_data_model_0.json文件（实际的文件名）
            confidence_file = os.path.join(result_dir, "confidence_data_model_0.json")
            if os.path.exists(confidence_file):
                with open(confidence_file, 'r') as f:
                    confidence_data = json.load(f)
                
                # 提取confidence相关指标，使用实际的字段名
                result_data.update({
                    'confidence': confidence_data.get('confidence_score', 0.0),
                    'iptm': confidence_data.get('iptm', 0.0),
                    'ptm': confidence_data.get('ptm', 0.0),
                    'plddt': confidence_data.get('complex_plddt', 0.0),
                    'ligand_iptm': confidence_data.get('ligand_iptm', 0.0),
                    'protein_iptm': confidence_data.get('protein_iptm', 0.0),
                    'complex_iplddt': confidence_data.get('complex_iplddt', 0.0)
                })
                logger.debug(f"读取置信度数据: confidence={result_data['confidence']:.4f}, iptm={result_data['iptm']:.4f}, ptm={result_data['ptm']:.4f}")
            
            # 2. 查找affinity_data.json文件（亲和力数据）
            affinity_file = os.path.join(result_dir, "affinity_data.json")
            if os.path.exists(affinity_file):
                with open(affinity_file, 'r') as f:
                    affinity_data = json.load(f)
                
                # 提取亲和力预测值
                affinity_pred = affinity_data.get('affinity_pred_value', None)
                binding_prob = affinity_data.get('affinity_probability_binary', None)
                
                if affinity_pred is not None:
                    # 计算IC50相关指标
                    # 根据文档：affinity_pred_value是log(IC50)，单位为μM
                    # IC50 (μM) = 10^affinity_pred_value
                    ic50_uM = 10 ** affinity_pred
                    
                    # 计算pIC50 = -log10(IC50_M) = -log10(IC50_uM * 1e-6) = 6 - log10(IC50_uM)
                    pIC50 = 6 - affinity_pred
                    
                    # 计算结合自由能 ΔG (kcal/mol) = (6 - affinity_pred) * 1.364
                    delta_g_kcal_mol = pIC50 * 1.364
                    
                    result_data.update({
                        'affinity_pred_value': affinity_pred,  # 原始预测值
                        'ic50_uM': ic50_uM,                    # IC50 (μM)
                        'pIC50': pIC50,                        # pIC50
                        'delta_g_kcal_mol': delta_g_kcal_mol,  # ΔG (kcal/mol)
                        'binding_probability': binding_prob,    # 结合概率
                        'affinity': affinity_pred  # 用于后续计算
                    })
                    
                    logger.debug(f"读取亲和力数据: 预测值={affinity_pred:.4f}, IC50={ic50_uM:.2f}μM, pIC50={pIC50:.2f}, ΔG={delta_g_kcal_mol:.2f}kcal/mol")
            
            # 3. 兼容旧格式：查找confidence_metrics.json
            if not result_data:
                confidence_metrics_file = os.path.join(result_dir, "confidence_metrics.json")
                if os.path.exists(confidence_metrics_file):
                    with open(confidence_metrics_file, 'r') as f:
                        return json.load(f)
            
            return result_data if result_data else {}
            
        except Exception as e:
            logger.error(f"解析结果目录失败: {e}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
            return None

    def cleanup(self):
        """清理临时文件"""
        try:
            # 只有在成功时才清理，失败时保留以便调试
            if hasattr(self, '_cleanup_enabled') and self._cleanup_enabled:
                if os.path.exists(self.temp_dir):
                    shutil.rmtree(self.temp_dir)
                    logger.info(f"清理临时目录: {self.temp_dir}")
            else:
                logger.info(f"保留临时目录以便调试: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"清理临时目录失败: {e}")

class SimpleScreeningEngine:
    """简化版虚拟筛选引擎"""
    
    def __init__(self, client: BoltzApiClient, config: ScreeningConfig):
        self.client = client
        self.config = config
        self.scoring_system = ScoringSystem(config.scoring_weights)
        
        # 创建输出目录
        os.makedirs(config.output_dir, exist_ok=True)
        
        # 初始化结果存储
        self.screening_results: List[ScreeningResult] = []
        self.failed_molecules: List[str] = []
        
        # 初始化亲和力计算器（智能判断是否需要）
        self.affinity_enhancer = None
        self._auto_enable_affinity_calculation()
        
        # 创建简化的批处理管理器
        self.batch_manager = SimpleBatchManager(client, config, self.affinity_enhancer)
        # 建立双向引用，让batch_manager能够访问engine的scoring_system
        self.batch_manager.engine = self
        
        logger.info(f"简化版虚拟筛选引擎已初始化，输出目录: {config.output_dir}")
        if self.affinity_enhancer:
            logger.info("亲和力计算已启用")
    
    def _auto_enable_affinity_calculation(self):
        """自动判断是否启用亲和力计算"""
        try:
            logger.info("开始自动启用亲和力计算检查")
            
            # 如果用户明确禁用了自动启用功能
            if hasattr(self.config, 'auto_enable_affinity') and not self.config.auto_enable_affinity:
                logger.info("用户已禁用自动启用亲和力计算功能")
                return
            
            # 检查是否是小分子筛选
            if hasattr(self.config, 'library_type') and self.config.library_type in ['small_molecule', 'ligand', 'compound', 'chemical']:
                logger.info(f"检测到小分子筛选，library_type: {self.config.library_type}")
                
                # 尝试从目标YAML文件获取蛋白质序列
                target_sequence = self._extract_target_protein_sequence()
                
                if target_sequence:
                    logger.info(f"从目标文件提取到蛋白质序列: {len(target_sequence)} 个氨基酸")
                    try:
                        self.affinity_enhancer = SmallMoleculeAffinityEnhancer(self.client, target_sequence)
                        # 自动更新配置标记
                        self.config.enable_affinity = True
                        logger.info(f"成功提取蛋白质序列，长度: {len(target_sequence)}")
                        logger.info("🧪 亲和力计算已自动启用（基于library_type检测）")
                        return
                    except Exception as e:
                        logger.error(f"创建SmallMoleculeAffinityEnhancer失败: {e}")
                        self.affinity_enhancer = None
                else:
                    logger.warning("无法获取目标蛋白质序列，亲和力计算未启用")
                    self.affinity_enhancer = None
            else:
                logger.info(f"基于library_type检测：非小分子筛选 (library_type: {getattr(self.config, 'library_type', 'None')})")
            
        except Exception as e:
            logger.error(f"自动启用亲和力计算时发生错误: {e}")
            self.affinity_enhancer = None
    
    def _extract_target_protein_sequence(self) -> Optional[str]:
        """从目标YAML文件中提取蛋白质序列"""
        try:
            with open(self.config.target_yaml, 'r') as f:
                target_config = yaml.safe_load(f)
            
            sequences = target_config.get('sequences', [])
            for seq in sequences:
                if 'protein' in seq:
                    protein_seq = seq['protein'].get('sequence', '')
                    if protein_seq:
                        return protein_seq
            
            logger.warning("在目标配置文件中未找到蛋白质序列")
            return None
            
        except Exception as e:
            logger.error(f"提取目标蛋白质序列失败: {e}")
            return None
    
    def run_screening(self) -> bool:
        """运行简化的虚拟筛选流程"""
        try:
            logger.info("开始虚拟筛选流程")
            start_time = time.time()
            
            # 检查是否支持续算
            if self._should_resume():
                logger.info("检测到现有结果，启用续算模式")
                return self._resume_screening()
            
            # 开始新的筛选
            logger.info("开始新的筛选任务")
            return self._run_new_screening()
            
        except Exception as e:
            logger.error(f"虚拟筛选过程中发生错误: {e}")
            return False
        finally:
            # 清理资源
            self.batch_manager.cleanup()
    
    def _should_resume(self) -> bool:
        """检查是否应该续算"""
        # 检查是否存在结果文件或任务记录
        results_file = os.path.join(self.config.output_dir, "screening_results_complete.csv")
        task_dir = os.path.join(self.config.output_dir, "tasks")
        
        # 如果有结果文件或任务目录存在，则支持续算
        return os.path.exists(results_file) or (os.path.exists(task_dir) and os.listdir(task_dir))
    
    def _get_completed_smiles(self) -> set:
        """获取已完成计算的SMILES集合，支持从多个源加载"""
        completed_smiles = set()
        
        # 1. 从结果文件中加载已完成的SMILES
        results_file = os.path.join(self.config.output_dir, "screening_results_complete.csv")
        if os.path.exists(results_file):
            try:
                df = pd.read_csv(results_file)
                if 'sequence' in df.columns:
                    for sequence in df['sequence'].dropna():
                        if sequence.strip():  # 确保不是空字符串
                            completed_smiles.add(sequence.strip())
                    logger.info(f"从结果文件加载了 {len(completed_smiles)} 个已完成的SMILES")
            except Exception as e:
                logger.warning(f"读取结果文件失败: {e}")
        
        # 2. 从任务记录中加载已完成的SMILES
        task_dir = os.path.join(self.config.output_dir, "tasks")
        if os.path.exists(task_dir):
            task_smiles = 0
            for task_file in os.listdir(task_dir):
                if task_file.endswith('.json'):
                    try:
                        task_path = os.path.join(task_dir, task_file)
                        with open(task_path, 'r') as f:
                            task_record = json.load(f)
                        
                        # 检查任务是否成功完成
                        if task_record.get('status') == 'completed':
                            sequence = task_record.get('sequence', '').strip()
                            if sequence:
                                completed_smiles.add(sequence)
                                task_smiles += 1
                                
                    except Exception as e:
                        logger.warning(f"读取任务记录 {task_file} 失败: {e}")
            
            if task_smiles > 0:
                logger.info(f"从任务记录加载了额外 {task_smiles} 个已完成的SMILES")
        
        logger.info(f"总计加载了 {len(completed_smiles)} 个已完成的SMILES用于续算")
        return completed_smiles
    
    def _resume_screening(self) -> bool:
        """智能续算筛选"""
        try:
            # 1. 加载分子库
            library = self._load_molecule_library()
            if not library:
                return False
            
            # 2. 获取已完成的SMILES集合（智能加载）
            completed_smiles = self._get_completed_smiles()
            
            # 3. 加载已有结果到内存中
            existing_results = self._load_existing_results()
            if existing_results:
                self.screening_results = existing_results
                logger.info(f"加载了 {len(existing_results)} 个已有结果")
            
            # 4. 首先应用max_molecules限制到原始分子列表
            molecules_to_process = library.molecules
            if self.config.max_molecules > 0:
                molecules_to_process = molecules_to_process[:self.config.max_molecules]
                logger.info(f"应用max_molecules限制: {len(library.molecules)} -> {len(molecules_to_process)}")
            
            # 5. 筛选未完成的分子（基于SMILES比较）
            remaining_molecules = []
            skipped_count = 0
            
            for mol in molecules_to_process:
                mol_smiles = mol.sequence.strip() if mol.sequence else ""
                if mol_smiles and mol_smiles in completed_smiles:
                    skipped_count += 1
                    continue
                remaining_molecules.append(mol)
            
            logger.info(f"续算分析: 限制后分子数 {len(molecules_to_process)}, 已完成 {skipped_count}, 待处理 {len(remaining_molecules)}")
            
            # 6. 如果没有待处理的分子，直接处理现有结果
            if not remaining_molecules:
                logger.info("所有分子都已完成计算")
                if self.screening_results:
                    self._process_and_save_results()
                    return True
                else:
                    logger.warning("没有找到有效的筛选结果")
                    return False
            
            # 6. 处理剩余分子
            logger.info(f"继续处理剩余的 {len(remaining_molecules)} 个分子...")
            return self._process_molecules(remaining_molecules)
            
        except Exception as e:
            logger.error(f"智能续算失败: {e}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
            return False
    
    def _run_new_screening(self) -> bool:
        """运行新的筛选"""
        try:
            # 1. 加载分子库
            library = self._load_molecule_library()
            if not library:
                return False
            
            # 2. 预处理分子
            molecules = self._preprocess_molecules(library)
            if not molecules:
                return False
            
            # 3. 处理分子
            return self._process_molecules(molecules)
            
        except Exception as e:
            logger.error(f"新筛选失败: {e}")
            return False
    
    def _load_molecule_library(self) -> Optional[MoleculeLibrary]:
        """加载分子库"""
        try:
            library = LibraryProcessor.create_library(self.config.library_path, self.config.library_type)
            success = library.load_library()
            if not success:
                logger.error("分子库加载失败")
                return None
            logger.info(f"成功加载{library.mol_type}库: {len(library.molecules)} 个分子")
            return library
        except Exception as e:
            logger.error(f"加载分子库失败: {e}")
            return None
    
    def _load_existing_results(self) -> List[ScreeningResult]:
        """加载已有结果"""
        results_file = os.path.join(self.config.output_dir, "screening_results_complete.csv")
        if not os.path.exists(results_file):
            return []
        
        try:
            df = pd.read_csv(results_file)
            results = []
            
            for _, row in df.iterrows():
                result = ScreeningResult(
                    molecule_id=row.get('molecule_id', ''),
                    molecule_name=row.get('molecule_name', ''),
                    sequence=row.get('sequence', ''),
                    mol_type=row.get('mol_type', ''),
                    binding_score=float(row.get('binding_score', 0.0)),
                    confidence_score=float(row.get('confidence_score', 0.0)),
                    structural_score=float(row.get('structural_score', 0.0)),
                    combined_score=float(row.get('combined_score', 0.0)),
                    rank=int(row.get('rank', 0)),
                    structure_path=row.get('structure_path', ''),
                    properties={}
                )
                results.append(result)
            
            logger.info(f"加载了 {len(results)} 个已有结果")
            return results
            
        except Exception as e:
            logger.error(f"加载已有结果失败: {e}")
            return []
    
    def _preprocess_molecules(self, library: MoleculeLibrary) -> List[Molecule]:
        """预处理分子"""
        molecules = library.molecules
        
        # 限制分子数量
        if self.config.max_molecules > 0:
            molecules = molecules[:self.config.max_molecules]
            logger.info(f"限制筛选分子数量为: {self.config.max_molecules}")
        
        logger.info(f"预处理后有效分子数量: {len(molecules)}")
        
        # 检测分子类型并记录
        if molecules:
            sample_mol = molecules[0]
            logger.info(f"✓ 已检测到 {len(molecules)} 个{sample_mol.mol_type}，亲和力计算已{'启用' if self.affinity_enhancer else '未启用'}")
        
        return molecules
    
    def _process_molecules(self, molecules: List[Molecule]) -> bool:
        """处理分子列表"""
        try:
            # 1. 加载目标配置
            target_config = self._load_target_config()
            if not target_config:
                return False
            
            # 2. 准备分子配置
            config_files = self.batch_manager.prepare_molecule_configs(molecules, target_config)
            if not config_files:
                logger.error("准备分子配置失败")
                return False
            
            # 3. 提交并监控分子
            job_results = self.batch_manager.submit_and_monitor_molecules(config_files, molecules)
            
            # 4. 收集结果
            success = self._collect_results(job_results, molecules)
            if not success:
                logger.error("收集结果失败")
                return False
            
            # 5. 处理和保存结果
            self._process_and_save_results()
            
            # 标记可以清理临时文件
            self.batch_manager._cleanup_enabled = True
            
            return True
            
        except Exception as e:
            logger.error(f"处理分子失败: {e}")
            return False
    
    def _load_target_config(self) -> Optional[Dict]:
        """加载目标配置"""
        try:
            with open(self.config.target_yaml, 'r') as f:
                config = yaml.safe_load(f)
            logger.info(f"成功加载目标配置: {self.config.target_yaml}")
            return config
        except Exception as e:
            logger.error(f"加载目标配置失败: {e}")
            return None
    
    def _collect_results(self, job_results: Dict[str, Dict], molecules: List[Molecule]) -> bool:
        """收集和处理结果（现在只需要统计，因为结果已经实时处理了）"""
        try:
            successful_count = 0
            failed_count = 0
            
            for task_id, result in job_results.items():
                status = result.get('status', 'unknown')
                
                if status == 'completed':
                    successful_count += 1
                else:
                    failed_count += 1
                    logger.warning(f"任务 {task_id} 失败: {status}")
            
            logger.info(f"结果收集完成: 成功 {successful_count}, 失败 {failed_count}")
            return successful_count > 0
            
        except Exception as e:
            logger.error(f"收集结果时发生错误: {e}")
            return False
    
    def _find_molecule_for_task(self, task_id: str, molecules: List[Molecule]) -> Optional[Molecule]:
        """根据任务ID找到对应的分子"""
        # 从任务记录中查找
        record_file = os.path.join(self.batch_manager.task_dir, f"task_{task_id}.json")
        if os.path.exists(record_file):
            try:
                with open(record_file, 'r') as f:
                    record = json.load(f)
                molecule_id = record.get('molecule_id', '')
                
                # 在分子列表中查找
                for molecule in molecules:
                    if molecule.id == molecule_id:
                        return molecule
            except Exception as e:
                logger.warning(f"读取任务记录失败: {e}")
        
        # 如果找不到，返回None
        return None
    
    def _process_and_save_results(self):
        """处理和保存最终结果"""
        try:
            if not self.screening_results:
                logger.warning("没有可保存的筛选结果")
                return
            
            # 移除多余的亲和力增强步骤，因为第一次结构预测就包含了亲和力数据
            logger.info("亲和力数据已在结构预测阶段计算完成，跳过二次计算")
            
            # 按综合评分排序
            self.screening_results.sort(key=lambda x: x.combined_score, reverse=True)
            
            # 分配排名
            for i, result in enumerate(self.screening_results):
                result.rank = i + 1
            
            # 保存完整结果
            self._save_complete_results()
            
            # 保存Top结果
            self._save_top_results()
            
            # 生成摘要
            self._save_summary()
            
            # 生成HTML报告
            if self.config.generate_plots:
                self._generate_html_report()
            
            logger.info(f"结果处理完成，共 {len(self.screening_results)} 个结果")
            
        except Exception as e:
            logger.error(f"处理和保存结果失败: {e}")
    
    def _save_complete_results(self):
        """保存完整结果"""
        try:
            results_data = []
            for result in self.screening_results:
                row = {
                    'rank': result.rank,
                    'molecule_id': result.molecule_id,
                    'molecule_name': result.molecule_name,
                    'sequence': result.sequence,
                    'mol_type': result.mol_type,
                    'combined_score': result.combined_score,
                    'binding_score': result.binding_score,
                    'structural_score': result.structural_score,
                    'confidence_score': result.confidence_score,
                    'structure_path': result.structure_path
                }
                
                # 添加属性信息
                if result.properties:
                    row.update(result.properties)
                
                results_data.append(row)
            
            df = pd.DataFrame(results_data)
            results_file = os.path.join(self.config.output_dir, "screening_results_complete.csv")
            df.to_csv(results_file, index=False, encoding='utf-8')
            
            logger.info(f"完整结果已保存: {results_file}")
            
        except Exception as e:
            logger.error(f"保存完整结果失败: {e}")
    
    def _save_top_results(self):
        """保存Top结果"""
        try:
            top_results = self.screening_results[:self.config.top_n]
            
            results_data = []
            for result in top_results:
                row = {
                    'rank': result.rank,
                    'molecule_id': result.molecule_id,
                    'molecule_name': result.molecule_name,
                    'sequence': result.sequence,
                    'mol_type': result.mol_type,
                    'combined_score': result.combined_score,
                    'binding_score': result.binding_score,
                    'structural_score': result.structural_score,
                    'confidence_score': result.confidence_score,
                    'structure_path': result.structure_path
                }
                results_data.append(row)
            
            df = pd.DataFrame(results_data)
            top_file = os.path.join(self.config.output_dir, "top_hits.csv")
            df.to_csv(top_file, index=False, encoding='utf-8')
            
            logger.info(f"Top {len(top_results)} 结果已保存: {top_file}")
            
        except Exception as e:
            logger.error(f"保存Top结果失败: {e}")
    
    def _save_summary(self):
        """保存筛选摘要"""
        try:
            summary = {
                'total_screened': len(self.screening_results),
                'successful_predictions': len(self.screening_results),
                'failed_predictions': len(self.failed_molecules),
                'success_rate': len(self.screening_results) / (len(self.screening_results) + len(self.failed_molecules)) if (len(self.screening_results) + len(self.failed_molecules)) > 0 else 0,
                'top_score': self.screening_results[0].combined_score if self.screening_results else 0.0,
                'average_score': np.mean([r.combined_score for r in self.screening_results]) if self.screening_results else 0.0,
                'screening_config': asdict(self.config),
                'timestamp': time.time()
            }
            
            summary_file = os.path.join(self.config.output_dir, "screening_summary.json")
            with open(summary_file, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            
            logger.info(f"筛选摘要已保存: {summary_file}")
            
        except Exception as e:
            logger.error(f"保存筛选摘要失败: {e}")
    
    def _generate_html_report(self):
        """生成HTML报告"""
        try:
            logger.info("开始生成HTML报告...")
            
            # 创建HTML报告生成器
            reporter = HTMLReporter(
                screening_results=self.screening_results,
                output_dir=self.config.output_dir
            )
            
            # 先生成图表
            plots = reporter.generate_screening_plots()
            logger.info(f"生成了 {len(plots)} 个图表")
            
            # 再生成HTML报告
            report_path = reporter.generate_html_report(plots=plots)
            
            if report_path and os.path.exists(report_path):
                logger.info(f"HTML报告已生成: {report_path}")
            else:
                logger.warning("HTML报告生成失败，但图表已生成")
                
        except Exception as e:
            logger.error(f"生成HTML报告失败: {e}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
    
    def get_screening_summary(self) -> Dict[str, Any]:
        """获取筛选摘要"""
        return {
            'total_screened': len(self.screening_results),
            'successful_predictions': len(self.screening_results),
            'failed_predictions': len(self.failed_molecules),
            'success_rate': len(self.screening_results) / (len(self.screening_results) + len(self.failed_molecules)) if (len(self.screening_results) + len(self.failed_molecules)) > 0 else 0,
            'top_score': self.screening_results[0].combined_score if self.screening_results else 0.0
        }
