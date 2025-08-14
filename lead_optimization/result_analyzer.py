# /data/boltz_webui/lead_optimization/result_analyzer.py

"""
result_analyzer.py

该模块负责lead optimization结果的分析和可视化：
1. OptimizationResultAnalyzer: 优化结果分析器
2. 统计分析功能
3. 可视化图表生成
4. HTML报告生成
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime
import pandas as pd
import numpy as np
from pathlib import Path

# 可视化库
try:
    import matplotlib
    matplotlib.use('Agg')  # 使用非交互式后端
    import matplotlib.pyplot as plt
    try:
        import seaborn as sns
        SEABORN_AVAILABLE = True
    except ImportError:
        SEABORN_AVAILABLE = False
    plt.style.use('default')
    MATPLOTLIB_AVAILABLE = True
    logger = logging.getLogger(__name__)
    logger.info(f"Matplotlib {matplotlib.__version__} 已成功加载")
except ImportError as e:
    MATPLOTLIB_AVAILABLE = False
    SEABORN_AVAILABLE = False
    logging.warning(f"Matplotlib未安装，可视化功能将受限: {e}")

try:
    from rdkit import Chem
    from rdkit.Chem import Draw, Descriptors
    from rdkit.Chem.Draw import rdMolDraw2D
    RDKIT_AVAILABLE = True
except ImportError:
    RDKIT_AVAILABLE = False

logger = logging.getLogger(__name__)

class OptimizationResult:
    """优化结果数据类"""
    
    def __init__(self, data: Dict[str, Any], rank: int = 0):
        self.rank = rank
        self.smiles = data.get('smiles', '')
        self.combined_score = data.get('combined_score', 0.0)
        self.scores = data.get('scores', {})
        self.properties = data.get('properties', {})
        self.generation_method = data.get('generation_method', '')
        self.transformation_rule = data.get('transformation_rule', '')
        self.boltz_metrics = data.get('boltz_metrics', {})
        
        # 从boltz_metrics中提取置信度等信息
        self.confidence_score = self.boltz_metrics.get('confidence', 0.0)
        self.iptm = self.boltz_metrics.get('iptm', 0.0)
        self.ptm = self.boltz_metrics.get('ptm', 0.0)
        
        # 提取结构文件路径
        result_files = self.boltz_metrics.get('result_files', {})
        self.structure_path = result_files.get('structure', '')
        self.metrics_path = result_files.get('metrics', '')

class OptimizationAnalyzer:
    """Lead optimization结果分析器"""
    
    def __init__(self, optimization_data: Dict[str, Any], output_dir: str):
        self.optimization_data = optimization_data
        self.output_dir = output_dir
        self.plots_dir = os.path.join(output_dir, "plots")
        
        # 创建图表目录
        os.makedirs(self.plots_dir, exist_ok=True)
        
        # 解析结果数据
        self.original_compound = optimization_data.get('original_compound', '')
        self.strategy = optimization_data.get('strategy', '')
        self.execution_time = optimization_data.get('execution_time', 0)
        self.statistics = optimization_data.get('statistics', {})
        
        # 转换候选化合物为结果对象
        self.results = []
        top_candidates = optimization_data.get('top_candidates', [])
        for i, candidate in enumerate(top_candidates):
            result = OptimizationResult(candidate, rank=i+1)
            self.results.append(result)
        
        # 转换为DataFrame用于分析
        self.df = self._results_to_dataframe()
        
        logger.info(f"优化结果分析器初始化完成，共 {len(self.results)} 个候选化合物")
    
    def _json_serializer(self, obj):
        """自定义JSON序列化器，处理numpy类型"""
        if isinstance(obj, np.integer):
            return int(obj)
        elif isinstance(obj, np.floating):
            return float(obj)
        elif isinstance(obj, np.ndarray):
            return obj.tolist()
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
    
    def _results_to_dataframe(self) -> pd.DataFrame:
        """将结果转换为DataFrame"""
        data = []
        for result in self.results:
            row = {
                "rank": result.rank,
                "smiles": result.smiles,
                "combined_score": result.combined_score,
                "confidence_score": result.confidence_score,
                "iptm": result.iptm,
                "ptm": result.ptm,
                "generation_method": result.generation_method,
                "transformation_rule": result.transformation_rule
            }
            
            # 添加评分信息
            if result.scores:
                row.update({
                    "affinity": result.scores.get("affinity", 0),
                    "binding_quality": result.scores.get("binding_quality", 0),
                    "drug_likeness": result.scores.get("drug_likeness", 0),
                    "synthetic_accessibility": result.scores.get("synthetic_accessibility", 0),
                    "novelty": result.scores.get("novelty", 0)
                })
            
            # 计算分子属性（如果没有提供的话）
            if not result.properties and RDKIT_AVAILABLE:
                calculated_props = self._calculate_molecular_properties(result.smiles)
                row.update(calculated_props)
            elif result.properties:
                row.update({
                    "molecular_weight": result.properties.get("molecular_weight", 0),
                    "logp": result.properties.get("logp", 0),
                    "hbd": result.properties.get("hbd", 0),
                    "hba": result.properties.get("hba", 0),
                    "tpsa": result.properties.get("tpsa", 0),
                    "rotatable_bonds": result.properties.get("rotatable_bonds", 0),
                    "aromatic_rings": result.properties.get("aromatic_rings", 0),
                    "heavy_atoms": result.properties.get("heavy_atoms", 0)
                })
            
            data.append(row)
        
        return pd.DataFrame(data)
    
    def _calculate_molecular_properties(self, smiles: str) -> Dict[str, float]:
        """计算分子属性"""
        try:
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                return {}
            
            return {
                "molecular_weight": Descriptors.MolWt(mol),
                "logp": Descriptors.MolLogP(mol),
                "hbd": Descriptors.NumHDonors(mol),
                "hba": Descriptors.NumHAcceptors(mol),
                "tpsa": Descriptors.TPSA(mol),
                "rotatable_bonds": Descriptors.NumRotatableBonds(mol),
                "aromatic_rings": Descriptors.NumAromaticRings(mol),
                "heavy_atoms": Descriptors.HeavyAtomCount(mol)
            }
        except Exception as e:
            logger.warning(f"计算分子属性失败 {smiles}: {e}")
            return {}
    
    def save_results_to_csv(self):
        """保存结果为CSV格式，类似virtual_screening"""
        try:
            # 保存完整结果
            complete_results_path = os.path.join(self.output_dir, "optimization_results_complete.csv")
            self.df.to_csv(complete_results_path, index=False, encoding='utf-8')
            logger.info(f"完整结果已保存: {complete_results_path}")
            
            # 保存Top结果
            top_n = min(10, len(self.df))
            top_results = self.df.head(top_n)
            top_results_path = os.path.join(self.output_dir, "top_candidates.csv")
            top_results.to_csv(top_results_path, index=False, encoding='utf-8')
            logger.info(f"Top {top_n} 结果已保存: {top_results_path}")
            
            # 保存优化摘要
            self._save_optimization_summary()
            
        except Exception as e:
            logger.error(f"保存CSV结果失败: {e}")
    
    def _save_optimization_summary(self):
        """保存优化摘要"""
        try:
            summary = {
                'original_compound': self.original_compound,
                'optimization_strategy': self.strategy,
                'execution_time_seconds': self.execution_time,
                'execution_time_minutes': self.execution_time / 60,
                'total_candidates': len(self.results),
                'successful_candidates': len([r for r in self.results if r.combined_score > 0.5]),
                'high_scoring_candidates': len([r for r in self.results if r.combined_score > 0.7]),
                'timestamp': datetime.now().isoformat(),
                'statistics': self.statistics
            }
            
            if len(self.results) > 0:
                scores = [r.combined_score for r in self.results]
                summary.update({
                    'top_score': max(scores),
                    'average_score': np.mean(scores),
                    'score_std': np.std(scores),
                    'score_range': max(scores) - min(scores)
                })
                
                # 添加置信度统计
                if 'confidence_score' in self.df.columns:
                    confidence_scores = self.df['confidence_score'].values
                    summary.update({
                        'average_confidence': np.mean(confidence_scores),
                        'confidence_std': np.std(confidence_scores),
                        'high_confidence_count': sum(confidence_scores > 0.7)
                    })
            
            summary_file = os.path.join(self.output_dir, "optimization_summary.json")
            with open(summary_file, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2, ensure_ascii=False, default=self._json_serializer)
            
            logger.info(f"优化摘要已保存: {summary_file}")
            
        except Exception as e:
            logger.error(f"保存优化摘要失败: {e}")
    
    def generate_optimization_plots(self) -> List[Dict[str, str]]:
        """生成优化分析图表"""
        if not MATPLOTLIB_AVAILABLE:
            logger.warning("Matplotlib不可用，跳过图表生成")
            return []
        
        plots = []
        
        try:
            # 设置图表样式
            plt.style.use('default')
            
            # 1. 评分分布图
            score_dist_plot = self._generate_score_distribution_plot()
            if score_dist_plot:
                plots.append(score_dist_plot)
            
            # 2. 置信度分析图  
            confidence_plot = self._generate_confidence_analysis_plot()
            if confidence_plot:
                plots.append(confidence_plot)
            
            # 3. 分子属性分析图
            property_plot = self._generate_property_analysis_plot()
            if property_plot:
                plots.append(property_plot)
            
            # 4. 优化策略效果图
            strategy_plot = self._generate_strategy_effectiveness_plot()
            if strategy_plot:
                plots.append(strategy_plot)
            
            # 5. Top候选化合物对比图
            top_candidates_plot = self._generate_top_candidates_comparison()
            if top_candidates_plot:
                plots.append(top_candidates_plot)
            
            logger.info(f"成功生成 {len(plots)} 个分析图表")
            return plots
            
        except Exception as e:
            logger.error(f"生成图表时出错: {e}")
            return plots
    
    def _generate_score_distribution_plot(self) -> Optional[Dict[str, str]]:
        """生成评分分布图"""
        try:
            fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 12))
            fig.suptitle('Lead Optimization Score Analysis', fontsize=16, fontweight='bold')
            
            # 综合评分分布
            scores = self.df['combined_score'].values
            ax1.hist(scores, bins=20, alpha=0.7, color='#3498db', edgecolor='black')
            ax1.set_xlabel('Combined Score')
            ax1.set_ylabel('Frequency')
            ax1.set_title('Combined Score Distribution')
            ax1.grid(True, alpha=0.3)
            
            # 各项评分对比（如果存在）
            score_columns = ['drug_likeness', 'synthetic_accessibility', 'novelty']
            available_scores = [col for col in score_columns if col in self.df.columns and self.df[col].sum() > 0]
            
            if available_scores:
                score_data = [self.df[col].values for col in available_scores]
                bp = ax2.boxplot(score_data, labels=[col.replace('_', ' ').title() for col in available_scores])
                ax2.set_title('Score Components Distribution')
                ax2.set_ylabel('Score Value')
                ax2.grid(True, alpha=0.3)
                
                # 设置颜色
                for patch in bp['boxes']:
                    patch.set_facecolor('#2ecc71')
                    patch.set_alpha(0.7)
            
            # 评分与排名的关系
            ax3.scatter(self.df['rank'], self.df['combined_score'], 
                       alpha=0.6, color='#e74c3c', s=60)
            ax3.set_xlabel('Rank')
            ax3.set_ylabel('Combined Score')
            ax3.set_title('Score vs Rank')
            ax3.grid(True, alpha=0.3)
            
            # Top 10 候选化合物评分
            top_10 = self.df.head(10)
            bars = ax4.bar(range(1, min(11, len(top_10) + 1)), 
                          top_10['combined_score'].values,
                          color='#f39c12', alpha=0.8)
            ax4.set_xlabel('Rank')
            ax4.set_ylabel('Combined Score')
            ax4.set_title('Top 10 Candidates Scores')
            ax4.set_xticks(range(1, min(11, len(top_10) + 1)))
            ax4.grid(True, alpha=0.3)
            
            # 添加数值标签
            for i, bar in enumerate(bars):
                height = bar.get_height()
                ax4.text(bar.get_x() + bar.get_width()/2., height,
                        f'{height:.3f}', ha='center', va='bottom')
            
            plt.tight_layout()
            
            plot_path = os.path.join(self.plots_dir, "score_distribution.png")
            plt.savefig(plot_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            return {
                "title": "Score Distribution Analysis",
                "description": "Distribution and comparison of optimization scores",
                "path": plot_path,
                "filename": "score_distribution.png"
            }
            
        except Exception as e:
            logger.error(f"生成评分分布图失败: {e}")
            return None
    
    def _generate_confidence_analysis_plot(self) -> Optional[Dict[str, str]]:
        """生成置信度分析图"""
        try:
            # 检查是否有有效的置信度数据
            has_confidence_data = ('confidence_score' in self.df.columns and 
                                 self.df['confidence_score'].sum() > 0 and 
                                 self.df['confidence_score'].var() > 0)
            
            has_boltz_metrics = ('iptm' in self.df.columns and 'ptm' in self.df.columns and
                               (self.df['iptm'].sum() > 0 or self.df['ptm'].sum() > 0))
            
            if not has_confidence_data and not has_boltz_metrics:
                logger.warning("没有有效的置信度数据，跳过置信度分析图生成")
                return None
            
            fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 12))
            fig.suptitle('Structural Confidence Analysis', fontsize=16, fontweight='bold')
            
            # 置信度分布
            if has_confidence_data:
                confidence_scores = self.df['confidence_score'].values
                ax1.hist(confidence_scores, bins=20, alpha=0.7, color='#9b59b6', edgecolor='black')
                ax1.set_xlabel('Confidence Score')
                ax1.set_ylabel('Frequency')
                ax1.set_title('Confidence Score Distribution')
                ax1.axvline(np.mean(confidence_scores), color='red', linestyle='--', 
                           label=f'Mean: {np.mean(confidence_scores):.3f}')
                ax1.legend()
                ax1.grid(True, alpha=0.3)
            else:
                ax1.text(0.5, 0.5, 'No valid confidence\ndata available', 
                        ha='center', va='center', transform=ax1.transAxes,
                        fontsize=12, bbox=dict(boxstyle="round", facecolor='wheat'))
                ax1.set_title('Confidence Score Distribution')
            
            # iPTM vs PTM散点图
            if has_boltz_metrics:
                scatter = ax2.scatter(self.df['iptm'], self.df['ptm'], 
                           c=self.df['combined_score'], cmap='viridis', 
                           alpha=0.7, s=60)
                ax2.set_xlabel('iPTM Score')
                ax2.set_ylabel('PTM Score')
                ax2.set_title('iPTM vs PTM Correlation')
                cbar = plt.colorbar(scatter, ax=ax2)
                cbar.set_label('Combined Score')
                ax2.grid(True, alpha=0.3)
            else:
                ax2.text(0.5, 0.5, 'No Boltz metrics\navailable', 
                        ha='center', va='center', transform=ax2.transAxes,
                        fontsize=12, bbox=dict(boxstyle="round", facecolor='wheat'))
                ax2.set_title('iPTM vs PTM Correlation')
            
            # 置信度vs综合评分
            if has_confidence_data:
                ax3.scatter(self.df['confidence_score'], self.df['combined_score'],
                           alpha=0.6, color='#1abc9c', s=60)
                ax3.set_xlabel('Confidence Score')
                ax3.set_ylabel('Combined Score')
                ax3.set_title('Confidence vs Combined Score')
                
                # 添加趋势线（只有当数据有变化时）
                if len(self.df) > 1 and self.df['confidence_score'].var() > 1e-10:
                    try:
                        z = np.polyfit(self.df['confidence_score'], self.df['combined_score'], 1)
                        p = np.poly1d(z)
                        ax3.plot(self.df['confidence_score'], p(self.df['confidence_score']), 
                                "r--", alpha=0.8, label='Trend')
                        ax3.legend()
                    except (np.linalg.LinAlgError, np.RankWarning):
                        # SVD收敛失败时跳过趋势线
                        logger.debug("趋势线计算失败，跳过")
                        pass
                ax3.grid(True, alpha=0.3)
            else:
                ax3.text(0.5, 0.5, 'No confidence data\nfor correlation analysis', 
                        ha='center', va='center', transform=ax3.transAxes,
                        fontsize=12, bbox=dict(boxstyle="round", facecolor='wheat'))
                ax3.set_title('Confidence vs Combined Score')
            
            # 置信度分级统计
            if has_confidence_data:
                confidence_bins = pd.cut(self.df['confidence_score'], 
                                       bins=[0, 0.5, 0.7, 0.9, 1.0],
                                       labels=['Low (<0.5)', 'Medium (0.5-0.7)', 
                                              'High (0.7-0.9)', 'Very High (>0.9)'])
                confidence_counts = confidence_bins.value_counts()
                
                colors = ['#e74c3c', '#f39c12', '#2ecc71', '#3498db']
                wedges, texts, autotexts = ax4.pie(confidence_counts.values, 
                                                  labels=confidence_counts.index,
                                                  autopct='%1.1f%%',
                                                  colors=colors,
                                                  startangle=90)
                ax4.set_title('Confidence Level Distribution')
            else:
                ax4.text(0.5, 0.5, 'No confidence data\nfor level analysis', 
                        ha='center', va='center', transform=ax4.transAxes,
                        fontsize=12, bbox=dict(boxstyle="round", facecolor='wheat'))
                ax4.set_title('Confidence Level Distribution')
            
            plt.tight_layout()
            
            plot_path = os.path.join(self.plots_dir, "confidence_analysis.png")
            plt.savefig(plot_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            return {
                "title": "Confidence Analysis",
                "description": "Analysis of structural prediction confidence",
                "path": plot_path,
                "filename": "confidence_analysis.png"
            }
            
        except Exception as e:
            logger.error(f"生成置信度分析图失败: {e}")
            return None
    
    def _generate_property_analysis_plot(self) -> Optional[Dict[str, str]]:
        """生成分子属性分析图"""
        try:
            fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(15, 12))
            fig.suptitle('Molecular Properties Analysis', fontsize=16, fontweight='bold')
            
            # 分子量分布
            if 'molecular_weight' in self.df.columns:
                mw_data = self.df['molecular_weight'].values
                ax1.hist(mw_data, bins=20, alpha=0.7, color='#34495e', edgecolor='black')
                ax1.set_xlabel('Molecular Weight (Da)')
                ax1.set_ylabel('Frequency')
                ax1.set_title('Molecular Weight Distribution')
                ax1.axvline(500, color='red', linestyle='--', label='Lipinski Limit (500)')
                ax1.legend()
                ax1.grid(True, alpha=0.3)
            
            # LogP vs 分子量
            if 'logp' in self.df.columns and 'molecular_weight' in self.df.columns:
                scatter = ax2.scatter(self.df['molecular_weight'], self.df['logp'],
                                     c=self.df['combined_score'], cmap='plasma',
                                     alpha=0.7, s=60)
                ax2.set_xlabel('Molecular Weight (Da)')
                ax2.set_ylabel('LogP')
                ax2.set_title('Molecular Weight vs LogP')
                ax2.axhline(5, color='red', linestyle='--', alpha=0.7, label='Lipinski Limit')
                ax2.axvline(500, color='red', linestyle='--', alpha=0.7)
                ax2.legend()
                cbar = plt.colorbar(scatter, ax=ax2)
                cbar.set_label('Combined Score')
                ax2.grid(True, alpha=0.3)
            
            # Lipinski规则符合性
            lipinski_properties = ['molecular_weight', 'logp', 'hbd', 'hba']
            available_lipinski = [prop for prop in lipinski_properties if prop in self.df.columns]
            
            if len(available_lipinski) >= 3:
                violations = []
                for _, row in self.df.iterrows():
                    violation_count = 0
                    if 'molecular_weight' in row and row['molecular_weight'] > 500:
                        violation_count += 1
                    if 'logp' in row and row['logp'] > 5:
                        violation_count += 1
                    if 'hbd' in row and row['hbd'] > 5:
                        violation_count += 1
                    if 'hba' in row and row['hba'] > 10:
                        violation_count += 1
                    violations.append(violation_count)
                
                violation_counts = pd.Series(violations).value_counts().sort_index()
                colors = ['#2ecc71', '#f39c12', '#e67e22', '#e74c3c', '#8e44ad']
                bars = ax3.bar(violation_counts.index, violation_counts.values,
                              color=[colors[i] if i < len(colors) else colors[-1] 
                                    for i in violation_counts.index])
                ax3.set_xlabel('Number of Lipinski Violations')
                ax3.set_ylabel('Number of Compounds')
                ax3.set_title('Lipinski Rule Compliance')
                ax3.grid(True, alpha=0.3)
                
                # 添加数值标签
                for bar in bars:
                    height = bar.get_height()
                    ax3.text(bar.get_x() + bar.get_width()/2., height,
                            f'{int(height)}', ha='center', va='bottom')
            
            # TPSA分布
            if 'tpsa' in self.df.columns:
                tpsa_data = self.df['tpsa'].values
                ax4.hist(tpsa_data, bins=20, alpha=0.7, color='#16a085', edgecolor='black')
                ax4.set_xlabel('TPSA (Ų)')
                ax4.set_ylabel('Frequency')
                ax4.set_title('Topological Polar Surface Area Distribution')
                ax4.axvline(140, color='red', linestyle='--', label='Permeability Limit (140)')
                ax4.legend()
                ax4.grid(True, alpha=0.3)
            
            plt.tight_layout()
            
            plot_path = os.path.join(self.plots_dir, "property_analysis.png")
            plt.savefig(plot_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            return {
                "title": "Molecular Properties Analysis",
                "description": "Analysis of drug-like properties and Lipinski compliance",
                "path": plot_path,
                "filename": "property_analysis.png"
            }
            
        except Exception as e:
            logger.error(f"生成分子属性分析图失败: {e}")
            return None
    
    def _generate_strategy_effectiveness_plot(self) -> Optional[Dict[str, str]]:
        """生成优化策略效果分析图"""
        try:
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6))
            fig.suptitle(f'Optimization Strategy Analysis: {self.strategy.title()}', 
                        fontsize=16, fontweight='bold')
            
            # 生成方法统计
            if 'generation_method' in self.df.columns:
                method_counts = self.df['generation_method'].value_counts()
                colors = plt.cm.Set3(np.linspace(0, 1, len(method_counts)))
                
                wedges, texts, autotexts = ax1.pie(method_counts.values, 
                                                  labels=method_counts.index,
                                                  autopct='%1.1f%%',
                                                  colors=colors,
                                                  startangle=90)
                ax1.set_title('Generation Methods Distribution')
            
            # 转换规则效果分析
            if 'transformation_rule' in self.df.columns:
                rule_scores = self.df.groupby('transformation_rule')['combined_score'].agg(['mean', 'count'])
                rule_scores = rule_scores[rule_scores['count'] >= 2].sort_values('mean', ascending=False)
                
                if len(rule_scores) > 0:
                    top_rules = rule_scores.head(10)
                    bars = ax2.bar(range(len(top_rules)), top_rules['mean'], 
                                  color='#3498db', alpha=0.8)
                    ax2.set_xlabel('Transformation Rules')
                    ax2.set_ylabel('Average Combined Score')
                    ax2.set_title('Top Transformation Rules by Performance')
                    ax2.set_xticks(range(len(top_rules)))
                    ax2.set_xticklabels([f'Rule {i+1}' for i in range(len(top_rules))], 
                                       rotation=45)
                    ax2.grid(True, alpha=0.3)
                    
                    # 添加数值标签
                    for i, bar in enumerate(bars):
                        height = bar.get_height()
                        ax2.text(bar.get_x() + bar.get_width()/2., height,
                                f'{height:.3f}', ha='center', va='bottom')
                else:
                    ax2.text(0.5, 0.5, 'Insufficient data for\ntransformation rule analysis',
                            ha='center', va='center', transform=ax2.transAxes,
                            fontsize=12, bbox=dict(boxstyle="round", facecolor='wheat'))
                    ax2.set_title('Transformation Rules Analysis')
            
            plt.tight_layout()
            
            plot_path = os.path.join(self.plots_dir, "strategy_effectiveness.png")
            plt.savefig(plot_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            return {
                "title": "Strategy Effectiveness Analysis",
                "description": f"Analysis of {self.strategy} optimization strategy effectiveness",
                "path": plot_path,
                "filename": "strategy_effectiveness.png"
            }
            
        except Exception as e:
            logger.error(f"生成策略效果分析图失败: {e}")
            return None
    
    def _generate_top_candidates_comparison(self) -> Optional[Dict[str, str]]:
        """生成Top候选化合物对比图"""
        try:
            top_n = min(10, len(self.results))
            top_candidates = self.df.head(top_n)
            
            fig, ((ax1, ax2), (ax3, ax4)) = plt.subplots(2, 2, figsize=(16, 12))
            fig.suptitle(f'Top {top_n} Candidates Detailed Comparison', 
                        fontsize=16, fontweight='bold')
            
            # 各项评分对比
            score_columns = ['drug_likeness', 'synthetic_accessibility', 'novelty']
            available_scores = [col for col in score_columns if col in top_candidates.columns]
            
            if available_scores:
                x = np.arange(top_n)
                width = 0.25
                
                for i, score_col in enumerate(available_scores):
                    bars = ax1.bar(x + i*width, top_candidates[score_col], 
                                  width, label=score_col.replace('_', ' ').title(),
                                  alpha=0.8)
                
                ax1.set_xlabel('Candidate Rank')
                ax1.set_ylabel('Score Value')
                ax1.set_title('Score Components Comparison')
                ax1.set_xticks(x + width)
                ax1.set_xticklabels([f'#{i+1}' for i in range(top_n)])
                ax1.legend()
                ax1.grid(True, alpha=0.3)
            
            # 分子属性比较
            property_columns = ['molecular_weight', 'logp', 'tpsa']
            available_properties = [col for col in property_columns if col in top_candidates.columns]
            
            if len(available_properties) >= 2:
                # 标准化属性值用于比较
                normalized_data = top_candidates[available_properties].copy()
                for col in available_properties:
                    max_val = normalized_data[col].max()
                    if max_val > 0:
                        normalized_data[col] = normalized_data[col] / max_val
                
                x = np.arange(top_n)
                width = 0.25
                
                for i, prop_col in enumerate(available_properties):
                    ax2.bar(x + i*width, normalized_data[prop_col], 
                           width, label=prop_col.replace('_', ' ').title(),
                           alpha=0.8)
                
                ax2.set_xlabel('Candidate Rank')
                ax2.set_ylabel('Normalized Value')
                ax2.set_title('Molecular Properties Comparison (Normalized)')
                ax2.set_xticks(x + width)
                ax2.set_xticklabels([f'#{i+1}' for i in range(top_n)])
                ax2.legend()
                ax2.grid(True, alpha=0.3)
            
            # 综合评分趋势
            ax3.plot(range(1, top_n + 1), top_candidates['combined_score'], 
                    'o-', linewidth=2, markersize=8, color='#e74c3c')
            ax3.set_xlabel('Candidate Rank')
            ax3.set_ylabel('Combined Score')
            ax3.set_title('Combined Score Trend')
            ax3.grid(True, alpha=0.3)
            ax3.set_xticks(range(1, top_n + 1))
            
            # 为每个点添加数值标签
            for i, score in enumerate(top_candidates['combined_score']):
                ax3.annotate(f'{score:.3f}', (i+1, score), 
                            textcoords="offset points", xytext=(0,10), ha='center')
            
            # 置信度vs评分散点图
            if 'confidence_score' in top_candidates.columns and top_candidates['confidence_score'].var() > 1e-10:
                scatter = ax4.scatter(top_candidates['confidence_score'], 
                                     top_candidates['combined_score'],
                                     c=range(1, top_n + 1), cmap='viridis_r',
                                     s=100, alpha=0.7)
                ax4.set_xlabel('Confidence Score')
                ax4.set_ylabel('Combined Score')
                ax4.set_title('Confidence vs Combined Score (Top Candidates)')
                
                # 添加排名标签
                for i, (conf, comb) in enumerate(zip(top_candidates['confidence_score'],
                                                    top_candidates['combined_score'])):
                    ax4.annotate(f'#{i+1}', (conf, comb), 
                                textcoords="offset points", xytext=(5,5), ha='left')
                
                cbar = plt.colorbar(scatter, ax=ax4)
                cbar.set_label('Rank')
                ax4.grid(True, alpha=0.3)
            else:
                # 如果没有有效的置信度数据，显示排名vs评分的柱状图
                bars = ax4.bar(range(1, top_n + 1), top_candidates['combined_score'],
                              color='#9b59b6', alpha=0.8)
                ax4.set_xlabel('Candidate Rank')
                ax4.set_ylabel('Combined Score')
                ax4.set_title('Top Candidates Score Distribution')
                ax4.set_xticks(range(1, top_n + 1))
                ax4.grid(True, alpha=0.3)
                
                # 添加数值标签
                for i, bar in enumerate(bars):
                    height = bar.get_height()
                    ax4.text(bar.get_x() + bar.get_width()/2., height,
                            f'{height:.3f}', ha='center', va='bottom')
            
            plt.tight_layout()
            
            plot_path = os.path.join(self.plots_dir, "top_candidates_comparison.png")
            plt.savefig(plot_path, dpi=300, bbox_inches='tight')
            plt.close()
            
            return {
                "title": "Top Candidates Detailed Comparison",
                "description": f"Detailed comparison of top {top_n} optimized candidates",
                "path": plot_path,
                "filename": "top_candidates_comparison.png"
            }
            
        except Exception as e:
            logger.error(f"生成Top候选化合物对比图失败: {e}")
            return None
    
    def get_analysis_summary(self) -> Dict[str, Any]:
        """获取分析摘要"""
        summary = {
            "original_compound": self.original_compound,
            "optimization_strategy": self.strategy,
            "execution_time_seconds": self.execution_time,
            "execution_time_minutes": self.execution_time / 60,
            "total_candidates": len(self.results),
            "statistics": self.statistics.copy()
        }
        
        if len(self.results) > 0:
            scores = [r.combined_score for r in self.results]
            summary.update({
                "top_score": max(scores),
                "average_score": np.mean(scores),
                "score_std": np.std(scores),
                "score_range": max(scores) - min(scores)
            })
            
            # 添加置信度统计
            if 'confidence_score' in self.df.columns:
                confidence_scores = self.df['confidence_score'].values
                summary.update({
                    "average_confidence": np.mean(confidence_scores),
                    "confidence_std": np.std(confidence_scores),
                    "high_confidence_count": sum(confidence_scores > 0.7)
                })
        
        return summary

    def generate_html_report(self, results: Dict[str, Any]) -> str:
        """生成HTML报告"""
        try:
            html_template = self._create_html_template()
            
            # 准备数据
            report_data = {
                'title': f'Lead Optimization Report - {self.strategy.title()}',
                'original_compound': self.original_compound,
                'strategy': self.strategy,
                'execution_time': f"{self.execution_time / 60:.1f} minutes",
                'total_candidates': len(self.results),
                'statistics': self.statistics,
                'top_candidates': self.results[:10],  # Top 10
                'plots': []
            }
            
            # 添加图表
            plots = self.generate_optimization_plots()
            for plot in plots:
                if plot and 'filename' in plot:
                    report_data['plots'].append({
                        'title': plot['title'],
                        'description': plot['description'],
                        'filename': plot['filename']
                    })
            
            # 生成HTML内容
            compounds_html = self._generate_compounds_html(report_data['top_candidates'])
            plots_html = self._generate_plots_html(report_data['plots'])
            
            html_content = html_template.format(
                title=report_data['title'],
                original_compound=report_data['original_compound'],
                strategy=report_data['strategy'],
                execution_time=report_data['execution_time'],
                total_candidates=report_data['total_candidates'],
                timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                compounds_html=compounds_html,
                plots_html=plots_html
            )
            
            # 保存HTML文件
            html_path = os.path.join(self.output_dir, "optimization_report.html")
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            logger.info(f"HTML报告已生成: {html_path}")
            return html_path
            
        except Exception as e:
            logger.error(f"生成HTML报告失败: {e}")
            raise
    
    def _generate_compounds_html(self, compounds: List) -> str:
        """生成候选化合物的HTML"""
        html_parts = []
        
        for i, compound in enumerate(compounds[:10]):  # Top 10
            is_best = i == 0
            rank_class = "best" if is_best else ""
            
            # 尝试生成分子结构图
            structure_html = self._generate_molecule_structure(compound.smiles, f"compound_{i+1}")
            
            compound_html = f'''
            <div class="compound {rank_class}">
                <div class="compound-header">
                    <span class="rank-badge {rank_class}">#{compound.rank}</span>
                    <span class="score">{compound.combined_score:.4f}</span>
                </div>
                
                <div class="smiles">{compound.smiles}</div>
                
                {structure_html}
                
                <div class="properties">
                    <p><strong>Generation Method:</strong> {compound.generation_method or 'N/A'}</p>
                    <p><strong>Transformation Rule:</strong> {compound.transformation_rule or 'N/A'}</p>
                </div>
            </div>
            '''
            html_parts.append(compound_html)
        
        return '\n'.join(html_parts)
    
    def _generate_plots_html(self, plots: List[Dict]) -> str:
        """生成图表的HTML"""
        html_parts = []
        
        for plot in plots:
            plot_html = f'''
            <div class="plot">
                <h3>{plot['title']}</h3>
                <p>{plot['description']}</p>
                <img src="plots/{plot['filename']}" alt="{plot['title']}">
            </div>
            '''
            html_parts.append(plot_html)
        
        return '\n'.join(html_parts)
    
    def _generate_molecule_structure(self, smiles: str, filename: str) -> str:
        """生成分子结构图"""
        if not RDKIT_AVAILABLE:
            return '<p><em>RDKit not available for structure visualization</em></p>'
        
        try:
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                return '<p><em>Invalid SMILES for structure generation</em></p>'
            
            # 生成分子图片
            img = Draw.MolToImage(mol, size=(400, 300))
            
            # 保存图片
            img_dir = os.path.join(self.output_dir, "structures")
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{filename}.png")
            img.save(img_path)
            
            return f'<img src="structures/{filename}.png" alt="Molecular Structure" style="max-width: 400px; margin: 10px 0;">'
            
        except Exception as e:
            logger.warning(f"生成分子结构图失败: {e}")
            return '<p><em>Structure generation failed</em></p>'
    
    def _create_html_template(self) -> str:
        """创建HTML模板"""
        return '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
            background-color: #f5f7fa;
        }}
        
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
            text-align: center;
        }}
        
        .header h1 {{
            margin: 0;
            font-size: 2.5em;
        }}
        
        .summary {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        
        .summary-card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        
        .summary-card h3 {{
            margin-top: 0;
            color: #667eea;
        }}
        
        .compounds-section {{
            background: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }}
        
        .compound {{
            border: 2px solid #e1e8ed;
            border-radius: 10px;
            margin: 20px 0;
            padding: 20px;
            transition: all 0.3s ease;
        }}
        
        .compound:hover {{
            border-color: #667eea;
            box-shadow: 0 4px 20px rgba(102, 126, 234, 0.1);
        }}
        
        .compound.best {{
            border-color: #28a745;
            background: linear-gradient(45deg, rgba(40, 167, 69, 0.05), rgba(40, 167, 69, 0.02));
        }}
        
        .compound-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }}
        
        .rank-badge {{
            background: #667eea;
            color: white;
            padding: 5px 15px;
            border-radius: 20px;
            font-weight: bold;
        }}
        
        .rank-badge.best {{
            background: #28a745;
        }}
        
        .score {{
            font-size: 1.5em;
            font-weight: bold;
            color: #28a745;
        }}
        
        .smiles {{
            font-family: 'Courier New', monospace;
            background: #f8f9fa;
            padding: 10px;
            border-radius: 5px;
            word-break: break-all;
            margin: 10px 0;
        }}
        
        .plots-section {{
            background: white;
            padding: 30px;
            border-radius: 10px;
            margin-bottom: 30px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }}
        
        .plot {{
            margin: 30px 0;
            text-align: center;
        }}
        
        .plot img {{
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.1);
        }}
        
        .plot h3 {{
            color: #667eea;
            margin-bottom: 10px;
        }}
        
        .plot p {{
            color: #666;
            margin-bottom: 20px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>{title}</h1>
        <p>Generated on {timestamp}</p>
    </div>
    
    <div class="summary">
        <div class="summary-card">
            <h3>🎯 Original Compound</h3>
            <div class="smiles">{original_compound}</div>
        </div>
        
        <div class="summary-card">
            <h3>🔬 Strategy</h3>
            <p><strong>{strategy}</strong></p>
        </div>
        
        <div class="summary-card">
            <h3>⏱️ Execution Time</h3>
            <p><strong>{execution_time}</strong></p>
        </div>
        
        <div class="summary-card">
            <h3>📊 Results</h3>
            <p><strong>{total_candidates}</strong> candidates generated</p>
        </div>
    </div>
    
    <div class="compounds-section">
        <h2>🏆 Top Candidate Compounds</h2>
        {compounds_html}
    </div>
    
    <div class="plots-section">
        <h2>📈 Analysis Plots</h2>
        {plots_html}
    </div>
</body>
</html>
'''
