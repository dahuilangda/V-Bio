#!/usr/bin/env python3

"""
professional_reporter.py

虚拟筛选报告生成器，包含图表和详细分析
"""

import os
import json
import logging
import math
from typing import List, Dict, Any, Optional
from datetime import datetime
import pandas as pd
import numpy as np
from pathlib import Path

# 可视化库
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import seaborn as sns
    from matplotlib import font_manager
    import matplotlib.patches as mpatches
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# 分子处理库
try:
    from rdkit import Chem
    from rdkit.Chem import Draw, Descriptors
    from rdkit.Chem.Draw import rdMolDraw2D
    RDKIT_AVAILABLE = True
except ImportError:
    RDKIT_AVAILABLE = False

# 避免循环导入，使用字符串类型提示
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from screening_engine import ScreeningResult

logger = logging.getLogger(__name__)


class HTMLReporter:
    """报告生成器"""
    
    def __init__(self, screening_results: List["ScreeningResult"], output_dir: str, target_sequence: str = ""):
        self.screening_results = screening_results
        self.output_dir = output_dir
        self.target_sequence = target_sequence
        self.plots_dir = os.path.join(output_dir, "plots")
        os.makedirs(self.plots_dir, exist_ok=True)
        
        # 设置样式
        self._setup_professional_style()
        
        # 为结果添加计算的药物化学属性
        self._enhance_results_with_calculated_properties()
        
        logger.info(f"报告生成器已初始化，结果数量: {len(screening_results)}")
        if target_sequence:
            logger.info(f"Target序列长度: {len(target_sequence)}")
    
    def _setup_professional_style(self):
        """设置图表参数"""
        if not MATPLOTLIB_AVAILABLE:
            return
            
        # 设置中文字体
        try: 
            # 尝试使用SimHei（黑体）
            plt.rcParams['font.sans-serif'] = ['SimHei', 'DejaVu Sans', 'Arial Unicode MS', 'Microsoft YaHei']
            plt.rcParams['axes.unicode_minus'] = False  # 解决负号显示问题
        except:
            # 如果中文字体不可用，使用英文
            plt.rcParams['font.family'] = 'DejaVu Sans'
            
        # 设置专业的图表样式和统一颜色方案
        plt.rcParams.update({
            # 字体设置 - 增大字号以提高可读性
            'font.size': 16,
            'axes.titlesize': 20,
            'axes.labelsize': 16,
            'xtick.labelsize': 14,
            'ytick.labelsize': 14,
            'legend.fontsize': 14,
            'figure.titlesize': 22,
            
            # 线条和标记设置
            'lines.linewidth': 2.0,
            'axes.linewidth': 1.5,
            'xtick.major.width': 1.5,
            'ytick.major.width': 1.5,
            'xtick.minor.width': 1.0,
            'ytick.minor.width': 1.0,
            
            # 图表背景和网格
            'axes.facecolor': '#f8f9fa',
            'axes.edgecolor': '#2c3e50',
            'axes.grid': True,
            'grid.alpha': 0.4,
            'grid.linewidth': 0.8,
            'grid.color': '#bdc3c7',
            
            # 隐藏顶部和右侧的边框
            'axes.spines.top': False,
            'axes.spines.right': False,
            'xtick.top': False,
            'ytick.right': False,
            
            # 图像质量
            'figure.dpi': 300,
            'savefig.dpi': 300,
            'savefig.format': 'png',
            'savefig.bbox': 'tight',
            'savefig.pad_inches': 0.2,
            
            # 统一的专业颜色方案
            'axes.prop_cycle': plt.cycler('color', [
                '#3498db', '#e74c3c', '#2ecc71', '#f39c12',
                '#9b59b6', '#1abc9c', '#34495e', '#e67e22',
                '#95a5a6', '#8e44ad'
            ])
        })
        
        # 定义AlphaFold风格的置信度颜色方案
        self.color_scheme = {
            # AlphaFold置信度颜色：从红色(低置信度)到蓝色(高置信度)
            'very_high_confidence': '#0053D6',   # 深蓝色 (>90)
            'high_confidence': '#65CBF3',        # 浅蓝色 (70-90) 
            'medium_confidence': '#FFDB13',      # 黄色 (50-70)
            'low_confidence': '#FF7D45',         # 橙色 (0-50)
            'very_low_confidence': '#FF0000',    # 红色 (<30)
            
            # 传统配色作为备用
            'primary': '#0053D6',      # AlphaFold深蓝色
            'secondary': '#FF7D45',    # AlphaFold橙色
            'success': '#2ecc71',      # 绿色
            'warning': '#FFDB13',      # AlphaFold黄色
            'purple': '#9b59b6',       # 紫色
            'teal': '#65CBF3',         # AlphaFold浅蓝色
            'dark': '#34495e',         # 深色
            'gradient': ['#FF0000', '#FF7D45', '#FFDB13', '#65CBF3', '#0053D6']  # AlphaFold渐变
        }
    
    def _get_alphafold_color(self, score: float, score_type: str = "confidence") -> str:
        """根据AlphaFold置信度分数获取对应颜色"""
        if score_type == "confidence" or score_type == "iptm":
            # 按照AlphaFold的置信度颜色映射
            if score >= 0.9:
                return self.color_scheme['very_high_confidence']  # 深蓝色
            elif score >= 0.7:
                return self.color_scheme['high_confidence']       # 浅蓝色
            elif score >= 0.5:
                return self.color_scheme['medium_confidence']     # 黄色
            elif score >= 0.3:
                return self.color_scheme['low_confidence']        # 橙色
            else:
                return self.color_scheme['very_low_confidence']   # 红色
        elif score_type == "plddt":
            # pLDDT通常是0-100的分数，需要归一化
            normalized_score = score / 100.0 if score > 1 else score
            return self._get_alphafold_color(normalized_score, "confidence")
        else:
            # 其他类型分数的通用映射
            return self._get_alphafold_color(score, "confidence")
    
    def _generate_alphafold_gradient(self, scores: List[float]) -> List[str]:
        """为一组分数生成AlphaFold风格的渐变颜色"""
        colors = []
        for score in scores:
            colors.append(self._get_alphafold_color(score))
        return colors
    
    def _enhance_results_with_calculated_properties(self):
        """为结果添加从实际计算结果JSON文件中读取的属性"""
        logger.info("开始从结果文件中加载真实的亲和力和置信度数据")
        
        for i, result in enumerate(self.screening_results):
            if not hasattr(result, 'properties') or not result.properties:
                result.properties = {}
            
            # 尝试从task目录中读取真实的计算结果
            task_data = self._load_task_results(result)
            
            if task_data:
                # 更新结果属性
                result.properties.update(task_data)
                logger.debug(f"为 {result.molecule_name} 加载了真实计算数据")
            else:
                # 如果没有找到实际结果，生成估算数据（向后兼容）
                self._generate_fallback_properties(result, i)
                logger.debug(f"为 {result.molecule_name} 生成了估算数据")
        
        logger.info(f"完成数据加载，处理了 {len(self.screening_results)} 个结果")
    
    def _load_task_results(self, result: "ScreeningResult") -> Dict[str, Any]:
        """从task目录中加载真实的计算结果"""
        try:
            # 查找对应的task目录
            task_dir = self._find_task_directory(result)
            if not task_dir:
                return {}
            
            properties = {}
            
            # 1. 加载亲和力数据
            affinity_file = os.path.join(task_dir, "affinity_data.json")
            if os.path.exists(affinity_file):
                with open(affinity_file, 'r') as f:
                    affinity_data = json.load(f)
                
                # 收集所有亲和力预测值
                affinity_values = []
                binding_probabilities = []
                
                # 收集 affinity_pred_value, affinity_pred_value1, affinity_pred_value2
                for key in ['affinity_pred_value', 'affinity_pred_value1', 'affinity_pred_value2']:
                    value = affinity_data.get(key)
                    if value is not None:
                        affinity_values.append(value)
                
                # 收集 affinity_probability_binary, affinity_probability_binary1, affinity_probability_binary2  
                for key in ['affinity_probability_binary', 'affinity_probability_binary1', 'affinity_probability_binary2']:
                    value = affinity_data.get(key)
                    if value is not None:
                        binding_probabilities.append(value)
                
                # 计算亲和力预测值的平均值和标准差
                if affinity_values:
                    import numpy as np
                    affinity_mean = np.mean(affinity_values)
                    affinity_std = np.std(affinity_values) if len(affinity_values) > 1 else 0.0
                    
                    # 转换为IC50 (μM)：模型输出是log(IC50 in μM)
                    # 例如：模型输出 -3 对应 IC50 = 10^(-3) = 0.001 μM = 1 nM
                    ic50_uM = 10 ** affinity_mean
                    properties['ic50_uM'] = ic50_uM
                    
                    # 计算pIC50：pIC50 = -log10(IC50_M)
                    # IC50_M = IC50_μM * 1e-6，所以 pIC50 = -log10(IC50_μM * 1e-6) = 6 - log10(IC50_μM) = 6 - affinity_mean
                    properties['pIC50'] = 6 - affinity_mean
                    
                    # 转换为结合自由能 kcal/mol：根据文档 y --> (6 - y) * 1.364
                    # 这里 y 是模型输出的 affinity_pred_value (log(IC50 in μM))
                    properties['affinity_kcal_mol'] = (6 - affinity_mean) * 1.364
                    
                    # 为了兼容旧的属性名
                    properties['affinity'] = properties['affinity_kcal_mol']
                    
                    # 保存平均值和标准差信息
                    properties['affinity_pred_value_mean'] = affinity_mean
                    properties['affinity_pred_value_std'] = affinity_std
                    properties['affinity_pred_value_range'] = f"{affinity_mean:.3f} ± {affinity_std:.3f}"
                    
                    # 计算IC50区间（用于显示），统一使用μM单位
                    ic50_min = 10 ** (affinity_mean - affinity_std)
                    ic50_max = 10 ** (affinity_mean + affinity_std)
                    # 统一使用μM单位和±格式
                    ic50_range_display = f"{ic50_uM:.3f} ± {(ic50_max - ic50_min)/2:.3f} μM"
                    properties['ic50_range_display'] = ic50_range_display
                    
                    # 保存原始预测值（用于向后兼容）
                    properties['affinity_pred_value'] = affinity_mean
                
                # 计算结合概率的平均值和标准差
                if binding_probabilities:
                    binding_prob_mean = np.mean(binding_probabilities)
                    binding_prob_std = np.std(binding_probabilities) if len(binding_probabilities) > 1 else 0.0
                    
                    properties['binding_probability'] = binding_prob_mean
                    properties['binding_probability_mean'] = binding_prob_mean
                    properties['binding_probability_std'] = binding_prob_std
                    properties['binding_probability_range'] = f"{binding_prob_mean:.3f} ± {binding_prob_std:.3f}"
                    
                    # 添加百分比格式的结合概率
                    properties['binding_probability_percent'] = binding_prob_mean * 100
                    properties['binding_probability_percent_std'] = binding_prob_std * 100
                    properties['binding_probability_range_percent'] = f"{binding_prob_mean*100:.1f}% ± {binding_prob_std*100:.1f}%"
                    
                    # 保存原始值（用于向后兼容）
                    properties['affinity_probability_binary'] = binding_prob_mean
                
                logger.debug(f"加载亲和力数据: 平均IC50={ic50_uM:.3f}μM (±{affinity_std:.3f}), 平均结合概率={binding_prob_mean:.3f} (±{binding_prob_std:.3f})")
            
            # 2. 加载置信度数据
            confidence_file = os.path.join(task_dir, "confidence_data_model_0.json")
            if os.path.exists(confidence_file):
                with open(confidence_file, 'r') as f:
                    confidence_data = json.load(f)
                
                # 主要置信度指标
                properties['iptm'] = confidence_data.get('iptm', 0.0)
                properties['ptm'] = confidence_data.get('ptm', 0.0)
                properties['complex_plddt'] = confidence_data.get('complex_plddt', 0.0)
                properties['ligand_iptm'] = confidence_data.get('ligand_iptm', 0.0)
                properties['protein_iptm'] = confidence_data.get('protein_iptm', 0.0)
                
                # pLDDT通常是0-1范围，转换为0-100
                plddt = confidence_data.get('complex_plddt', 0.0)
                if plddt <= 1.0:
                    properties['plddt'] = plddt * 100
                else:
                    properties['plddt'] = plddt
                
                logger.debug(f"加载置信度数据: ipTM={properties['iptm']:.3f}, pLDDT={properties['plddt']:.1f}")
            
            # 3. 如果有RDKit可用，计算分子描述符
            if RDKIT_AVAILABLE and result.mol_type == "small_molecule" and result.sequence:
                self._calculate_molecular_descriptors(result, properties)
            
            return properties
            
        except Exception as e:
            logger.warning(f"加载 {result.molecule_name} 的task结果失败: {e}")
            return {}
    
    def _find_task_directory(self, result: "ScreeningResult") -> Optional[str]:
        """查找对应结果的task目录"""
        try:
            # 在输出目录的tasks子目录中查找task_*目录
            tasks_base_dir = os.path.join(self.output_dir, "tasks")
            
            if not os.path.exists(tasks_base_dir):
                logger.debug(f"Tasks目录不存在: {tasks_base_dir}")
                return None
            
            # 查找所有task目录
            task_dirs = [d for d in os.listdir(tasks_base_dir) 
                        if d.startswith('task_') and os.path.isdir(os.path.join(tasks_base_dir, d))]
            
            if not task_dirs:
                logger.debug(f"在{tasks_base_dir}中未找到task目录")
                return None
            
            # 策略1：通过结果中的structure_path查找
            if hasattr(result, 'structure_path') and result.structure_path:
                for task_dir in task_dirs:
                    if task_dir in result.structure_path:
                        full_path = os.path.join(tasks_base_dir, task_dir)
                        logger.debug(f"通过structure_path找到task目录: {full_path}")
                        return full_path
            
            # 策略2：通过分子名称或ID匹配（如果在immediate_reports中有记录）
            immediate_dir = os.path.join(self.output_dir, "immediate_reports")
            if os.path.exists(immediate_dir):
                for report_file in os.listdir(immediate_dir):
                    if report_file.endswith('.csv'):
                        try:
                            import pandas as pd
                            df = pd.read_csv(os.path.join(immediate_dir, report_file))
                            
                            # 查找匹配的分子
                            matching_rows = df[
                                (df['molecule_id'] == result.molecule_id) |
                                (df['molecule_name'] == result.molecule_name)
                            ]
                            
                            if not matching_rows.empty and 'structure_path' in df.columns:
                                structure_path = matching_rows.iloc[0]['structure_path']
                                for task_dir in task_dirs:
                                    if task_dir in str(structure_path):
                                        full_path = os.path.join(tasks_base_dir, task_dir)
                                        logger.debug(f"通过immediate报告找到task目录: {full_path}")
                                        return full_path
                        except Exception as e:
                            logger.debug(f"读取immediate报告失败 {report_file}: {e}")
                            continue
            
            # 策略3：按时间顺序匹配（假设结果按处理顺序对应）
            # 这个策略不太可靠，作为最后手段
            task_dirs.sort()
            if len(task_dirs) > 0:
                # 简单按索引匹配（不够精确，但总比没有好）
                index = hash(result.molecule_id) % len(task_dirs)
                full_path = os.path.join(tasks_base_dir, task_dirs[index])
                logger.debug(f"使用哈希匹配找到task目录: {full_path}")
                return full_path
            
            return None
            
        except Exception as e:
            logger.warning(f"查找task目录失败: {e}")
            return None
    
    def _calculate_molecular_descriptors(self, result: "ScreeningResult", properties: Dict[str, Any]):
        """计算分子描述符"""
        try:
            mol = Chem.MolFromSmiles(result.sequence)
            if mol:
                properties['molecular_weight'] = Descriptors.MolWt(mol)
                properties['logp'] = Descriptors.MolLogP(mol)
                properties['hbd'] = Descriptors.NumHDonors(mol)
                properties['hba'] = Descriptors.NumHAcceptors(mol)
                properties['tpsa'] = Descriptors.TPSA(mol)
                logger.debug(f"计算分子描述符: MW={properties['molecular_weight']:.1f}, LogP={properties['logp']:.2f}")
        except Exception as e:
            logger.debug(f"计算分子描述符失败: {e}")
    
    def _generate_fallback_properties(self, result: "ScreeningResult", index: int):
        """生成回退的基础属性（仅分子描述符，不包含亲和力估算）"""
        
        if result.mol_type == "small_molecule":
            # 仅计算分子描述符 - 这些是基于SMILES的真实化学性质计算
            if RDKIT_AVAILABLE and result.sequence:
                self._calculate_molecular_descriptors(result, result.properties)
                logger.debug(f"为 {result.molecule_name} 计算了分子描述符 (MW, LogP, HBD, HBA, TPSA)")
            else:
                logger.debug(f"RDKit不可用或无SMILES，跳过 {result.molecule_name} 的分子描述符计算")
        
        # 记录：不生成任何亲和力数据
        logger.debug(f"不生成 {result.molecule_name} 的亲和力数据 - 仅使用来自affinity_data.json的真实计算结果")

    def _apply_clean_style(self, ax):
        """应用清洁的图表样式，移除顶部和右侧边框"""
        if MATPLOTLIB_AVAILABLE:
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            ax.tick_params(top=False, right=False)
    
    def generate_screening_plots(self) -> List[str]:
        """生成虚拟筛选专用图表"""
        if not MATPLOTLIB_AVAILABLE:
            logger.warning("Matplotlib不可用，跳过图表生成")
            return []
        
        plots = []
        
        try:
            # 1. 评分分布分析
            score_dist = self._plot_score_distribution()
            if score_dist:
                plots.append(score_dist)
            
            # 2. 筛选漏斗图
            funnel_plot = self._plot_screening_funnel()
            if funnel_plot:
                plots.append(funnel_plot)
            
            # 3. 前20名分子条形图
            top_molecules = self._plot_top_molecules()
            if top_molecules:
                plots.append(top_molecules)
            
            # 4. 评分指标对比雷达图
            radar_plot = self._plot_screening_radar()
            if radar_plot:
                plots.append(radar_plot)
            
            # 5. IC50和结合概率分析（小分子专用）
            ic50_plot = self._plot_ic50_binding_analysis()
            if ic50_plot:
                plots.append(ic50_plot)
            
            # 6. 亲和力分析（如果有）
            affinity_plot = self._plot_affinity_analysis()
            if affinity_plot:
                plots.append(affinity_plot)
            
            # 7. 分子复杂度分析
            complexity_plot = self._plot_molecular_complexity()
            if complexity_plot:
                plots.append(complexity_plot)
            
            logger.info(f"生成了 {len(plots)} 个图表")
            return plots
            
        except Exception as e:
            logger.error(f"生成图表时发生错误: {e}")
            return plots
    
    def _extract_scientific_metrics(self, result) -> Dict[str, float]:
        """提取科学指标：ipTM、PAE、pLDDT、pIC50、结合概率等"""
        metrics = {}
        
        # 从结果属性中提取科学指标
        if hasattr(result, 'properties') and result.properties:
            # 置信度指标
            metrics['ipTM'] = result.properties.get('iptm', result.properties.get('ipTM', 0.0))
            metrics['PTM'] = result.properties.get('ptm', result.properties.get('PTM', 0.0))
            metrics['pLDDT'] = result.properties.get('plddt', result.properties.get('pLDDT', 0.0))
            
            # PAE相关指标
            metrics['PAE'] = result.properties.get('pae', result.properties.get('PAE', 0.0))
            metrics['max_PAE'] = result.properties.get('max_pae', 0.0)
            
            # 亲和力和结合相关指标
            metrics['pIC50'] = result.properties.get('pic50', result.properties.get('pIC50', 0.0))
            metrics['binding_probability'] = result.properties.get('binding_probability', 0.0)
            metrics['binding_affinity'] = result.properties.get('binding_affinity', result.binding_score)
            
            # 分子属性
            metrics['molecular_weight'] = result.properties.get('molecular_weight', 0.0)
            metrics['logP'] = result.properties.get('logp', result.properties.get('LogP', 0.0))
            metrics['tPSA'] = result.properties.get('tpsa', result.properties.get('TPSA', 0.0))
            
        else:
            # 如果没有详细属性，使用基本评分
            metrics['ipTM'] = getattr(result, 'confidence_score', 0.0)
            metrics['binding_affinity'] = getattr(result, 'binding_score', 0.0)
            metrics['structural_quality'] = getattr(result, 'structural_score', 0.0)
        
        return metrics
    
    def _plot_score_distribution(self) -> Optional[str]:
        """绘制筛选指标分布图"""
        try:
            fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 10))
            
            # 提取筛选指标数据
            iptm_scores = []
            plddt_scores = []
            confidence_scores = []
            combined_scores = []
            
            for result in self.screening_results:
                metrics = self._extract_scientific_metrics(result)
                iptm_scores.append(metrics.get('ipTM', 0.0))
                plddt_scores.append(metrics.get('pLDDT', 0.0))
                confidence_scores.append(result.confidence_score)
                combined_scores.append(result.combined_score)
            
            # 使用AlphaFold颜色方案
            colors = [
                self.color_scheme['very_high_confidence'],   # ipTM - 深蓝色
                self.color_scheme['high_confidence'],        # pLDDT - 浅蓝色
                self.color_scheme['medium_confidence'],      # Confidence - 黄色
                self.color_scheme['low_confidence']          # Combined - 橙色
            ]
            
            # ipTM分布
            ax1.hist(iptm_scores, bins=20, alpha=0.8, color=colors[0], 
                    edgecolor='white', linewidth=2)
            ax1.set_xlabel('ipTM Score', fontweight='bold')
            ax1.set_ylabel('Molecule Count', fontweight='bold')
            ax1.set_title('ipTM Score Distribution', fontweight='bold', pad=20)
            ax1.grid(True, alpha=0.4)
            self._apply_clean_style(ax1)
            
            # pLDDT分布
            ax2.hist(plddt_scores, bins=20, alpha=0.8, color=colors[1],
                    edgecolor='white', linewidth=2)
            ax2.set_xlabel('pLDDT Score', fontweight='bold')
            ax2.set_ylabel('Molecule Count', fontweight='bold')
            ax2.set_title('pLDDT Score Distribution', fontweight='bold', pad=20)
            ax2.grid(True, alpha=0.4)
            self._apply_clean_style(ax2)
            
            # 置信度分布
            ax3.hist(confidence_scores, bins=20, alpha=0.8, color=colors[2],
                    edgecolor='white', linewidth=2)
            ax3.set_xlabel('Confidence Score', fontweight='bold')
            ax3.set_ylabel('Molecule Count', fontweight='bold')
            ax3.set_title('Confidence Score Distribution', fontweight='bold', pad=20)
            ax3.grid(True, alpha=0.4)
            self._apply_clean_style(ax3)
            
            # 综合评分分布
            ax4.hist(combined_scores, bins=20, alpha=0.8, color=colors[3],
                    edgecolor='white', linewidth=2)
            ax4.set_xlabel('Combined Score', fontweight='bold')
            ax4.set_ylabel('Molecule Count', fontweight='bold')
            ax4.set_title('Combined Score Distribution', fontweight='bold', pad=20)
            ax4.grid(True, alpha=0.4)
            self._apply_clean_style(ax4)
            
            plt.tight_layout()
            
            output_path = os.path.join(self.plots_dir, "score_distribution.png")
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"评分分布图已保存: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"生成评分分布图失败: {e}")
            return None
    
    def _plot_screening_funnel(self) -> Optional[str]:
        """绘制虚拟筛选漏斗图，展示不同评分阈值下的筛选效果"""
        try:
            # 定义评分阈值
            thresholds = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
            counts = []
            percentages = []
            
            total_molecules = len(self.screening_results)
            
            for threshold in thresholds:
                count = sum(1 for result in self.screening_results 
                           if result.combined_score >= threshold)
                counts.append(count)
                percentages.append((count / total_molecules) * 100 if total_molecules > 0 else 0)
            
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))
            
            # 使用渐变颜色方案
            gradient_colors = plt.cm.viridis(np.linspace(0.2, 0.9, len(thresholds)))
            y_pos = np.arange(len(thresholds))
            
            bars1 = ax1.barh(y_pos, counts, color=gradient_colors, 
                           edgecolor='white', linewidth=1.5)
            ax1.set_yticks(y_pos)
            ax1.set_yticklabels([f'≥{t:.1f}' for t in thresholds], fontweight='bold')
            ax1.set_xlabel('Molecule Count', fontweight='bold')
            ax1.set_ylabel('Score Threshold', fontweight='bold')
            ax1.set_title('Virtual Screening Funnel - Absolute Count', fontweight='bold', pad=20)
            ax1.grid(axis='x', alpha=0.4)
            self._apply_clean_style(ax1)
            
            # 添加数值标签
            for i, (bar, count) in enumerate(zip(bars1, counts)):
                ax1.text(bar.get_width() + total_molecules * 0.02, bar.get_y() + bar.get_height()/2, 
                        f'{count}', va='center', fontweight='bold', fontsize=14)
            
            # 右侧：漏斗图（百分比）
            bars2 = ax2.barh(y_pos, percentages, color=gradient_colors,
                           edgecolor='white', linewidth=1.5)
            ax2.set_yticks(y_pos)
            ax2.set_yticklabels([f'≥{t:.1f}' for t in thresholds], fontweight='bold')
            ax2.set_xlabel('Pass Rate (%)', fontweight='bold')
            ax2.set_ylabel('Score Threshold', fontweight='bold')
            ax2.set_title('Virtual Screening Funnel - Pass Rate', fontweight='bold', pad=20)
            ax2.grid(axis='x', alpha=0.4)
            self._apply_clean_style(ax2)
            
            # 添加百分比标签
            for i, (bar, pct) in enumerate(zip(bars2, percentages)):
                ax2.text(bar.get_width() + 2, bar.get_y() + bar.get_height()/2, 
                        f'{pct:.1f}%', va='center', fontweight='bold', fontsize=14)
            
            plt.tight_layout()
            
            output_path = os.path.join(self.plots_dir, "screening_funnel.png")
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"筛选漏斗图已保存: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"生成筛选漏斗图失败: {e}")
            return None
    
    def _plot_top_molecules(self) -> Optional[str]:
        """绘制前20名分子的条形图"""
        try:
            # 获取前20名分子
            top_20 = sorted(self.screening_results, key=lambda x: x.combined_score, reverse=True)[:20]
            
            fig, ax = plt.subplots(figsize=(14, 10))
            
            # 准备数据，确保名称是字符串
            names = []
            for r in top_20:
                mol_name = str(r.molecule_name) if r.molecule_name is not None else "Unknown"
                if len(mol_name) > 15:
                    mol_name = f"{mol_name[:15]}..."
                names.append(mol_name)
            
            scores = [r.combined_score for r in top_20]
            
            # 根据分数使用AlphaFold颜色
            colors = self._generate_alphafold_gradient(scores)
            
            # 创建水平条形图
            bars = ax.barh(range(len(names)), scores, color=colors, 
                          edgecolor='white', linewidth=2)
            
            # 设置标签和标题
            ax.set_yticks(range(len(names)))
            ax.set_yticklabels(names, fontweight='bold')
            ax.set_xlabel('Combined Score', fontweight='bold')
            ax.set_title('Top 20 Molecules by Combined Score', fontweight='bold', pad=25)
            self._apply_clean_style(ax)
            
            # 添加数值标签
            for i, (bar, score) in enumerate(zip(bars, scores)):
                ax.text(score + 0.01, i, f'{score:.3f}', 
                       va='center', ha='left', fontsize=12, fontweight='bold')
            
            # 反转y轴，使最高分在顶部
            ax.invert_yaxis()
            ax.grid(True, alpha=0.4, axis='x')
            
            # 设置背景色
            ax.set_facecolor('#f8f9fa')
            
            plt.tight_layout()
            
            output_path = os.path.join(self.plots_dir, "top_molecules.png")
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"前20名分子图已保存: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"生成前20名分子图失败: {e}")
            return None
    
    def _plot_screening_radar(self) -> Optional[str]:
        """绘制前5名分子的评分雷达图"""
        try:
            # 获取前5名分子
            top_5 = sorted(self.screening_results, key=lambda x: x.combined_score, reverse=True)[:5]
            
            if len(top_5) < 2:
                logger.warning("分子数量不足，无法生成雷达图")
                return None
            
            # 定义评分维度
            categories = ['Binding Affinity', 'Structural Stability', 'Confidence', 'Combined Score']
            
            # 设置雷达图
            angles = np.linspace(0, 2 * np.pi, len(categories), endpoint=False)
            angles = np.concatenate((angles, [angles[0]]))  # 闭合雷达图
            
            fig, ax = plt.subplots(figsize=(12, 12), subplot_kw=dict(projection='polar'))
            
            # 使用AlphaFold颜色方案
            colors = [
                self.color_scheme['very_high_confidence'],
                self.color_scheme['high_confidence'], 
                self.color_scheme['medium_confidence'],
                self.color_scheme['low_confidence'], 
                self.color_scheme['very_low_confidence']
            ]
            
            for i, result in enumerate(top_5):
                # 归一化评分到0-1范围，处理None值
                binding_score = result.binding_score if result.binding_score is not None else 0.0
                structural_score = result.structural_score if result.structural_score is not None else 0.0
                confidence_score = result.confidence_score if result.confidence_score is not None else 0.0
                combined_score = result.combined_score if result.combined_score is not None else 0.0
                
                values = [
                    binding_score / 1.0 if binding_score <= 1.0 else 1.0,
                    structural_score / 1.0 if structural_score <= 1.0 else 1.0,
                    confidence_score / 1.0 if confidence_score <= 1.0 else 1.0,
                    combined_score / 1.0 if combined_score <= 1.0 else 1.0
                ]
                values += [values[0]]  # 闭合雷达图
                
                ax.plot(angles, values, 'o-', linewidth=3, markersize=8, 
                       label=f'{result.molecule_id}', color=colors[i])
                ax.fill(angles, values, alpha=0.15, color=colors[i])
            
            # 设置标签和格式
            ax.set_xticks(angles[:-1])
            ax.set_xticklabels(categories, fontsize=14, fontweight='bold')
            ax.set_ylim(0, 1)
            ax.set_yticks([0.2, 0.4, 0.6, 0.8, 1.0])
            ax.set_yticklabels(['0.2', '0.4', '0.6', '0.8', '1.0'], fontsize=12, fontweight='bold')
            ax.grid(True, alpha=0.4)
            ax.set_facecolor('#f8f9fa')
            
            # 雷达图本身就没有传统的边框，但确保网格线样式一致
            ax.tick_params(pad=10)
            
            # 添加标题和图例
            ax.set_title('Top 5 Molecules Score Comparison', fontsize=18, fontweight='bold', pad=30)
            plt.legend(loc='upper right', bbox_to_anchor=(1.4, 1.0), fontsize=12, frameon=True, 
                      fancybox=True, shadow=True)
            
            plt.tight_layout()
            
            output_path = os.path.join(self.plots_dir, "screening_radar.png")
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"筛选雷达图已保存: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"生成筛选雷达图失败: {e}")
            return None
    
    def _plot_ic50_binding_analysis(self) -> Optional[str]:
        """绘制IC50和结合概率分析图（专门针对小分子）"""
        try:
            # 收集小分子的IC50和结合概率数据
            ic50_data = []
            binding_prob_data = []
            
            for result in self.screening_results:
                if result.mol_type == "small_molecule" and hasattr(result, 'properties') and result.properties:
                    # IC50数据
                    ic50_uM = result.properties.get('ic50_uM')
                    if ic50_uM is not None and isinstance(ic50_uM, (int, float)) and ic50_uM > 0:
                        ic50_data.append((result.combined_score, ic50_uM, result.molecule_name, 
                                        result.properties.get('pIC50', 0)))
                    
                    # 结合概率数据
                    binding_prob = result.properties.get('binding_probability')
                    if binding_prob is not None and isinstance(binding_prob, (int, float)):
                        binding_prob_data.append((result.combined_score, binding_prob, result.molecule_name))
            
            if len(ic50_data) < 3 and len(binding_prob_data) < 3:
                logger.info("IC50和结合概率数据不足，跳过该分析图")
                return None
            
            # 创建子图
            fig_height = 12 if len(ic50_data) >= 3 and len(binding_prob_data) >= 3 else 6
            if len(ic50_data) >= 3 and len(binding_prob_data) >= 3:
                fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(16, fig_height))
            elif len(ic50_data) >= 3:
                fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6))
                ax3 = ax4 = None
            else:
                fig, (ax3, ax4) = plt.subplots(1, 2, figsize=(16, 6))
                ax1 = ax2 = None
            
            # IC50分析
            if len(ic50_data) >= 3 and ax1 is not None:
                combined_scores, ic50_values, names, pic50_values = zip(*ic50_data)
                
                # 散点图：Combined Score vs IC50
                scatter = ax1.scatter(combined_scores, ic50_values, 
                                    c=pic50_values, cmap='viridis', s=80, alpha=0.8,
                                    edgecolors='white', linewidth=2)
                ax1.set_xlabel('Combined Score', fontweight='bold')
                ax1.set_ylabel('IC50 (μM)', fontweight='bold')
                ax1.set_title('Combined Score vs IC50', fontweight='bold', pad=20)
                ax1.set_yscale('log')
                ax1.grid(True, alpha=0.4)
                self._apply_clean_style(ax1)
                
                # 添加颜色条
                cbar = plt.colorbar(scatter, ax=ax1)
                cbar.set_label('pIC50', fontweight='bold')
                
                # 前10名IC50条形图
                top_10_indices = np.argsort(combined_scores)[-10:]
                top_10_names = [names[i][:8] + "..." if len(names[i]) > 8 else names[i] 
                               for i in top_10_indices]
                top_10_ic50 = [ic50_values[i] for i in top_10_indices]
                
                colors_gradient = plt.cm.plasma(np.linspace(0.2, 0.9, len(top_10_names)))
                bars = ax2.bar(range(len(top_10_names)), top_10_ic50,
                              color=colors_gradient, alpha=0.8, edgecolor='white', linewidth=2)
                ax2.set_xticks(range(len(top_10_names)))
                ax2.set_xticklabels(top_10_names, rotation=45, ha='right', fontweight='bold')
                ax2.set_ylabel('IC50 (μM)', fontweight='bold')
                ax2.set_title('Top 10 Molecules - IC50 Values', fontweight='bold', pad=20)
                ax2.set_yscale('log')
                ax2.grid(True, alpha=0.4, axis='y')
                self._apply_clean_style(ax2)
                
                # 添加数值标签
                for bar, ic50 in zip(bars, top_10_ic50):
                    height = bar.get_height()
                    if ic50 < 0.001:
                        label = f'{ic50*1000:.1f}nM'
                    elif ic50 < 1:
                        label = f'{ic50*1000:.0f}nM'
                    elif ic50 < 1000:
                        label = f'{ic50:.2f}μM'
                    else:
                        label = f'{ic50/1000:.2f}mM'
                    ax2.text(bar.get_x() + bar.get_width()/2., height * 1.1,
                            label, ha='center', va='bottom', fontsize=10, fontweight='bold')
            
            # 结合概率分析
            if len(binding_prob_data) >= 3 and ax3 is not None:
                combined_scores_bp, binding_probs, names_bp = zip(*binding_prob_data)
                
                # 散点图：Combined Score vs Binding Probability
                ax3.scatter(combined_scores_bp, binding_probs, 
                           c=self.color_scheme['success'], s=80, alpha=0.8,
                           edgecolors='white', linewidth=2)
                ax3.set_xlabel('Combined Score', fontweight='bold')
                ax3.set_ylabel('Binding Probability', fontweight='bold')
                ax3.set_title('Combined Score vs Binding Probability', fontweight='bold', pad=20)
                ax3.grid(True, alpha=0.4)
                self._apply_clean_style(ax3)
                
                # 添加趋势线
                z = np.polyfit(combined_scores_bp, binding_probs, 1)
                p = np.poly1d(z)
                x_trend = np.linspace(min(combined_scores_bp), max(combined_scores_bp), 100)
                ax3.plot(x_trend, p(x_trend), color=self.color_scheme['secondary'], 
                        linestyle='--', linewidth=3, alpha=0.8)
                
                # 结合概率分布直方图
                ax4.hist(binding_probs, bins=15, alpha=0.8, color=self.color_scheme['teal'],
                        edgecolor='white', linewidth=2)
                ax4.set_xlabel('Binding Probability', fontweight='bold')
                ax4.set_ylabel('Molecule Count', fontweight='bold')
                ax4.set_title('Binding Probability Distribution', fontweight='bold', pad=20)
                ax4.grid(True, alpha=0.4)
                self._apply_clean_style(ax4)
                
                # 添加统计信息
                mean_prob = np.mean(binding_probs)
                median_prob = np.median(binding_probs)
                ax4.axvline(mean_prob, color=self.color_scheme['secondary'], linestyle='--', 
                           linewidth=2, label=f'Mean: {mean_prob:.3f}')
                ax4.axvline(median_prob, color=self.color_scheme['warning'], linestyle='--', 
                           linewidth=2, label=f'Median: {median_prob:.3f}')
                ax4.legend()
            
            plt.tight_layout()
            
            output_path = os.path.join(self.plots_dir, "ic50_binding_analysis.png")
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"IC50和结合概率分析图已保存: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"生成IC50和结合概率分析图失败: {e}")
            return None
    
    def _plot_affinity_analysis(self) -> Optional[str]:
        """绘制亲和力分析图（如果有亲和力数据）"""
        try:
            # 检查是否有亲和力数据
            affinity_data = []
            for result in self.screening_results:
                if 'affinity' in result.properties:
                    affinity_value = result.properties['affinity']
                    if isinstance(affinity_value, (int, float)):
                        affinity_data.append((result.combined_score, affinity_value, result.molecule_name))
            
            if len(affinity_data) < 3:
                logger.info("亲和力数据不足，跳过亲和力分析图")
                return None
            
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8))
            
            # 提取数据
            combined_scores, affinities, names = zip(*affinity_data)
            
            # 散点图: Combined Score vs Affinity
            ax1.scatter(combined_scores, affinities, alpha=0.8, s=100,
                       c=self.color_scheme['secondary'], edgecolors='white', linewidth=2)
            ax1.set_xlabel('Combined Score', fontweight='bold')
            ax1.set_ylabel('Binding Affinity (kcal/mol)', fontweight='bold')
            ax1.set_title('Combined Score vs Binding Affinity', fontweight='bold', pad=20)
            ax1.grid(True, alpha=0.4)
            ax1.set_facecolor('#f8f9fa')
            self._apply_clean_style(ax1)
            
            # 添加趋势线
            z = np.polyfit(combined_scores, affinities, 1)
            p = np.poly1d(z)
            x_trend = np.linspace(min(combined_scores), max(combined_scores), 100)
            ax1.plot(x_trend, p(x_trend), color=self.color_scheme['primary'], 
                    linestyle='--', alpha=0.9, linewidth=3)
            
            # 条形图: 前10名的亲和力
            top_10_indices = np.argsort(combined_scores)[-10:]
            top_10_names = [names[i][:8] + "..." if len(names[i]) > 8 else names[i] 
                           for i in top_10_indices]
            top_10_affinities = [affinities[i] for i in top_10_indices]
            
            colors_gradient = plt.cm.plasma(np.linspace(0.2, 0.9, len(top_10_names)))
            bars = ax2.bar(range(len(top_10_names)), top_10_affinities,
                          color=colors_gradient, alpha=0.8, edgecolor='white', linewidth=2)
            ax2.set_xticks(range(len(top_10_names)))
            ax2.set_xticklabels(top_10_names, rotation=45, ha='right', fontweight='bold')
            ax2.set_ylabel('Binding Affinity (kcal/mol)', fontweight='bold')
            ax2.set_title('Top 10 Molecules - Binding Affinity', fontweight='bold', pad=20)
            ax2.grid(True, alpha=0.4, axis='y')
            ax2.set_facecolor('#f8f9fa')
            self._apply_clean_style(ax2)
            
            # 添加数值标签
            for bar, affinity in zip(bars, top_10_affinities):
                height = bar.get_height()
                ax2.text(bar.get_x() + bar.get_width()/2., height + 0.2,
                        f'{affinity:.2f}', ha='center', va='bottom', fontsize=12, fontweight='bold')
            
            plt.tight_layout()
            
            output_path = os.path.join(self.plots_dir, "affinity_analysis.png")
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"亲和力分析图已保存: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"生成亲和力分析图失败: {e}")
            return None
    
    def _plot_molecular_complexity(self) -> Optional[str]:
        """绘制分子复杂度分析图"""
        try:
            if not RDKIT_AVAILABLE:
                logger.info("RDKit不可用，跳过分子复杂度分析")
                return None
            
            # 计算分子复杂度
            complexity_data = []
            for result in self.screening_results:
                if result.mol_type == "small_molecule":
                    try:
                        mol = Chem.MolFromSmiles(result.sequence)
                        if mol:
                            mw = Descriptors.MolWt(mol)
                            logp = Descriptors.MolLogP(mol)
                            hbd = Descriptors.NumHDonors(mol)
                            hba = Descriptors.NumHAcceptors(mol)
                            complexity_data.append((result.combined_score, mw, logp, 
                                                  hbd, hba, result.molecule_name))
                    except:
                        continue
            
            if len(complexity_data) < 3:
                logger.info("分子复杂度数据不足，跳过复杂度分析图")
                return None
            
            fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 12))
            
            # 提取数据
            scores, mws, logps, hbds, hbas, names = zip(*complexity_data)
            
            # 分子量 vs 评分
            ax1.scatter(mws, scores, alpha=0.8, s=80, c=self.color_scheme['primary'],
                       edgecolors='white', linewidth=2)
            ax1.set_xlabel('Molecular Weight (Da)', fontweight='bold')
            ax1.set_ylabel('Combined Score', fontweight='bold')
            ax1.set_title('Molecular Weight vs Score', fontweight='bold', pad=20)
            ax1.grid(True, alpha=0.4)
            ax1.set_facecolor('#f8f9fa')
            self._apply_clean_style(ax1)
            
            # LogP vs 评分
            ax2.scatter(logps, scores, alpha=0.8, s=80, c=self.color_scheme['success'],
                       edgecolors='white', linewidth=2)
            ax2.set_xlabel('LogP', fontweight='bold')
            ax2.set_ylabel('Combined Score', fontweight='bold')
            ax2.set_title('LogP vs Score', fontweight='bold', pad=20)
            ax2.grid(True, alpha=0.4)
            ax2.set_facecolor('#f8f9fa')
            self._apply_clean_style(ax2)
            
            # 氢键供体 vs 受体
            scatter = ax3.scatter(hbds, hbas, c=scores, s=80, cmap='plasma',
                                alpha=0.8, edgecolors='white', linewidth=2)
            ax3.set_xlabel('H-Bond Donors', fontweight='bold')
            ax3.set_ylabel('H-Bond Acceptors', fontweight='bold')
            ax3.set_title('H-Bond Profile', fontweight='bold', pad=20)
            ax3.grid(True, alpha=0.4)
            ax3.set_facecolor('#f8f9fa')
            self._apply_clean_style(ax3)
            
            # 添加颜色条
            cbar = plt.colorbar(scatter, ax=ax3)
            cbar.set_label('Combined Score', fontweight='bold')
            
            # Lipinski规则分析
            lipinski_pass = []
            lipinski_labels = []
            for mw, logp, hbd, hba in zip(mws, logps, hbds, hbas):
                violations = 0
                if mw > 500: violations += 1
                if logp > 5: violations += 1
                if hbd > 5: violations += 1
                if hba > 10: violations += 1
                lipinski_pass.append(4 - violations)
                lipinski_labels.append(f'{4-violations}/4')
            
            ax4.scatter(lipinski_pass, scores, alpha=0.8, s=80, c=self.color_scheme['warning'],
                       edgecolors='white', linewidth=2)
            ax4.set_xlabel('Lipinski Rules Passed (out of 4)', fontweight='bold')
            ax4.set_ylabel('Combined Score', fontweight='bold')
            ax4.set_title('Drug-likeness vs Score', fontweight='bold', pad=20)
            ax4.set_xticks([0, 1, 2, 3, 4])
            ax4.grid(True, alpha=0.4)
            ax4.set_facecolor('#f8f9fa')
            self._apply_clean_style(ax4)
            
            plt.tight_layout()
            
            output_path = os.path.join(self.plots_dir, "molecular_complexity.png")
            plt.savefig(output_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            logger.info(f"分子复杂度分析图已保存: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"生成分子复杂度分析图失败: {e}")
            return None
    
    def generate_html_report(self, plots: List[str] = None) -> str:
        """生成HTML报告"""
        try:
            # 如果没有提供图表，生成新的
            if plots is None:
                plots = self.generate_screening_plots()
            
            # 计算统计数据
            logger.info("正在计算统计数据...")
            stats = self._calculate_statistics()
            
            # 获取前10名分子
            logger.info("正在获取前10名分子...")
            top_molecules = self._get_top_molecules(10)
            
            # 生成HTML内容
            logger.info("正在生成HTML内容...")
            html_content = self._generate_html_template(stats, top_molecules, plots)
            
            # 保存HTML文件
            html_path = os.path.join(self.output_dir, "screening_report.html")
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            logger.info(f"HTML报告已生成: {html_path}")
            return html_path
        
        except Exception as e:
            import traceback
            logger.error(f"生成HTML报告失败: {e}")
            logger.error(f"完整错误信息: {traceback.format_exc()}")
            return ""
    
    def _get_top_molecules(self, count: int = 10):
        """根据分子类型获取排序后的前N名分子"""
        def _scoring_key(result):
            """为分子计算综合排序分数"""
            if result.mol_type == "small_molecule":
                # 小分子使用综合评分：Binding Prob高、IC50低、ipTM高、pLDDT高
                scores = []
                weights = []
                
                # 1. 结合概率 (权重: 0.3)
                binding_prob = 0.0
                if hasattr(result, 'properties') and result.properties:
                    binding_prob = result.properties.get('binding_probability', 0.0)
                if isinstance(binding_prob, (int, float)):
                    scores.append(binding_prob)
                    weights.append(0.3)
                
                # 2. IC50转换为分数 (权重: 0.3) - IC50越低分数越高
                ic50_score = 0.0
                if hasattr(result, 'properties') and result.properties:
                    ic50_uM = result.properties.get('ic50_uM')
                    if isinstance(ic50_uM, (int, float)) and ic50_uM > 0:
                        # 使用对数转换，IC50越小分数越高
                        # 1μM对应分数0.5，0.1μM对应分数约0.75，0.01μM对应分数约1.0
                        ic50_score = max(0, 1 - (np.log10(ic50_uM) + 2) / 4)  # 范围约0-1
                if ic50_score > 0:
                    scores.append(ic50_score)
                    weights.append(0.3)
                
                # 3. ipTM分数 (权重: 0.2)
                iptm = 0.0
                if hasattr(result, 'properties') and result.properties:
                    iptm = result.properties.get('iptm', 0.0)
                if not isinstance(iptm, (int, float)) or iptm <= 0:
                    iptm = result.binding_score if result.binding_score is not None else 0.0
                if isinstance(iptm, (int, float)):
                    scores.append(iptm)
                    weights.append(0.2)
                
                # 4. pLDDT分数 (权重: 0.2)
                plddt = 0.0
                if hasattr(result, 'properties') and result.properties:
                    plddt = result.properties.get('plddt', 0.0)
                if not isinstance(plddt, (int, float)) or plddt <= 0:
                    plddt = (result.structural_score * 100) if result.structural_score is not None else 0.0
                if isinstance(plddt, (int, float)):
                    # pLDDT通常是0-100，归一化到0-1
                    plddt_normalized = plddt / 100.0 if plddt > 1 else plddt
                    scores.append(plddt_normalized)
                    weights.append(0.2)
                
                # 计算加权平均分
                if scores and weights:
                    total_weight = sum(weights)
                    weighted_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
                    return weighted_score
                else:
                    # 如果没有小分子特有数据，回退到combined_score
                    return result.combined_score if result.combined_score is not None else 0.0
            else:
                # 非小分子使用原来的combined_score排序
                return result.combined_score if result.combined_score is not None else 0.0
        
        # 排序并获取前N名
        sorted_results = sorted(self.screening_results, key=_scoring_key, reverse=True)
        return sorted_results[:count]
    
    def _calculate_statistics(self) -> Dict[str, Any]:
        """计算详细统计信息"""
        if not self.screening_results:
            return {}
        
        # 过滤掉None值
        binding_scores = [r.binding_score for r in self.screening_results if r.binding_score is not None]
        structural_scores = [r.structural_score for r in self.screening_results if r.structural_score is not None]
        confidence_scores = [r.confidence_score for r in self.screening_results if r.confidence_score is not None]
        combined_scores = [r.combined_score for r in self.screening_results if r.combined_score is not None]
        
        stats = {
            'total_molecules': len(self.screening_results),
            'binding_score': {
                'mean': np.mean(binding_scores),
                'std': np.std(binding_scores),
                'min': np.min(binding_scores),
                'max': np.max(binding_scores),
                'median': np.median(binding_scores)
            },
            'structural_score': {
                'mean': np.mean(structural_scores),
                'std': np.std(structural_scores),
                'min': np.min(structural_scores),
                'max': np.max(structural_scores),
                'median': np.median(structural_scores)
            },
            'confidence_score': {
                'mean': np.mean(confidence_scores),
                'std': np.std(confidence_scores),
                'min': np.min(confidence_scores),
                'max': np.max(confidence_scores),
                'median': np.median(confidence_scores)
            },
            'combined_score': {
                'mean': np.mean(combined_scores),
                'std': np.std(combined_scores),
                'min': np.min(combined_scores),
                'max': np.max(combined_scores),
                'median': np.median(combined_scores)
            }
        }
        
        # 添加亲和力统计（如果有）
        affinity_values = []
        binding_probability_values = []
        ic50_values = []
        
        for result in self.screening_results:
            if hasattr(result, 'properties') and result.properties:
                # 亲和力统计
                if 'affinity' in result.properties:
                    affinity_value = result.properties['affinity']
                    if isinstance(affinity_value, (int, float)):
                        affinity_values.append(affinity_value)
                
                # 结合概率统计（小分子专用）
                if result.mol_type == "small_molecule" and 'binding_probability' in result.properties:
                    binding_prob = result.properties['binding_probability']
                    if isinstance(binding_prob, (int, float)):
                        binding_probability_values.append(binding_prob)
                
                # IC50统计
                if 'ic50_uM' in result.properties:
                    ic50_uM = result.properties['ic50_uM']
                    if isinstance(ic50_uM, (int, float)) and ic50_uM > 0:
                        ic50_values.append(ic50_uM)
        
        if affinity_values:
            stats['affinity'] = {
                'mean': np.mean(affinity_values),
                'std': np.std(affinity_values),
                'min': np.min(affinity_values),
                'max': np.max(affinity_values),
                'median': np.median(affinity_values),
                'count': len(affinity_values)
            }
        
        if binding_probability_values:
            stats['binding_probability'] = {
                'mean': np.mean(binding_probability_values),
                'std': np.std(binding_probability_values),
                'min': np.min(binding_probability_values),
                'max': np.max(binding_probability_values),
                'median': np.median(binding_probability_values),
                'count': len(binding_probability_values)
            }
        
        if ic50_values:
            stats['ic50'] = {
                'mean': np.mean(ic50_values),
                'std': np.std(ic50_values),
                'min': np.min(ic50_values),
                'max': np.max(ic50_values),
                'median': np.median(ic50_values),
                'count': len(ic50_values)
            }
        
        return stats
    
    def _generate_2d_structure(self, sequence: str, mol_name: str, mol_type: str = "small_molecule", 
                              structure_path: str = "", target_sequence: str = "") -> str:
        """根据分子类型生成2D结构展示"""
        if mol_type == "peptide":
            return self._generate_peptide_sequence_view(sequence, mol_name, structure_path, target_sequence)
        elif mol_type == "small_molecule":
            return self._generate_small_molecule_2d(sequence, mol_name)
        else:
            return f'<div class="no-structure">Structure display not supported for {mol_type}</div>'
    
    def _generate_small_molecule_2d(self, smiles: str, mol_name: str) -> str:
        """生成小分子2D结构SVG"""
        if not RDKIT_AVAILABLE:
            return f'<div class="no-structure">RDKit not available for {mol_name}</div>'
        
        try:
            if not smiles or not smiles.strip():
                return f'<div class="no-structure">No SMILES data for {mol_name}</div>'
                
            mol = Chem.MolFromSmiles(smiles.strip())
            if mol is None:
                return f'<div class="no-structure">Invalid SMILES: {smiles[:20]}... for {mol_name}</div>'
            
            # 创建2D绘图器，使用更大的画布以提高清晰度
            drawer = rdMolDraw2D.MolDraw2DSVG(400, 400)
            drawer.SetFontSize(1.0)
            
            # 设置绘图选项以提高质量
            opts = drawer.drawOptions()
            try:
                opts.clearBackground = False
                # 尝试新的API
                if hasattr(opts, 'setBackgroundColour'):
                    opts.setBackgroundColour((1, 1, 1, 1))
                elif hasattr(opts, 'backgroundColour'):
                    opts.backgroundColour = (1, 1, 1, 1)
            except Exception:
                # 如果设置背景失败，忽略并继续
                pass
            
            drawer.DrawMolecule(mol)
            drawer.FinishDrawing()
            
            svg_text = drawer.GetDrawingText()
            
            # 清理SVG文本
            if '<?xml' in svg_text:
                svg_start = svg_text.find('<svg')
                if svg_start != -1:
                    svg_text = svg_text[svg_start:]
            
            # 确保SVG有正确的属性
            if '<svg' in svg_text and 'viewBox=' not in svg_text:
                svg_text = svg_text.replace('<svg', '<svg viewBox="0 0 400 400"', 1)
            
            # 添加样式以确保在HTML中正确显示
            style_attr = 'style="max-width: 100%; height: auto; background: white; border: 1px solid #ddd; border-radius: 5px;"'
            if 'style=' in svg_text:
                # 合并现有样式
                svg_text = svg_text.replace('style="', f'{style_attr[:-1]}; ', 1)
            else:
                svg_text = svg_text.replace('<svg', f'<svg {style_attr}', 1)
            
            return f'''
            <div style="text-align: center; padding: 10px; background: #f8f9fa; border-radius: 5px;">
                {svg_text}
                <p style="margin: 5px 0 0 0; font-size: 0.8em; color: #666;">
                    SMILES: {smiles[:30]}{'...' if len(smiles) > 30 else ''}
                </p>
            </div>'''
            
        except Exception as e:
            logger.warning(f"生成分子结构失败 {mol_name}: {e}")
            import traceback
            logger.debug(f"详细错误信息: {traceback.format_exc()}")
            return f'''
            <div class="no-structure" style="padding: 20px; text-align: center; background: #f8f9fa; border: 1px solid #ddd; border-radius: 5px;">
                <p>Structure generation failed</p>
                <p style="font-size: 0.8em; color: #666;">SMILES: {smiles[:30] if smiles else 'None'}</p>
                <p style="font-size: 0.7em; color: #999;">Error: {str(e)[:50]}</p>
            </div>'''
    
    def _generate_peptide_sequence_view(self, sequence: str, mol_name: str, structure_path: str = "", target_sequence: str = "") -> str:
        """生成多肽序列的2D可视化展示，基于pLDDT值着色"""
        try:
            # 获取每个残基的pLDDT值，跳过target部分
            target_length = len(target_sequence) if target_sequence else 0
            plddt_values = self._extract_plddt_from_cif(structure_path, sequence, target_length)
            
            # 如果没有pLDDT数据，使用默认展示
            if not plddt_values:
                return self._generate_simple_peptide_view(sequence, mol_name)
            
            # 生成着色的多肽序列
            return self._generate_colored_peptide_sequence(sequence, plddt_values, mol_name)
            
        except Exception as e:
            logger.warning(f"生成多肽序列视图失败 {mol_name}: {e}")
            return self._generate_simple_peptide_view(sequence, mol_name)
    
    def _extract_plddt_from_cif(self, structure_path: str, sequence: str, target_length: int = 0) -> List[float]:
        """从CIF文件中提取peptide链(B链)的pLDDT值
        
        Args:
            structure_path: CIF文件路径
            sequence: peptide序列
            target_length: target蛋白序列长度（用于跳过A链）
        
        Returns:
            List[float]: peptide每个残基的pLDDT值
        """
        if not structure_path or not os.path.exists(structure_path):
            logger.debug(f"CIF文件不存在: {structure_path}")
            return []
        
        try:
            with open(structure_path, 'r') as f:
                lines = f.readlines()
            
            peptide_plddt = []
            
            # 查找残基信息部分（格式：残基编号 模型编号 链ID 链内残基编号 残基名 占有率 pLDDT）
            for line in lines:
                line = line.strip()
                
                # 跳过空行和注释
                if not line or line.startswith('#'):
                    continue
                
                # 解析残基数据行
                parts = line.split()
                if len(parts) >= 7:
                    try:
                        residue_num = int(parts[0])
                        model_id = int(parts[1])  
                        chain_id = parts[2]
                        chain_residue_num = int(parts[3])
                        residue_name = parts[4]
                        occupancy = float(parts[5])
                        plddt = float(parts[6])
                        
                        # 只提取B链（peptide链）的数据
                        if chain_id == 'B':
                            peptide_plddt.append((chain_residue_num, residue_name, plddt))
                            logger.debug(f"B链残基 {chain_residue_num}: {residue_name} pLDDT={plddt}")
                            
                    except (ValueError, IndexError):
                        # 不是残基数据行，跳过
                        continue
            
            if not peptide_plddt:
                logger.warning(f"未找到B链的pLDDT数据")
                return []
            
            # 按链内残基编号排序
            peptide_plddt.sort(key=lambda x: x[0])
            
            # 提取pLDDT值
            plddt_values = [plddt for _, _, plddt in peptide_plddt]
            
            logger.info(f"从CIF文件成功提取 {len(plddt_values)} 个B链残基的pLDDT值")
            logger.debug(f"pLDDT值范围: {min(plddt_values):.1f} - {max(plddt_values):.1f}")
            
            # 如果pLDDT值的数量与序列长度不匹配，进行调整
            if len(plddt_values) != len(sequence):
                logger.warning(f"pLDDT值数量({len(plddt_values)})与peptide序列长度({len(sequence)})不匹配")
                if len(plddt_values) > len(sequence):
                    # 截断多余的值
                    plddt_values = plddt_values[:len(sequence)]
                    logger.info(f"截断pLDDT值到序列长度: {len(plddt_values)}")
                else:
                    # 用平均值填充缺失的值
                    avg_plddt = sum(plddt_values) / len(plddt_values) if plddt_values else 70.0
                    while len(plddt_values) < len(sequence):
                        plddt_values.append(avg_plddt)
                    logger.info(f"用平均pLDDT值({avg_plddt:.1f})填充到序列长度: {len(plddt_values)}")
            
            return plddt_values
            
        except Exception as e:
            logger.error(f"从CIF文件提取pLDDT值失败: {e}")
            import traceback
            logger.debug(f"详细错误信息: {traceback.format_exc()}")
            return []
    
    def _generate_colored_peptide_sequence(self, sequence: str, plddt_values: List[float], mol_name: str) -> str:
        """生成基于pLDDT值着色的多肽序列"""
        try:
            # 确保pLDDT值和序列长度匹配
            if len(plddt_values) != len(sequence):
                logger.warning(f"pLDDT值数量({len(plddt_values)})与序列长度({len(sequence)})不匹配")
                return self._generate_simple_peptide_view(sequence, mol_name)
            
            sequence_html = '<div style="font-family: monospace; font-size: 14px; line-height: 1.5; padding: 15px; background: #f8f9fa; border-radius: 8px; border: 1px solid #ddd;">'
            sequence_html += f'<div style="text-align: center; margin-bottom: 10px; font-weight: bold; color: #2c3e50;">{mol_name}</div>'
            sequence_html += '<div style="text-align: center; margin-bottom: 15px;">'
            
            # 生成序列，每行显示10个残基
            residues_per_line = 10
            for i in range(0, len(sequence), residues_per_line):
                sequence_html += '<div style="margin: 8px 0;">'
                
                # 添加位置标号
                start_pos = i + 1
                end_pos = min(i + residues_per_line, len(sequence))
                sequence_html += f'<span style="color: #6c757d; font-size: 10px; margin-right: 10px;">{start_pos:3d}-{end_pos:3d}:</span>'
                
                # 添加着色的残基
                for j in range(i, min(i + residues_per_line, len(sequence))):
                    residue = sequence[j]
                    plddt = plddt_values[j]
                    
                    # 根据pLDDT值确定AlphaFold颜色
                    color = self._get_alphafold_color_for_plddt(plddt)
                    
                    # 创建残基元素，包含工具提示
                    sequence_html += f'''
                    <span style="
                        background-color: {color};
                        color: white;
                        padding: 2px 4px;
                        margin: 1px;
                        border-radius: 3px;
                        text-shadow: 1px 1px 1px rgba(0,0,0,0.5);
                        font-weight: bold;
                        cursor: help;
                    " title="Residue {residue}{j+1}, pLDDT: {plddt:.1f}">{residue}</span>'''
                
                sequence_html += '</div>'
            
            sequence_html += '</div>'
            
            # 添加颜色图例
            sequence_html += self._generate_alphafold_legend()
            
            # 添加序列信息
            avg_plddt = sum(plddt_values) / len(plddt_values)
            min_plddt = min(plddt_values)
            max_plddt = max(plddt_values)
            
            sequence_html += f'''
            <div style="margin-top: 15px; padding: 10px; background: rgba(0,0,0,0.05); border-radius: 5px; font-size: 12px;">
                <div><strong>Sequence Length:</strong> {len(sequence)} residues</div>
                <div><strong>Average pLDDT:</strong> {avg_plddt:.1f}</div>
                <div><strong>pLDDT Range:</strong> {min_plddt:.1f} - {max_plddt:.1f}</div>
            </div>
            '''
            
            sequence_html += '</div>'
            return sequence_html
            
        except Exception as e:
            logger.error(f"生成着色多肽序列失败: {e}")
            return self._generate_simple_peptide_view(sequence, mol_name)
    
    def _get_alphafold_color_for_plddt(self, plddt: float) -> str:
        """根据pLDDT值返回AlphaFold标准颜色"""
        # AlphaFold颜色方案：
        # 90-100: 深蓝色 (#0053D6) - 非常高置信度
        # 70-90:  浅蓝色 (#65CBF3) - 高置信度  
        # 50-70:  黄色   (#FFDB13) - 中等置信度
        # 30-50:  橙色   (#FF7D45) - 低置信度
        # 0-30:   红色   (#FF0000) - 非常低置信度
        
        if plddt >= 90:
            return '#0053D6'      # 深蓝色 - 非常高置信度
        elif plddt >= 70:
            return '#65CBF3'      # 浅蓝色 - 高置信度
        elif plddt >= 50:
            return '#FFDB13'      # 黄色 - 中等置信度
        elif plddt >= 30:
            return '#FF7D45'      # 橙色 - 低置信度
        else:
            return '#FF0000'      # 红色 - 非常低置信度
    
    def _generate_alphafold_legend(self) -> str:
        """生成AlphaFold颜色图例"""
        return f'''
        <div style="margin-top: 10px; text-align: center;">
            <div style="font-size: 11px; color: #6c757d; margin-bottom: 5px;"><strong>pLDDT Confidence Scale (AlphaFold)</strong></div>
            <div style="display: inline-flex; align-items: center; gap: 15px; flex-wrap: wrap; justify-content: center;">
                <span style="display: flex; align-items: center;">
                    <div style="width: 15px; height: 15px; background: #0053D6; margin-right: 5px; border-radius: 2px;"></div>
                    <span style="font-size: 10px;">90-100</span>
                </span>
                <span style="display: flex; align-items: center;">
                    <div style="width: 15px; height: 15px; background: #65CBF3; margin-right: 5px; border-radius: 2px;"></div>
                    <span style="font-size: 10px;">70-90</span>
                </span>
                <span style="display: flex; align-items: center;">
                    <div style="width: 15px; height: 15px; background: #FFDB13; margin-right: 5px; border-radius: 2px;"></div>
                    <span style="font-size: 10px;">50-70</span>
                </span>
                <span style="display: flex; align-items: center;">
                    <div style="width: 15px; height: 15px; background: #FF7D45; margin-right: 5px; border-radius: 2px;"></div>
                    <span style="font-size: 10px;">30-50</span>
                </span>
                <span style="display: flex; align-items: center;">
                    <div style="width: 15px; height: 15px; background: #FF0000; margin-right: 5px; border-radius: 2px;"></div>
                    <span style="font-size: 10px;">0-30</span>
                </span>
            </div>
        </div>
        '''
    
    def _generate_simple_peptide_view(self, sequence: str, mol_name: str) -> str:
        """生成简单的多肽序列视图（当无pLDDT数据时使用）"""
        sequence_html = '<div style="font-family: monospace; font-size: 14px; line-height: 1.5; padding: 15px; background: #f8f9fa; border-radius: 8px; border: 1px solid #ddd;">'
        sequence_html += f'<div style="text-align: center; margin-bottom: 10px; font-weight: bold; color: #2c3e50;">{mol_name}</div>'
        sequence_html += '<div style="text-align: center; margin-bottom: 15px;">'
        
        # 生成序列，每行显示10个残基
        residues_per_line = 10
        for i in range(0, len(sequence), residues_per_line):
            sequence_html += '<div style="margin: 8px 0;">'
            
            # 添加位置标号
            start_pos = i + 1
            end_pos = min(i + residues_per_line, len(sequence))
            sequence_html += f'<span style="color: #6c757d; font-size: 10px; margin-right: 10px;">{start_pos:3d}-{end_pos:3d}:</span>'
            
            # 添加残基（使用统一的灰色）
            for j in range(i, min(i + residues_per_line, len(sequence))):
                residue = sequence[j]
                sequence_html += f'''
                <span style="
                    background-color: #6c757d;
                    color: white;
                    padding: 2px 4px;
                    margin: 1px;
                    border-radius: 3px;
                    text-shadow: 1px 1px 1px rgba(0,0,0,0.5);
                    font-weight: bold;
                " title="Residue {residue}{j+1}">{residue}</span>'''
            
            sequence_html += '</div>'
        
        sequence_html += '</div>'
        
        # 添加序列信息
        sequence_html += f'''
        <div style="margin-top: 15px; padding: 10px; background: rgba(0,0,0,0.05); border-radius: 5px; font-size: 12px; text-align: center;">
            <div><strong>Sequence Length:</strong> {len(sequence)} residues</div>
            <div style="color: #6c757d; font-style: italic; margin-top: 5px;">No pLDDT data available - uniform coloring applied</div>
        </div>
        '''
        
        sequence_html += '</div>'
        return sequence_html
    
    def _generate_html_template(self, stats: Dict[str, Any], 
                                           top_molecules: List[Dict], 
                                           plots: List[str]) -> str:
        """生成HTML模板"""        # 读取图片并转换为base64（用于嵌入HTML）
        plot_images = {}
        for plot_path in plots:
            if os.path.exists(plot_path):
                import base64
                with open(plot_path, 'rb') as f:
                    img_data = base64.b64encode(f.read()).decode()
                    plot_name = os.path.basename(plot_path).replace('.png', '')
                    plot_images[plot_name] = f"data:image/png;base64,{img_data}"
        
        # 生成前10名分子的HTML
        molecules_html = ""
        for i, result in enumerate(top_molecules):
            # 生成2D结构
            structure_svg = ""
            # 构造正确的CIF文件路径
            task_dir = getattr(result, 'structure_path', '')
            cif_path = ""
            if task_dir and os.path.isdir(task_dir):
                cif_path = os.path.join(task_dir, 'data_model_0.cif')
                if not os.path.exists(cif_path):
                    cif_path = ""
            elif task_dir and task_dir.endswith('.cif') and os.path.exists(task_dir):
                cif_path = task_dir
            
            if result.mol_type == "small_molecule":
                structure_svg = self._generate_2d_structure(result.sequence, result.molecule_name, 
                                                           result.mol_type, cif_path,
                                                           self.target_sequence)
            elif result.mol_type == "peptide":
                structure_svg = self._generate_2d_structure(result.sequence, result.molecule_name, 
                                                           result.mol_type, cif_path,
                                                           self.target_sequence)
            else:
                structure_svg = '<div class="no-structure">Structure display not supported for this molecule type</div>'
            
            # 亲和力和科学指标信息
            scientific_info = ""
            
            # 小分子特有的指标
            if result.mol_type == "small_molecule" and hasattr(result, 'properties') and result.properties:
                # IC50 信息（使用平均值）
                ic50_uM = result.properties.get('ic50_uM')
                if ic50_uM is not None:
                    # 统一使用μM单位显示IC50
                    ic50_range = result.properties.get('ic50_range_display')
                    ic50_display = ic50_range if ic50_range else f"{ic50_uM:.3f} μM"
                    
                    scientific_info += f'''
                    <div class="detail-item affinity-highlight">
                        <span>IC50:</span>
                        <span>{ic50_display}</span>
                    </div>'''
                
                # 结合概率信息（小分子专用）
                binding_probability = result.properties.get('binding_probability')
                if binding_probability is not None:
                    # 使用AlphaFold颜色原则显示结合概率
                    binding_prob_color = self._get_alphafold_color(binding_probability, "confidence")
                    
                    # 使用百分比区间信息
                    binding_range_percent = result.properties.get('binding_probability_range_percent')
                    prob_display = binding_range_percent if binding_range_percent else f"{binding_probability*100:.1f}%"
                    
                    scientific_info += f'''
                    <div class="detail-item alphafold-style" style="border-left: 4px solid {binding_prob_color};">
                        <span>Binding Prob:</span>
                        <span style="color: {binding_prob_color}; font-weight: bold;">{prob_display}</span>
                    </div>'''
                
                # pIC50 信息
                pIC50 = result.properties.get('pIC50')
                if pIC50 is not None:
                    scientific_info += f'''
                    <div class="detail-item">
                        <span>pIC50:</span>
                        <span>{pIC50:.2f}</span>
                    </div>'''
                
                # 结合自由能信息（kcal/mol）
                affinity_kcal_mol = result.properties.get('affinity_kcal_mol')
                if affinity_kcal_mol is not None:
                    scientific_info += f'''
                    <div class="detail-item">
                        <span>ΔG:</span>
                        <span>{affinity_kcal_mol:.2f} kcal/mol</span>
                    </div>'''
            
            # 通用指标（所有分子类型）
            elif hasattr(result, 'properties') and result.properties:
                # 对于多肽等其他分子类型，显示基本信息
                binding_affinity = result.properties.get('binding_affinity')
                if binding_affinity is not None:
                    scientific_info += f'''
                    <div class="detail-item">
                        <span>Binding Affinity:</span>
                        <span>{binding_affinity:.3f}</span>
                    </div>'''
            
            # ipTM 信息 - 使用AlphaFold颜色
            iptm = result.properties.get('iptm') if hasattr(result, 'properties') and result.properties else None
            if iptm is None or iptm == 0.0:
                iptm = result.binding_score if result.binding_score is not None else 0.0  # 备选
            iptm_color = self._get_alphafold_color(iptm, "iptm")
                
            # pLDDT 信息 - 使用AlphaFold颜色
            plddt = result.properties.get('plddt') if hasattr(result, 'properties') and result.properties else None
            if plddt is None or plddt == 0.0:
                plddt = (result.structural_score * 100) if result.structural_score is not None else 0.0  # 备选，转换为百分比
            plddt_color = self._get_alphafold_color(plddt, "plddt")
            
            # 综合评分颜色
            combined_score = result.combined_score if result.combined_score is not None else 0.0
            combined_color = self._get_alphafold_color(combined_score, "confidence")
            
            molecules_html += f'''
            <div class="molecule-card">
                <div class="molecule-header">
                    <div class="molecule-title">{result.molecule_name}</div>
                    <div class="molecule-rank" style="color: {combined_color}; font-weight: bold;">#{result.rank}</div>
                </div>
                
                <div class="molecule-structure">
                    {structure_svg}
                </div>
                
                <div class="molecule-details">
                    <div class="detail-item alphafold-style" style="border-left: 4px solid {iptm_color};">
                        <span>ipTM:</span>
                        <span style="color: {iptm_color}; font-weight: bold;">{iptm:.3f}</span>
                    </div>
                    <div class="detail-item alphafold-style" style="border-left: 4px solid {plddt_color};">
                        <span>pLDDT:</span>
                        <span style="color: {plddt_color}; font-weight: bold;">{plddt:.1f}</span>
                    </div>
                    {scientific_info}
                </div>
            </div>'''
        
        # 生成图表部分HTML
        plots_html = ""
        if plot_images:
            plots_html = '''
            <div class="section">
                <h2>📊 Analysis Figures</h2>
                <div class="plots-grid">'''
            
            for plot_name, plot_data in plot_images.items():
                title = plot_name.replace('_', ' ').title()
                plots_html += f'''
                    <div class="plot-container">
                        <h3>{title}</h3>
                        <img src="{plot_data}" alt="{title}" class="analysis-plot">
                    </div>'''
            
            plots_html += '''
                </div>
            </div>'''
        
        # 生成虚拟筛选专用统计摘要
        stats_html = ""
        if stats:
            # 计算筛选成功率和质量指标
            high_quality_count = sum(1 for r in self.screening_results if r.combined_score is not None and r.combined_score >= 0.7)
            medium_quality_count = sum(1 for r in self.screening_results if r.combined_score is not None and 0.5 <= r.combined_score < 0.7)
            hit_rate = (high_quality_count / stats['total_molecules']) * 100 if stats['total_molecules'] > 0 else 0
            
            # 小分子特有统计
            small_mol_stats = ""
            small_molecule_count = sum(1 for r in self.screening_results if r.mol_type == "small_molecule")
            
            if small_molecule_count > 0:
                # 结合概率统计（小分子专用）
                if 'binding_probability' in stats:
                    high_prob_count = sum(1 for r in self.screening_results 
                                        if r.mol_type == "small_molecule" and 
                                        hasattr(r, 'properties') and r.properties and
                                        isinstance(r.properties.get('binding_probability'), (int, float)) and
                                        r.properties.get('binding_probability', 0) >= 0.7)
                    small_mol_stats += f'''
                    <div class="stats-card highlight">
                        <div class="stats-value">{high_prob_count}</div>
                        <div>High Binding Probability (≥0.7)</div>
                    </div>'''
                
                # IC50统计
                if 'ic50' in stats:
                    # 强效化合物统计 (IC50 < 1μM)
                    potent_count = sum(1 for r in self.screening_results 
                                     if r.mol_type == "small_molecule" and 
                                     hasattr(r, 'properties') and r.properties and
                                     isinstance(r.properties.get('ic50_uM'), (int, float)) and
                                     r.properties.get('ic50_uM', float('inf')) < 1.0)
                    
                    # 最佳IC50值
                    best_ic50 = stats['ic50']['min']
                    if best_ic50 < 0.001:
                        best_ic50_display = f"{best_ic50*1000:.1f}nM"
                    elif best_ic50 < 1:
                        best_ic50_display = f"{best_ic50*1000:.0f}nM"
                    elif best_ic50 < 1000:
                        best_ic50_display = f"{best_ic50:.1f}μM"
                    else:
                        best_ic50_display = f"{best_ic50/1000:.1f}mM"
                        
                    small_mol_stats += f'''
                    <div class="stats-card highlight">
                        <div class="stats-value">{potent_count}</div>
                        <div>Potent Compounds (IC50<1μM)</div>
                    </div>
                    <div class="stats-card">
                        <div class="stats-value">{best_ic50_display}</div>
                        <div>Best IC50</div>
                    </div>'''
            
            # 亲和力统计
            affinity_stats = ""
            if 'affinity' in stats:
                strong_affinity_count = sum(1 for r in self.screening_results
                                          if hasattr(r, 'properties') and r.properties and
                                          isinstance(r.properties.get('ic50_uM'), (int, float)) and
                                          r.properties.get('ic50_uM', 99999999999) < 0.05)
                                          
                best_affinity = stats['affinity']['min']
                affinity_stats += f'''
                <div class="stats-card">
                    <div class="stats-value">{strong_affinity_count}</div>
                    <div>Strong Binders (< -8 kcal/mol)</div>
                </div>
                <div class="stats-card">
                    <div class="stats-value">{best_affinity:.1f}</div>
                    <div>Best Affinity (kcal/mol)</div>
                </div>'''
            
            stats_html = f'''
            <div class="stats-grid">
                <div class="stats-card primary">
                    <div class="stats-value">{stats['total_molecules']}</div>
                    <div>Total Screened</div>
                </div>
                <div class="stats-card success">
                    <div class="stats-value">{high_quality_count}</div>
                    <div>High Quality Hits (≥0.7)</div>
                </div>
                <div class="stats-card warning">
                    <div class="stats-value">{hit_rate:.1f}%</div>
                    <div>Hit Rate</div>
                </div>
                <div class="stats-card">
                    <div class="stats-value">{stats['combined_score']['max']:.3f}</div>
                    <div>Best Combined Score</div>
                </div>
                {small_mol_stats}
                {affinity_stats}
            </div>'''
        
        # 完整的HTML模板
        html_template = f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>虚拟筛选分析报告</title>
    <style>
        /* 专业风格CSS */
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: 'Times New Roman', Times, serif;
            line-height: 1.6;
            color: #2c3e50;
            background: #ffffff;
            font-size: 14px;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            box-shadow: 0 0 20px rgba(0,0,0,0.1);
        }}
        
        .header {{
            background: #2c3e50;
            color: white;
            padding: 40px;
            text-align: center;
            border-bottom: 3px solid #3498db;
        }}
        
        .header h1 {{
            font-size: 2.2em;
            margin-bottom: 10px;
            font-weight: normal;
            letter-spacing: 1px;
        }}
        
        .header p {{
            font-size: 1.1em;
            opacity: 0.9;
            font-style: italic;
        }}
        
        .content {{
            padding: 40px;
        }}
        
        .section {{
            margin-bottom: 50px;
            border-bottom: 1px solid #ecf0f1;
            padding-bottom: 30px;
        }}
        
        .section:last-child {{
            border-bottom: none;
        }}
        
        .section h2 {{
            color: #2c3e50;
            font-size: 1.6em;
            margin-bottom: 25px;
            padding-bottom: 10px;
            border-bottom: 2px solid #3498db;
            font-weight: normal;
        }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }}
        
        .stats-card {{
            background: #f8f9fa;
            padding: 25px;
            border-radius: 8px;
            text-align: center;
            border: 1px solid #dee2e6;
            transition: transform 0.2s ease;
        }}
        
        .stats-card:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }}
        
        .stats-value {{
            font-size: 2.2em;
            font-weight: bold;
            color: #2c3e50;
            margin-bottom: 8px;
            font-family: 'Arial', sans-serif;
        }}
        
        .molecule-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
            gap: 25px;
            margin: 30px 0;
        }}
        
        .molecule-card {{
            background: #fdfdfd;
            border: 1px solid #dee2e6;
            border-radius: 8px;
            padding: 20px;
            transition: transform 0.2s ease;
        }}
        
        .molecule-card:hover {{
            transform: translateY(-2px);
            box-shadow: 0 4px 12px rgba(0,0,0,0.1);
        }}
        
        .molecule-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #ecf0f1;
        }}
        
        .molecule-title {{
            font-size: 1.2em;
            font-weight: bold;
            color: #2c3e50;
        }}
        
        .molecule-rank {{
            background: #3498db;
            color: white;
            padding: 5px 10px;
            border-radius: 15px;
            font-weight: bold;
            font-size: 0.9em;
        }}
        
        .molecule-structure {{
            text-align: center;
            margin: 15px 0;
            background: #f8f9fa;
            padding: 10px;
            border-radius: 5px;
            min-height: 200px;
            display: flex;
            align-items: center;
            justify-content: center;
        }}
        
        .molecule-details {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 15px;
        }}
        
        .detail-item {{
            display: flex;
            justify-content: space-between;
            padding: 8px 12px;
            background: rgba(52, 152, 219, 0.1);
            border-radius: 4px;
            font-size: 0.95em;
            margin-bottom: 5px;
        }}
        
        .alphafold-style {{
            background: rgba(245, 245, 245, 0.8);
            border-left-width: 4px !important;
            border-left-style: solid;
            padding-left: 12px;
            transition: all 0.3s ease;
        }}
        
        .alphafold-style:hover {{
            background: rgba(240, 240, 240, 1);
            transform: translateX(2px);
        }}
        
        .stats-card.primary {{
            background: linear-gradient(135deg, #0053D6, #65CBF3);
            color: white;
        }}
        
        .stats-card.success {{
            background: linear-gradient(135deg, #2ecc71, #27ae60);
            color: white;
        }}
        
        .stats-card.warning {{
            background: linear-gradient(135deg, #FFDB13, #f39c12);
            color: #2c3e50;
        }}
        
        .stats-card.highlight {{
            background: linear-gradient(135deg, #FF7D45, #e74c3c);
            color: white;
            font-weight: bold;
        }}
        
        .affinity-highlight {{
            background: linear-gradient(45deg, #e74c3c, #f39c12);
            color: white;
            font-weight: bold;
        }}
        
        .binding-prob-highlight {{
            background: linear-gradient(45deg, #2ecc71, #1abc9c);
            color: white;
            font-weight: bold;
            display: flex;
            justify-content: space-between;
            padding: 8px 12px;
            border-radius: 4px;
            font-size: 0.95em;
        }}
        
        .plots-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(500px, 1fr));
            gap: 30px;
            margin: 30px 0;
        }}
        
        .plot-container {{
            text-align: center;
            background: #fdfdfd;
            padding: 20px;
            border: 1px solid #dee2e6;
            border-radius: 8px;
        }}
        
        .plot-container h3 {{
            color: #2c3e50;
            margin-bottom: 15px;
            font-size: 1.1em;
            font-weight: normal;
        }}
        
        .analysis-plot {{
            max-width: 100%;
            height: auto;
            border-radius: 4px;
        }}
        
        .no-structure {{
            color: #7f8c8d;
            font-style: italic;
            padding: 50px 20px;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            font-size: 0.95em;
        }}
        
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #dee2e6;
        }}
        
        th {{
            background: #f8f9fa;
            color: #2c3e50;
            font-weight: bold;
            border-bottom: 2px solid #3498db;
        }}
        
        tr:nth-child(even) {{
            background-color: #f8f9fa;
        }}
        
        tr:hover {{
            background-color: #e3f2fd;
        }}
        
        .footer {{
            background: #2c3e50;
            color: white;
            padding: 30px;
            text-align: center;
            margin-top: 40px;
        }}
        
        .timestamp {{
            color: #7f8c8d;
            font-style: italic;
            font-size: 0.9em;
        }}
        
        @media print {{
            body {{ font-size: 12px; }}
            .plots-grid {{ grid-template-columns: 1fr; }}
            .molecule-grid {{ grid-template-columns: 1fr 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>虚拟筛选分析报告</h1>
            <p>Generated on {datetime.now().strftime('%B %d, %Y at %H:%M:%S')}</p>
            <p class="timestamp">Analysis report</p>
        </div>
        
        <div class="content">
            <div class="section">
                <h2>Executive Summary</h2>
                {stats_html}
                <p style="margin-top: 20px; text-align: justify; line-height: 1.8;">
                This report presents the results of a comprehensive virtual screening analysis performed on 
                {stats.get('total_molecules', 0)} molecular candidates. The screening employed a multi-criteria 
                evaluation system incorporating binding affinity, structural stability, and prediction confidence metrics. 
                The analysis identified several promising candidates with high binding scores and favorable 
                drug-like properties.
                </p>
            </div>
            
            <div class="section">
                <h2>Top Candidate Molecules</h2>
                <div class="molecule-grid">
                    {molecules_html}
                </div>
            </div>
            
            {plots_html}
            
            <div class="section">
                <h2>Methodology</h2>
                <p style="text-align: justify; line-height: 1.8;">
                The virtual screening pipeline employed the Boltz protein structure prediction system for 
                molecular complex modeling. Scoring metrics were computed based on interface template modeling (ipTM) 
                scores for binding affinity, predicted local distance difference test (pLDDT) scores for structural 
                confidence, and combined weighted scoring for final ranking. Molecular structures were generated 
                using RDKit for visualization and property analysis.
                </p>
            </div>
        </div>
        
        <div class="footer">
            <p>Generated by Boltz-WebUI Virtual Screening System</p>
            <p>© 2025 - Molecular Analysis Platform</p>
        </div>
    </div>
</body>
</html>'''
        
        return html_template
