# /Boltz-WebUI/virtual_screening/result_analyzer.py

"""
result_analyzer.py

该模块负责虚拟筛选结果的分析和可视化：
1. ResultAnalyzer: 结果分析器
2. 统计分析功能
3. 可视化图表生成
4. HTML报告生成
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional
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
    print(f"Matplotlib {matplotlib.__version__} 已成功加载")
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

from screening_engine import ScreeningResult

logger = logging.getLogger(__name__)

class ResultAnalyzer:
    """虚拟筛选结果分析器"""
    
    def __init__(self, results: List[ScreeningResult], output_dir: str):
        self.results = results
        self.output_dir = output_dir
        self.plots_dir = os.path.join(output_dir, "plots")
        
        # 创建图表目录
        os.makedirs(self.plots_dir, exist_ok=True)
        
        # 转换为DataFrame用于分析
        self.df = self._results_to_dataframe()
        
        logger.info(f"结果分析器初始化完成，共 {len(results)} 个结果")
    
    def _results_to_dataframe(self) -> pd.DataFrame:
        """将结果转换为DataFrame"""
        data = []
        for result in self.results:
            row = {
                "rank": result.rank,
                "molecule_id": result.molecule_id,
                "molecule_name": result.molecule_name,
                "mol_type": result.mol_type,
                "sequence": result.sequence,  # 添加序列字段
                "combined_score": result.combined_score,
                "binding_score": result.binding_score,
                "structural_score": result.structural_score,
                "confidence_score": result.confidence_score,
                "sequence_length": len(result.sequence),
                "molecular_weight": result.properties.get("molecular_weight", 0) if result.properties else 0
            }
            
            # 添加分子特异性属性
            if result.properties:
                if result.mol_type == "peptide":
                    row.update({
                        "hydrophobicity": result.properties.get("hydrophobicity", 0),
                        "net_charge": result.properties.get("net_charge", 0),
                        "aromatic_residues": result.properties.get("aromatic_residues", 0)
                    })
                elif result.mol_type == "small_molecule":
                    row.update({
                        "logp": result.properties.get("logp", 0),
                        "hbd": result.properties.get("hbd", 0),
                        "hba": result.properties.get("hba", 0),
                        "rotatable_bonds": result.properties.get("rotatable_bonds", 0),
                        "lipinski_violations": result.properties.get("lipinski_violations", 0)
                    })
            
            data.append(row)
        
        return pd.DataFrame(data)
    
    def generate_plots(self) -> Dict[str, str]:
        """生成所有分析图表"""
        plots = {}
        
        if not MATPLOTLIB_AVAILABLE:
            logger.warning("Matplotlib未安装，跳过图表生成")
            return plots
            
        # 检查是否有数据
        if self.df.empty:
            logger.warning("没有数据，跳过图表生成")
            return plots
        
        try:
            # 1. 评分分布图
            plots["score_distribution"] = self._plot_score_distribution()
            
            # 2. 评分相关性热图
            plots["score_correlation"] = self._plot_score_correlation()
            
            # 3. 分子类型分布
            plots["molecule_type_distribution"] = self._plot_molecule_type_distribution()
            
            # 4. 顶部结果详细分析
            plots["top_hits_analysis"] = self._plot_top_hits_analysis()
            
            # 5. 分子属性分布（按类型）
            if not self.df.empty and "mol_type" in self.df.columns and "peptide" in self.df["mol_type"].values:
                plots["peptide_properties"] = self._plot_peptide_properties()
            
            if not self.df.empty and "mol_type" in self.df.columns and "small_molecule" in self.df["mol_type"].values:
                plots["small_molecule_properties"] = self._plot_small_molecule_properties()
            
            # 6. 排名vs评分趋势
            plots["ranking_trend"] = self._plot_ranking_trend()
            
            logger.info(f"生成了 {len(plots)} 个分析图表")
            return plots
            
        except Exception as e:
            logger.error(f"生成图表时发生错误: {e}")
            return plots
    
    def _plot_score_distribution(self) -> str:
        """Plot score distribution charts"""
        fig, axes = plt.subplots(2, 2, figsize=(15, 12))
        fig.suptitle("Screening Score Distribution Analysis", fontsize=16, fontweight='bold')
        
        # Combined score distribution
        axes[0, 0].hist(self.df["combined_score"], bins=30, alpha=0.7, color='skyblue', edgecolor='black')
        axes[0, 0].set_title("Combined Score Distribution")
        axes[0, 0].set_xlabel("Combined Score")
        axes[0, 0].set_ylabel("Frequency")
        axes[0, 0].grid(True, alpha=0.3)
        
        # Binding affinity score distribution
        axes[0, 1].hist(self.df["binding_score"], bins=30, alpha=0.7, color='lightcoral', edgecolor='black')
        axes[0, 1].set_title("Binding Affinity Score Distribution")
        axes[0, 1].set_xlabel("Binding Affinity Score")
        axes[0, 1].set_ylabel("Frequency")
        axes[0, 1].grid(True, alpha=0.3)
        
        # Structural stability score distribution
        axes[1, 0].hist(self.df["structural_score"], bins=30, alpha=0.7, color='lightgreen', edgecolor='black')
        axes[1, 0].set_title("Structural Stability Score Distribution")
        axes[1, 0].set_xlabel("Structural Stability Score")
        axes[1, 0].set_ylabel("Frequency")
        axes[1, 0].grid(True, alpha=0.3)
        
        # Confidence score distribution
        axes[1, 1].hist(self.df["confidence_score"], bins=30, alpha=0.7, color='gold', edgecolor='black')
        axes[1, 1].set_title("Confidence Score Distribution")
        axes[1, 1].set_xlabel("Confidence Score")
        axes[1, 1].set_ylabel("Frequency")
        axes[1, 1].grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = os.path.join(self.plots_dir, "score_distribution.png")
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        return output_path
    
    def _plot_score_correlation(self) -> str:
        """Plot score correlation heatmap"""
        score_columns = ["combined_score", "binding_score", "structural_score", "confidence_score"]
        correlation_matrix = self.df[score_columns].corr()
        
        plt.figure(figsize=(10, 8))
        sns.heatmap(correlation_matrix, annot=True, cmap='coolwarm', center=0,
                   square=True, linewidths=0.5, cbar_kws={"shrink": 0.8})
        plt.title("Score Correlation Heatmap", fontsize=16, fontweight='bold')
        plt.tight_layout()
        
        output_path = os.path.join(self.plots_dir, "score_correlation.png")
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        return output_path
    
    def _plot_molecule_type_distribution(self) -> str:
        """Plot molecule type distribution"""
        fig, axes = plt.subplots(1, 2, figsize=(15, 6))
        
        # Molecule type count distribution
        type_counts = self.df["mol_type"].value_counts()
        axes[0].pie(type_counts.values, labels=type_counts.index, autopct='%1.1f%%', 
                   colors=['lightblue', 'lightcoral'], startangle=90)
        axes[0].set_title("Molecule Type Distribution")
        
        # Score boxplot by molecule type
        self.df.boxplot(column='combined_score', by='mol_type', ax=axes[1])
        axes[1].set_title("Combined Score Distribution by Molecule Type")
        axes[1].set_xlabel("Molecule Type")
        axes[1].set_ylabel("Combined Score")
        axes[1].grid(True, alpha=0.3)
        
        plt.suptitle("Molecule Type Analysis", fontsize=16, fontweight='bold')
        plt.tight_layout()
        
        output_path = os.path.join(self.plots_dir, "molecule_type_distribution.png")
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        return output_path
    
    def _plot_top_hits_analysis(self) -> str:
        """Plot detailed analysis of top hit molecules"""
        top_n = min(20, len(self.df))
        top_hits = self.df.head(top_n)
        
        fig, axes = plt.subplots(2, 2, figsize=(16, 12))
        fig.suptitle(f"Top {top_n} Hit Molecules Detailed Analysis", fontsize=16, fontweight='bold')
        
        # 1. Score comparison for top molecules
        x_pos = range(len(top_hits))
        width = 0.2
        
        axes[0, 0].bar([x - width*1.5 for x in x_pos], top_hits["binding_score"], 
                      width, label="Binding Affinity", alpha=0.8)
        axes[0, 0].bar([x - width*0.5 for x in x_pos], top_hits["structural_score"], 
                      width, label="Structural Stability", alpha=0.8)
        axes[0, 0].bar([x + width*0.5 for x in x_pos], top_hits["confidence_score"], 
                      width, label="Confidence", alpha=0.8)
        axes[0, 0].bar([x + width*1.5 for x in x_pos], top_hits["combined_score"], 
                      width, label="Combined Score", alpha=0.8)
        
        axes[0, 0].set_xlabel("Molecule Rank")
        axes[0, 0].set_ylabel("Score")
        axes[0, 0].set_title("Score Comparison for Top Molecules")
        axes[0, 0].legend()
        axes[0, 0].set_xticks(x_pos)
        axes[0, 0].set_xticklabels([f"#{i+1}" for i in x_pos], rotation=45)
        axes[0, 0].grid(True, alpha=0.3)
        
        # 2. Molecular weight distribution
        axes[0, 1].scatter(top_hits["molecular_weight"], top_hits["combined_score"], 
                          c=top_hits["rank"], cmap='viridis', s=60, alpha=0.7)
        axes[0, 1].set_xlabel("Molecular Weight")
        axes[0, 1].set_ylabel("Combined Score")
        axes[0, 1].set_title("Molecular Weight vs Combined Score")
        axes[0, 1].grid(True, alpha=0.3)
        
        # 3. Sequence length distribution
        axes[1, 0].scatter(top_hits["sequence_length"], top_hits["combined_score"], 
                          c=top_hits["rank"], cmap='viridis', s=60, alpha=0.7)
        axes[1, 0].set_xlabel("Sequence Length")
        axes[1, 0].set_ylabel("Combined Score")
        axes[1, 0].set_title("Sequence Length vs Combined Score")
        axes[1, 0].grid(True, alpha=0.3)
        
        # 4. Score trend
        axes[1, 1].plot(top_hits["rank"], top_hits["combined_score"], 'o-', linewidth=2, markersize=6)
        axes[1, 1].set_xlabel("Rank")
        axes[1, 1].set_ylabel("Combined Score")
        axes[1, 1].set_title("Top Molecules Score Trend")
        axes[1, 1].grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = os.path.join(self.plots_dir, "top_hits_analysis.png")
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        return output_path
    
    def _plot_peptide_properties(self) -> str:
        """Plot peptide properties analysis"""
        peptide_df = self.df[self.df["mol_type"] == "peptide"]
        
        if peptide_df.empty:
            return ""
        
        fig, axes = plt.subplots(2, 2, figsize=(15, 12))
        fig.suptitle("Peptide Properties Analysis", fontsize=16, fontweight='bold')
        
        # Hydrophobicity distribution
        if "hydrophobicity" in peptide_df.columns:
            axes[0, 0].hist(peptide_df["hydrophobicity"], bins=20, alpha=0.7, color='lightblue', edgecolor='black')
            axes[0, 0].set_title("Hydrophobicity Distribution")
            axes[0, 0].set_xlabel("Hydrophobicity Index")
            axes[0, 0].set_ylabel("Frequency")
            axes[0, 0].grid(True, alpha=0.3)
        
        # Net charge distribution
        if "net_charge" in peptide_df.columns:
            axes[0, 1].hist(peptide_df["net_charge"], bins=20, alpha=0.7, color='lightcoral', edgecolor='black')
            axes[0, 1].set_title("Net Charge Distribution")
            axes[0, 1].set_xlabel("Net Charge")
            axes[0, 1].set_ylabel("Frequency")
            axes[0, 1].grid(True, alpha=0.3)
        
        # Sequence length vs score
        axes[1, 0].scatter(peptide_df["sequence_length"], peptide_df["combined_score"], 
                          alpha=0.6, s=50, c='green')
        axes[1, 0].set_xlabel("Sequence Length")
        axes[1, 0].set_ylabel("Combined Score")
        axes[1, 0].set_title("Sequence Length vs Combined Score")
        axes[1, 0].grid(True, alpha=0.3)
        
        # Hydrophobicity vs score
        if "hydrophobicity" in peptide_df.columns:
            axes[1, 1].scatter(peptide_df["hydrophobicity"], peptide_df["combined_score"], 
                              alpha=0.6, s=50, c='purple')
            axes[1, 1].set_xlabel("Hydrophobicity Index")
            axes[1, 1].set_ylabel("Combined Score")
            axes[1, 1].set_title("Hydrophobicity vs Combined Score")
            axes[1, 1].grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = os.path.join(self.plots_dir, "peptide_properties.png")
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        return output_path
    
    def _plot_small_molecule_properties(self) -> str:
        """Plot small molecule properties analysis"""
        sm_df = self.df[self.df["mol_type"] == "small_molecule"]
        
        if sm_df.empty:
            return ""
        
        fig, axes = plt.subplots(2, 3, figsize=(18, 12))
        fig.suptitle("Small Molecule Properties Analysis", fontsize=16, fontweight='bold')
        
        # LogP distribution
        if "logp" in sm_df.columns:
            axes[0, 0].hist(sm_df["logp"], bins=20, alpha=0.7, color='lightblue', edgecolor='black')
            axes[0, 0].set_title("LogP Distribution")
            axes[0, 0].set_xlabel("LogP")
            axes[0, 0].set_ylabel("Frequency")
            axes[0, 0].grid(True, alpha=0.3)
        
        # Hydrogen bond donor distribution
        if "hbd" in sm_df.columns:
            axes[0, 1].hist(sm_df["hbd"], bins=10, alpha=0.7, color='lightcoral', edgecolor='black')
            axes[0, 1].set_title("Hydrogen Bond Donor Distribution")
            axes[0, 1].set_xlabel("Number of HBD")
            axes[0, 1].set_ylabel("Frequency")
            axes[0, 1].grid(True, alpha=0.3)
        
        # Hydrogen bond acceptor distribution
        if "hba" in sm_df.columns:
            axes[0, 2].hist(sm_df["hba"], bins=15, alpha=0.7, color='lightgreen', edgecolor='black')
            axes[0, 2].set_title("Hydrogen Bond Acceptor Distribution")
            axes[0, 2].set_xlabel("Number of HBA")
            axes[0, 2].set_ylabel("Frequency")
            axes[0, 2].grid(True, alpha=0.3)
        
        # LogP vs score
        if "logp" in sm_df.columns:
            axes[1, 0].scatter(sm_df["logp"], sm_df["combined_score"], alpha=0.6, s=50, c='blue')
            axes[1, 0].set_xlabel("LogP")
            axes[1, 0].set_ylabel("Combined Score")
            axes[1, 0].set_title("LogP vs Combined Score")
            axes[1, 0].grid(True, alpha=0.3)
        
        # Molecular weight vs score
        axes[1, 1].scatter(sm_df["molecular_weight"], sm_df["combined_score"], alpha=0.6, s=50, c='red')
        axes[1, 1].set_xlabel("Molecular Weight")
        axes[1, 1].set_ylabel("Combined Score")
        axes[1, 1].set_title("Molecular Weight vs Combined Score")
        axes[1, 1].grid(True, alpha=0.3)
        
        # Lipinski violations distribution
        if "lipinski_violations" in sm_df.columns:
            violation_counts = sm_df["lipinski_violations"].value_counts().sort_index()
            axes[1, 2].bar(violation_counts.index, violation_counts.values, alpha=0.7, color='orange')
            axes[1, 2].set_title("Lipinski Rule Violations Distribution")
            axes[1, 2].set_xlabel("Number of Violations")
            axes[1, 2].set_ylabel("Number of Compounds")
            axes[1, 2].grid(True, alpha=0.3)
        
        plt.tight_layout()
        output_path = os.path.join(self.plots_dir, "small_molecule_properties.png")
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        return output_path
    
    def _plot_ranking_trend(self) -> str:
        """Plot ranking vs score trend chart"""
        plt.figure(figsize=(12, 8))
        
        # Plot combined score trend
        plt.subplot(2, 1, 1)
        plt.plot(self.df["rank"], self.df["combined_score"], 'b-', linewidth=2, alpha=0.7, label="Combined Score")
        plt.fill_between(self.df["rank"], self.df["combined_score"], alpha=0.3)
        plt.xlabel("Rank")
        plt.ylabel("Combined Score")
        plt.title("Combined Score Ranking Trend")
        plt.grid(True, alpha=0.3)
        plt.legend()
        
        # Plot score comparison
        plt.subplot(2, 1, 2)
        sample_indices = np.linspace(0, len(self.df)-1, min(50, len(self.df)), dtype=int)
        sample_df = self.df.iloc[sample_indices]
        
        plt.plot(sample_df["rank"], sample_df["binding_score"], 'r-', linewidth=2, alpha=0.7, label="Binding Affinity")
        plt.plot(sample_df["rank"], sample_df["structural_score"], 'g-', linewidth=2, alpha=0.7, label="Structural Stability")
        plt.plot(sample_df["rank"], sample_df["confidence_score"], 'orange', linewidth=2, alpha=0.7, label="Confidence")
        plt.plot(sample_df["rank"], sample_df["combined_score"], 'b-', linewidth=2, alpha=0.7, label="Combined Score")
        
        plt.xlabel("Rank")
        plt.ylabel("Score")
        plt.title("Score Ranking Trend Comparison")
        plt.grid(True, alpha=0.3)
        plt.legend()
        
        plt.tight_layout()
        output_path = os.path.join(self.plots_dir, "ranking_trend.png")
        plt.savefig(output_path, dpi=300, bbox_inches='tight')
        plt.close()
        
        return output_path
    
    def generate_statistics(self) -> Dict[str, Any]:
        """Generate statistical information"""
        if self.df.empty:
            return {
                "total_molecules": 0,
                "molecule_type_distribution": {},
                "score_statistics": {
                    "combined_score": {"mean": 0, "std": 0, "max": 0, "min": 0, "median": 0},
                    "binding_affinity": {"mean": 0, "std": 0, "max": 0, "min": 0},
                    "structural_stability": {"mean": 0, "std": 0, "max": 0, "min": 0}
                }
            }
            
        stats = {
            "total_molecules": len(self.df),
            "molecule_type_distribution": self.df["mol_type"].value_counts().to_dict(),
            "score_statistics": {
                "combined_score": {
                    "mean": float(self.df["combined_score"].mean()),
                    "std": float(self.df["combined_score"].std()),
                    "max": float(self.df["combined_score"].max()),
                    "min": float(self.df["combined_score"].min()),
                    "median": float(self.df["combined_score"].median())
                },
                "binding_affinity": {
                    "mean": float(self.df["binding_score"].mean()),
                    "std": float(self.df["binding_score"].std()),
                    "max": float(self.df["binding_score"].max()),
                    "min": float(self.df["binding_score"].min())
                },
                "structural_stability": {
                    "mean": float(self.df["structural_score"].mean()),
                    "std": float(self.df["structural_score"].std()),
                    "max": float(self.df["structural_score"].max()),
                    "min": float(self.df["structural_score"].min())
                }
            }
        }
        
        # Molecule-specific statistics
        if "peptide" in self.df["mol_type"].values:
            peptide_df = self.df[self.df["mol_type"] == "peptide"]
            stats["peptide_statistics"] = {
                "count": len(peptide_df),
                "average_sequence_length": float(peptide_df["sequence_length"].mean()),
                "average_molecular_weight": float(peptide_df["molecular_weight"].mean())
            }
        
        if "small_molecule" in self.df["mol_type"].values:
            sm_df = self.df[self.df["mol_type"] == "small_molecule"]
            stats["small_molecule_statistics"] = {
                "count": len(sm_df),
                "average_molecular_weight": float(sm_df["molecular_weight"].mean())
            }
            
            if "lipinski_violations" in sm_df.columns:
                stats["small_molecule_statistics"]["drug_likeness"] = {
                    "lipinski_compliant": int((sm_df["lipinski_violations"] <= 1).sum()),
                    "lipinski_violations": int((sm_df["lipinski_violations"] > 1).sum())
                }
        
        return stats
    
    def generate_html_report(self) -> str:
        """Generate enhanced HTML analysis report"""
        try:
            from datetime import datetime
            
            # Generate statistics
            stats = self.generate_statistics()
            
            # Get top hit molecules
            top_hits = self.df.head(20) if not self.df.empty else pd.DataFrame()
            
            # HTML template with enhanced styling
            html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Virtual Screening Analysis Report</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }}
        .content {{
            padding: 40px;
        }}
        .section {{
            margin-bottom: 50px;
        }}
        .section h2 {{
            color: #2c3e50;
            font-size: 1.8em;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 3px solid #667eea;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }}
        .stats-card {{
            background: linear-gradient(145deg, #f8f9fa, #e9ecef);
            padding: 25px;
            border-radius: 10px;
            border-left: 5px solid #667eea;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        .stats-value {{
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }}
        .molecule-card {{
            background: white;
            border: 1px solid #e9ecef;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            border-radius: 10px;
            overflow: hidden;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        th {{
            background: #667eea;
            color: white;
            font-weight: bold;
        }}
        tr:nth-child(even) {{
            background-color: #f2f2f2;
        }}
        .footer {{
            background: #2c3e50;
            color: white;
            text-align: center;
            padding: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧬 Virtual Screening Analysis Report</h1>
            <p>Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
        
        <div class="content">
            <div class="section">
                <h2>📊 Overview Statistics</h2>
                <div class="stats-grid">
                    <div class="stats-card">
                        <h3>📈 Total Molecules</h3>
                        <div class="stats-value">{stats.get('total_molecules', 0)}</div>
                    </div>"""
            
            # Add molecule type distribution
            mol_types = stats.get('molecule_type_distribution', {})
            for mol_type, count in mol_types.items():
                if mol_type == 'small_molecule':
                    icon = '💊'
                    label = 'Small Molecules'
                elif mol_type == 'peptide':
                    icon = '🧬'
                    label = 'Peptides'
                else:
                    icon = '🔬'
                    label = mol_type.title()
                    
                html_content += f"""
                    <div class="stats-card">
                        <h3>{icon} {label}</h3>
                        <div class="stats-value">{count}</div>
                    </div>"""
            
            # Add score statistics
            score_stats = stats.get('score_statistics', {})
            combined_stats = score_stats.get('combined_score', {})
            
            html_content += f"""
                    <div class="stats-card">
                        <h3>🎯 Best Score</h3>
                        <div class="stats-value">{combined_stats.get('max', 0):.3f}</div>
                    </div>
                    <div class="stats-card">
                        <h3>📊 Average Score</h3>
                        <div class="stats-value">{combined_stats.get('mean', 0):.3f}</div>
                    </div>
                </div>
            </div>"""
            
            # Add top molecules section
            if not top_hits.empty:
                html_content += """
            <div class="section">
                <h2>🎯 Top Hit Molecules</h2>"""
                
                for idx, (_, mol) in enumerate(top_hits.iterrows()):
                    html_content += f"""
                    <div class="molecule-card">
                        <h4>#{int(mol.get('rank', idx + 1))} - {mol.get('molecule_id', f'Molecule_{idx+1}')}</h4>
                        <p><strong>Combined Score:</strong> {mol.get('combined_score', 0):.3f}</p>
                        <p><strong>Binding Score:</strong> {mol.get('binding_score', 0):.3f}</p>
                        <p><strong>Structural Score:</strong> {mol.get('structural_score', 0):.3f}</p>
                        <p><strong>Confidence:</strong> {mol.get('confidence_score', 0):.3f}</p>
                    </div>"""
                
                html_content += """
            </div>"""
            
            # Add results table
            html_content += f"""
            <div class="section">
                <h2>📋 Detailed Results</h2>
                {self._generate_results_table(top_hits)}
            </div>"""
            
            html_content += f"""
        </div>
        
        <div class="footer">
            <p>🧬 Virtual Screening Analysis Report - Generated by Boltz-WebUI</p>
            <p>Analysis completed on {datetime.now().strftime('%Y-%m-%d at %H:%M:%S')}</p>
        </div>
    </div>
</body>
</html>"""
            
            # Save HTML report
            output_path = os.path.join(self.output_dir, "screening_report.html")
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            logger.info(f"Enhanced HTML report generated: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"Failed to generate HTML report: {e}")
            return ""
    
    def _generate_results_table(self, data: pd.DataFrame) -> str:
        """生成结果表格"""
        if data.empty:
            return "<p>No results to display.</p>"
        
        # 选择要显示的列
        display_cols = ['rank', 'molecule_id', 'molecule_name', 'mol_type', 
                       'binding_score', 'confidence_score', 'combined_score']
        
        # 过滤存在的列
        available_cols = [col for col in display_cols if col in data.columns]
        
        # 生成表格HTML
        table_html = '<table><thead><tr>'
        for col in available_cols:
            table_html += f'<th>{col.replace("_", " ").title()}</th>'
        table_html += '</tr></thead><tbody>'
        
        for _, row in data.head(20).iterrows():
            table_html += '<tr>'
            for col in available_cols:
                value = row[col]
                if isinstance(value, float):
                    value = f"{value:.3f}"
                table_html += f'<td>{value}</td>'
            table_html += '</tr>'
        
        table_html += '</tbody></table>'
        return table_html
    
    def generate_html_report(self) -> str:
        """Generate HTML analysis report"""
        try:
            from datetime import datetime
            
            stats = self.generate_statistics()
            top_hits = self.df.head(10) if not self.df.empty else pd.DataFrame()
            
            html = f"""<!DOCTYPE html>
<html><head><title>Virtual Screening Report</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }}
.container {{ max-width: 1200px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; }}
h1 {{ color: #2c3e50; text-align: center; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin: 20px 0; }}
.stat-card {{ background: #ecf0f1; padding: 20px; border-radius: 8px; text-align: center; }}
.plot {{ text-align: center; margin: 20px 0; }}
.molecules {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; }}
.molecule {{ background: #f8f9fa; padding: 15px; border-radius: 8px; border: 1px solid #dee2e6; }}
</style></head><body>
<div class="container">
<h1>🧬 Virtual Screening Analysis Report</h1>
<p style="text-align: center;">Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

<h2>📊 Statistics</h2>
<div class="stats">
<div class="stat-card"><h3>Total Molecules</h3><p>{stats.get('total_molecules', 0)}</p></div>
<div class="stat-card"><h3>Best Score</h3><p>{stats.get('score_statistics', {}).get('combined_score', {}).get('max', 0):.3f}</p></div>
<div class="stat-card"><h3>Average Score</h3><p>{stats.get('score_statistics', {}).get('combined_score', {}).get('mean', 0):.3f}</p></div>
</div>

<h2>📈 Analysis Charts</h2>"""
            
            # Add plots
            plots = ["score_distribution.png", "score_correlation.png", "top_hits_analysis.png"]
            for plot in plots:
                if os.path.exists(os.path.join(self.plots_dir, plot)):
                    html += f'<div class="plot"><img src="plots/{plot}" style="max-width:100%; border-radius:8px;"></div>'
            
            # Add top molecules
            if not top_hits.empty:
                html += '<h2>🎯 Top Hit Molecules</h2><div class="molecules">'
                for _, mol in top_hits.iterrows():
                    html += f'''<div class="molecule">
<h4>#{int(mol.get('rank', 0))} - {mol.get('molecule_id', 'Unknown')}</h4>
<p><strong>Score:</strong> {mol.get('combined_score', 0):.3f}</p>
<p><strong>Binding:</strong> {mol.get('binding_score', 0):.3f}</p>
<p><strong>Structural:</strong> {mol.get('structural_score', 0):.3f}</p>
</div>'''
                html += '</div>'
            
            html += '''
<footer style="margin-top: 40px; text-align: center; color: #7f8c8d;">
<p>Report Generated by Boltz-WebUI Virtual Screening System</p>
</footer>
</div></body></html>'''
            
            output_path = os.path.join(self.output_dir, "screening_report.html")
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html)
            
            logger.info(f"HTML report generated: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"Failed to generate HTML report: {e}")
            return ""
    
    def generate_html_report(self) -> str:
        """Generate enhanced HTML analysis report"""
        try:
            from datetime import datetime
            
            # Generate statistics
            stats = self.generate_statistics()
            
            # Get top hit molecules
            top_hits = self.df.head(20) if not self.df.empty else pd.DataFrame()
            
            # Create HTML content
            html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Virtual Screening Analysis Report</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            line-height: 1.6;
            color: #333;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 15px;
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            text-align: center;
        }}
        .header h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }}
        .content {{
            padding: 40px;
        }}
        .section {{
            margin-bottom: 50px;
        }}
        .section h2 {{
            color: #2c3e50;
            font-size: 1.8em;
            margin-bottom: 20px;
            padding-bottom: 10px;
            border-bottom: 3px solid #667eea;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }}
        .stats-card {{
            background: linear-gradient(145deg, #f8f9fa, #e9ecef);
            padding: 25px;
            border-radius: 10px;
            border-left: 5px solid #667eea;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        .stats-value {{
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }}
        .plot-container {{
            text-align: center;
            margin: 30px 0;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 10px;
        }}
        .plot-container img {{
            max-width: 100%;
            height: auto;
            border-radius: 10px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
        }}
        .molecules-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
            gap: 20px;
        }}
        .molecule-card {{
            background: white;
            border: 1px solid #e9ecef;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
        }}
        .molecule-properties {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            margin-top: 15px;
        }}
        .property-item {{
            padding: 8px;
            background: #f8f9fa;
            border-radius: 5px;
            text-align: center;
        }}
        .footer {{
            background: #2c3e50;
            color: white;
            text-align: center;
            padding: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧬 Virtual Screening Analysis Report</h1>
            <p>Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
        
        <div class="content">
            <div class="section">
                <h2>📊 Overview Statistics</h2>
                <div class="stats-grid">
                    <div class="stats-card">
                        <h3>📈 Total Molecules</h3>
                        <div class="stats-value">{stats.get('total_molecules', 0)}</div>
                    </div>"""
            
            # Add molecule type distribution
            mol_types = stats.get('molecule_type_distribution', {})
            for mol_type, count in mol_types.items():
                if mol_type == 'small_molecule':
                    icon = '💊'
                    label = 'Small Molecules'
                elif mol_type == 'peptide':
                    icon = '🧬'
                    label = 'Peptides'
                else:
                    icon = '🔬'
                    label = mol_type.title()
                    
                html_content += f"""
                    <div class="stats-card">
                        <h3>{icon} {label}</h3>
                        <div class="stats-value">{count}</div>
                    </div>"""
            
            # Add score statistics
            score_stats = stats.get('score_statistics', {})
            combined_stats = score_stats.get('combined_score', {})
            
            html_content += f"""
                    <div class="stats-card">
                        <h3>🎯 Best Score</h3>
                        <div class="stats-value">{combined_stats.get('max', 0):.3f}</div>
                    </div>
                    <div class="stats-card">
                        <h3>📊 Average Score</h3>
                        <div class="stats-value">{combined_stats.get('mean', 0):.3f}</div>
                    </div>
                </div>
            </div>"""
            
            # Add plots section
            html_content += """
            <div class="section">
                <h2>📈 Analysis Charts</h2>"""
            
            plot_files = [
                ("score_distribution.png", "Score Distribution Analysis"),
                ("score_correlation.png", "Score Correlation Heatmap"),
                ("molecule_type_distribution.png", "Molecule Type Distribution"),
                ("top_hits_analysis.png", "Top Hits Analysis"),
                ("ranking_trend.png", "Ranking Trend Analysis")
            ]
            
            for plot_file, plot_title in plot_files:
                plot_path = os.path.join(self.plots_dir, plot_file)
                if os.path.exists(plot_path):
                    html_content += f"""
                <div class="plot-container">
                    <h3>{plot_title}</h3>
                    <img src="plots/{plot_file}" alt="{plot_title}">
                </div>"""
            
            html_content += """
            </div>"""
            
            # Add top molecules section
            if not top_hits.empty:
                html_content += """
            <div class="section">
                <h2>🎯 Top Hit Molecules</h2>
                <div class="molecules-grid">"""
                
                for idx, (_, mol) in enumerate(top_hits.iterrows()):
                    html_content += f"""
                    <div class="molecule-card">
                        <h4>#{int(mol.get('rank', idx + 1))} - {mol.get('molecule_id', f'Molecule_{idx+1}')}</h4>
                        <p><strong>Combined Score:</strong> {mol.get('combined_score', 0):.3f}</p>
                        
                        <div class="molecule-properties">
                            <div class="property-item">
                                <strong>Binding:</strong> {mol.get('binding_score', 0):.3f}
                            </div>
                            <div class="property-item">
                                <strong>Structural:</strong> {mol.get('structural_score', 0):.3f}
                            </div>
                            <div class="property-item">
                                <strong>Confidence:</strong> {mol.get('confidence_score', 0):.3f}
                            </div>
                            <div class="property-item">
                                <strong>Mol. Weight:</strong> {mol.get('molecular_weight', 0):.1f}
                            </div>
                        </div>
                    </div>"""
                
                html_content += """
                </div>
            </div>"""
            
            html_content += f"""
        </div>
        
        <div class="footer">
            <p>🧬 Virtual Screening Analysis Report - Generated by Boltz-WebUI</p>
            <p>Analysis completed on {datetime.now().strftime('%Y-%m-%d at %H:%M:%S')}</p>
        </div>
    </div>
</body>
</html>"""
            
            # Save HTML report
            output_path = os.path.join(self.output_dir, "screening_report.html")
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            logger.info(f"Enhanced HTML report generated: {output_path}")
            return output_path
            
        except Exception as e:
            logger.error(f"Failed to generate HTML report: {e}")
            return ""

    def generate_html_report(self) -> str:
        """Generate enhanced HTML analysis report with 2D structures and affinity data"""
        try:
            # Generate statistics
            stats = self.generate_statistics()
            
            # Get top hit molecules
            top_hits = self.df.head(20) if not self.df.empty else pd.DataFrame()
            
            # Check if affinity data is available
            has_affinity = 'affinity_score' in self.df.columns if not self.df.empty else False
            
            # Generate 2D structures for top hits if possible
            structures_html = self._generate_structures_section(top_hits)
            
            # Generate affinity analysis if available
            affinity_section = self._generate_affinity_section() if has_affinity else ""
            
            # HTML template with enhanced styling
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <title>Virtual Screening Analysis Report</title>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    * {{
                        margin: 0;
                        padding: 0;
                        box-sizing: border-box;
                    }}
                    body {{
                        font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                        line-height: 1.6;
                        color: #333;
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        min-height: 100vh;
                        padding: 20px;
                    }}
                    .container {{
                        max-width: 1400px;
                        margin: 0 auto;
                        background: white;
                        border-radius: 15px;
                        box-shadow: 0 20px 40px rgba(0,0,0,0.1);
                        overflow: hidden;
                    }}
                    .header {{
                        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                        color: white;
                        padding: 40px;
                        text-align: center;
                    }}
                    .header h1 {{
                        font-size: 2.5em;
                        margin-bottom: 10px;
                        text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
                    }}
                    .header p {{
                        font-size: 1.2em;
                        opacity: 0.9;
                    }}
                    .content {{
                        padding: 40px;
                    }}
                    .section {{
                        margin-bottom: 50px;
                    }}
                    .section h2 {{
                        color: #2c3e50;
                        font-size: 1.8em;
                        margin-bottom: 20px;
                        padding-bottom: 10px;
                        border-bottom: 3px solid #667eea;
                    }}
                    .stats-grid {{
                        display: grid;
                        grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
                        gap: 20px;
                        margin: 30px 0;
                    }}
                    .stats-card {{
                        background: linear-gradient(145deg, #f8f9fa, #e9ecef);
                        padding: 25px;
                        border-radius: 10px;
                        text-align: center;
                        box-shadow: 0 5px 15px rgba(0,0,0,0.1);
                        transition: transform 0.3s ease;
                    }}
                    .stats-card:hover {{
                        transform: translateY(-5px);
                    }}
                    .stats-value {{
                        font-size: 2.5em;
                        font-weight: bold;
                        color: #667eea;
                        margin-bottom: 10px;
                    }}
                    .molecule-grid {{
                        display: grid;
                        grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
                        gap: 30px;
                        margin: 30px 0;
                    }}
                    .molecule-card {{
                        background: #f8f9fa;
                        border-radius: 10px;
                        padding: 20px;
                        box-shadow: 0 5px 15px rgba(0,0,0,0.1);
                        transition: transform 0.3s ease;
                    }}
                    .molecule-card:hover {{
                        transform: translateY(-5px);
                    }}
                    .molecule-header {{
                        display: flex;
                        justify-content: space-between;
                        align-items: center;
                        margin-bottom: 15px;
                    }}
                    .molecule-title {{
                        font-size: 1.2em;
                        font-weight: bold;
                        color: #2c3e50;
                    }}
                    .molecule-rank {{
                        background: #667eea;
                        color: white;
                        padding: 5px 10px;
                        border-radius: 15px;
                        font-weight: bold;
                    }}
                    .molecule-structure {{
                        text-align: center;
                        margin: 15px 0;
                    }}
                    .molecule-details {{
                        display: grid;
                        grid-template-columns: 1fr 1fr;
                        gap: 10px;
                        margin-top: 15px;
                    }}
                    .detail-item {{
                        display: flex;
                        justify-content: space-between;
                        padding: 8px;
                        background: rgba(102, 126, 234, 0.1);
                        border-radius: 5px;
                    }}
                    .affinity-highlight {{
                        background: linear-gradient(45deg, #ff6b6b, #feca57);
                        color: white;
                        font-weight: bold;
                    }}
                    table {{
                        width: 100%;
                        border-collapse: collapse;
                        margin: 20px 0;
                        box-shadow: 0 5px 15px rgba(0,0,0,0.1);
                        border-radius: 10px;
                        overflow: hidden;
                    }}
                    th, td {{
                        padding: 12px;
                        text-align: left;
                        border-bottom: 1px solid #ddd;
                    }}
                    th {{
                        background: #667eea;
                        color: white;
                        font-weight: bold;
                    }}
                    tr:nth-child(even) {{
                        background-color: #f2f2f2;
                    }}
                    tr:hover {{
                        background-color: #e9ecef;
                    }}
                    .timestamp {{
                        color: #6c757d;
                        font-style: italic;
                    }}
                </style>
            </head>
            <body>
                <div class="container">
                    <div class="header">
                        <h1>🧬 Virtual Screening Analysis Report</h1>
                        <p>Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                        {'<p>✨ Enhanced with Affinity Calculation</p>' if has_affinity else ''}
                    </div>
                    
                    <div class="content">
                        <div class="section">
                            <h2>📊 Statistics Summary</h2>
                            <div class="stats-grid">
                                <div class="stats-card">
                                    <div class="stats-value">{stats.get('total_molecules', 0)}</div>
                                    <div>Total Molecules</div>
                                </div>
                                <div class="stats-card">
                                    <div class="stats-value">{stats.get('avg_binding_score', 0):.3f}</div>
                                    <div>Avg Binding Score</div>
                                </div>
                                <div class="stats-card">
                                    <div class="stats-value">{stats.get('avg_confidence', 0):.3f}</div>
                                    <div>Avg Confidence</div>
                                </div>
                                {'<div class="stats-card"><div class="stats-value">' + f"{stats.get('avg_affinity', 0):.3f}" + '</div><div>Avg Affinity Score</div></div>' if has_affinity else ''}
                            </div>
                        </div>
                        
                        {affinity_section}
                        
                        <div class="section">
                            <h2>🏆 Top Hit Molecules</h2>
                            {structures_html}
                        </div>
                        
                        <div class="section">
                            <h2>📋 Detailed Results</h2>
                            {self._generate_results_table(top_hits)}
                        </div>
                    </div>
                </div>
            </body>
            </html>
            """
            
            return html_content
            
        except Exception as e:
            logger.error(f"生成HTML报告失败: {e}")
            # 返回简化版本
            return self._generate_simple_html_report()
    
    def _generate_structures_section(self, top_hits: pd.DataFrame) -> str:
        """生成2D结构展示部分"""
        if top_hits.empty:
            return "<p>No molecules to display.</p>"
        
        structures_html = '<div class="molecule-grid">'
        
        for idx, row in top_hits.iterrows():
            # 尝试生成2D结构图像
            structure_img = self._generate_2d_structure(row['sequence'], row['mol_type'])
            
            # 获取亲和力信息
            affinity_info = ""
            if 'affinity_score' in row and pd.notna(row['affinity_score']):
                affinity_info = f'<div class="detail-item affinity-highlight"><span>Affinity Score:</span><span>{row["affinity_score"]:.3f}</span></div>'
            
            structures_html += f'''
            <div class="molecule-card">
                <div class="molecule-header">
                    <div class="molecule-title">{row.get("molecule_name", row["molecule_id"])}</div>
                    <div class="molecule-rank">#{row.get("rank", idx+1)}</div>
                </div>
                
                <div class="molecule-structure">
                    {structure_img}
                </div>
                
                <div class="molecule-details">
                    <div class="detail-item">
                        <span>Binding Score:</span>
                        <span>{row["binding_score"]:.3f}</span>
                    </div>
                    <div class="detail-item">
                        <span>Confidence:</span>
                        <span>{row["confidence_score"]:.3f}</span>
                    </div>
                    <div class="detail-item">
                        <span>Combined Score:</span>
                        <span>{row["combined_score"]:.3f}</span>
                    </div>
                    {affinity_info}
                </div>
            </div>
            '''
        
        structures_html += '</div>'
        return structures_html
    
    def _generate_2d_structure(self, sequence: str, mol_type: str) -> str:
        """生成2D分子结构图像"""
        if not RDKIT_AVAILABLE or mol_type != "small_molecule":
            return f'<div style="padding: 20px; background: #f0f0f0; border-radius: 5px; text-align: center;">Structure: {sequence[:50]}...</div>'
        
        try:
            mol = Chem.MolFromSmiles(sequence)
            if mol is None:
                return f'<div style="padding: 20px; background: #f0f0f0; border-radius: 5px; text-align: center;">Invalid SMILES: {sequence[:50]}...</div>'
            
            # 生成2D图像
            drawer = rdMolDraw2D.MolDraw2DSVG(300, 300)
            drawer.SetFontSize(0.8)
            drawer.DrawMolecule(mol)
            drawer.FinishDrawing()
            svg_text = drawer.GetDrawingText()
            
            # 确保SVG正确嵌入
            if svg_text.startswith('<?xml'):
                # 移除XML声明，因为它可能在HTML中造成问题
                svg_start = svg_text.find('<svg')
                if svg_start != -1:
                    svg_text = svg_text[svg_start:]
            
            return f'<div style="text-align: center; padding: 10px;">{svg_text}</div>'
        
        except Exception as e:
            logger.warning(f"生成2D结构失败: {e}")
            return f'<div style="padding: 20px; background: #f0f0f0; border-radius: 5px; text-align: center;">Structure: {sequence[:50]}...</div>'
    
    def _generate_affinity_section(self) -> str:
        """生成亲和力分析部分"""
        if self.df.empty or 'affinity_score' not in self.df.columns:
            return ""
        
        # 计算亲和力统计
        affinity_stats = {
            'count': self.df['affinity_score'].count(),
            'mean': self.df['affinity_score'].mean(),
            'std': self.df['affinity_score'].std(),
            'min': self.df['affinity_score'].min(),
            'max': self.df['affinity_score'].max()
        }
        
        return f'''
        <div class="section">
            <h2>🎯 Affinity Analysis</h2>
            <div class="stats-grid">
                <div class="stats-card">
                    <div class="stats-value">{affinity_stats['count']}</div>
                    <div>Molecules with Affinity</div>
                </div>
                <div class="stats-card">
                    <div class="stats-value">{affinity_stats['mean']:.3f}</div>
                    <div>Mean Affinity Score</div>
                </div>
                <div class="stats-card">
                    <div class="stats-value">{affinity_stats['min']:.3f}</div>
                    <div>Min Affinity</div>
                </div>
                <div class="stats-card">
                    <div class="stats-value">{affinity_stats['max']:.3f}</div>
                    <div>Max Affinity</div>
                </div>
            </div>
        </div>
        '''
    
    def _generate_results_table(self, data: pd.DataFrame) -> str:
        """生成结果表格"""
        if data.empty:
            return "<p>No results to display.</p>"
        
        # 选择要显示的列
        display_cols = ['rank', 'molecule_id', 'molecule_name', 'mol_type', 
                       'binding_score', 'confidence_score', 'combined_score']
        
        if 'affinity_score' in data.columns:
            display_cols.append('affinity_score')
        
        # 过滤存在的列
        available_cols = [col for col in display_cols if col in data.columns]
        
        # 生成表格HTML
        table_html = '<table><thead><tr>'
        for col in available_cols:
            table_html += f'<th>{col.replace("_", " ").title()}</th>'
        table_html += '</tr></thead><tbody>'
        
        for _, row in data.head(20).iterrows():
            table_html += '<tr>'
            for col in available_cols:
                value = row[col]
                if isinstance(value, float):
                    value = f"{value:.3f}"
                table_html += f'<td>{value}</td>'
            table_html += '</tr>'
        
        table_html += '</tbody></table>'
        return table_html
    
    def export_top_structures(self, top_n: int = 10) -> List[str]:
        """导出顶部命中分子的结构"""
        exported_files = []
        
        if not RDKIT_AVAILABLE:
            logger.warning("RDKit未安装，跳过结构导出")
            return exported_files
        
        try:
            top_hits = self.results[:top_n]
            structures_dir = os.path.join(self.output_dir, "top_structures")
            os.makedirs(structures_dir, exist_ok=True)
            
            for i, result in enumerate(top_hits):
                if result.mol_type == "small_molecule":
                    try:
                        mol = Chem.MolFromSmiles(result.sequence)
                        if mol:
                            # 生成2D结构图
                            img_path = os.path.join(structures_dir, f"rank_{i+1}_{result.molecule_id}.png")
                            img = Draw.MolToImage(mol, size=(300, 300))
                            img.save(img_path)
                            exported_files.append(img_path)
                    except Exception as e:
                        logger.warning(f"导出分子 {result.molecule_id} 结构失败: {e}")
            
            logger.info(f"成功导出 {len(exported_files)} 个分子结构")
            return exported_files
            
        except Exception as e:
            logger.error(f"导出结构失败: {e}")
            return exported_files
    
    def _generate_simple_html_report(self) -> str:
        """生成简化版HTML报告（备用方案）"""
        try:
            from datetime import datetime
            
            stats = self.generate_statistics()
            
            # 简化的HTML模板
            html_content = f"""<!DOCTYPE html>
<html>
<head>
    <title>Virtual Screening Report</title>
    <meta charset="UTF-8">
    <style>
        body {{
            font-family: Arial, sans-serif;
            margin: 20px;
            background: #f5f5f5;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            background: white;
            padding: 30px;
            border-radius: 10px;
            box-shadow: 0 4px 8px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #2c3e50;
            text-align: center;
            border-bottom: 2px solid #3498db;
            padding-bottom: 10px;
        }}
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }}
        .stat-card {{
            background: #ecf0f1;
            padding: 20px;
            border-radius: 8px;
            text-align: center;
        }}
        .stat-value {{
            font-size: 2em;
            font-weight: bold;
            color: #3498db;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #ddd;
        }}
        th {{
            background-color: #3498db;
            color: white;
        }}
        tr:nth-child(even) {{
            background-color: #f2f2f2;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🧬 Virtual Screening Report</h1>
        <p style="text-align: center; color: #7f8c8d;">
            Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        </p>
        
        <div class="stats">
            <div class="stat-card">
                <div class="stat-value">{stats.get('total_molecules', 0)}</div>
                <div>Total Molecules</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{stats.get('score_statistics', {}).get('combined_score', {}).get('max', 0):.3f}</div>
                <div>Best Score</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{stats.get('score_statistics', {}).get('combined_score', {}).get('mean', 0):.3f}</div>
                <div>Average Score</div>
            </div>
        </div>
        
        {self._generate_simple_results_table()}
        
        <footer style="margin-top: 40px; text-align: center; color: #7f8c8d;">
            <p>Generated by Boltz-WebUI Virtual Screening System</p>
        </footer>
    </div>
</body>
</html>"""
            
            return html_content
            
        except Exception as e:
            logger.error(f"生成简化HTML报告失败: {e}")
            return "<html><body><h1>Report Generation Failed</h1></body></html>"
    
    def _generate_simple_results_table(self) -> str:
        """生成简化的结果表格"""
        try:
            if self.df.empty:
                return "<p>No results to display.</p>"
            
            top_results = self.df.head(10)
            
            table_html = """
            <h2>Top Results</h2>
            <table>
                <thead>
                    <tr>
                        <th>Rank</th>
                        <th>Molecule ID</th>
                        <th>Type</th>
                        <th>Combined Score</th>
                        <th>Binding Score</th>
                        <th>Confidence</th>
                    </tr>
                </thead>
                <tbody>
            """
            
            for _, row in top_results.iterrows():
                table_html += f"""
                    <tr>
                        <td>{int(row.get('rank', 0))}</td>
                        <td>{row.get('molecule_id', 'Unknown')}</td>
                        <td>{row.get('mol_type', 'Unknown')}</td>
                        <td>{row.get('combined_score', 0):.3f}</td>
                        <td>{row.get('binding_score', 0):.3f}</td>
                        <td>{row.get('confidence_score', 0):.3f}</td>
                    </tr>
                """
            
            table_html += """
                </tbody>
            </table>
            """
            
            return table_html
            
        except Exception as e:
            logger.error(f"生成简化表格失败: {e}")
            return "<p>Table generation failed.</p>"
