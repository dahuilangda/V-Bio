"""
Boltz-WebUI 虚拟筛选模块

一个用于多肽和小分子虚拟筛选的完整工具包，支持：
- 多种分子格式（FASTA、SDF、CSV、SMILES）
- 高通量并行筛选
- 多目标评分系统
- 结果分析和可视化
- 灵活的配置选项

主要组件：
- MoleculeLibrary: 分子库管理
- ScreeningEngine: 筛选引擎
- ResultAnalyzer: 结果分析
- 各种实用工具

使用示例：
    # 命令行使用
    python run_screening.py --target target.yaml --library library.fasta --output_dir results
    
    # 程序化使用
    from virtual_screening import ScreeningEngine, ScreeningConfig
    config = ScreeningConfig(...)
    engine = ScreeningEngine(client, config)
    engine.run_screening()
"""

__version__ = "1.0.0"
__author__ = "Boltz-WebUI Team"
__email__ = "support@boltz-webui.com"

# 导入主要类和函数
from .molecule_library import (
    Molecule,
    MoleculeLibrary,
    PeptideLibrary,
    SmallMoleculeLibrary,
    LibraryProcessor
)

from .screening_engine import (
    ScreeningEngine,
    ScreeningConfig,
    ScreeningResult,
    ScoringSystem,
    BatchManager
)

from .api_client import BoltzApiClient

from .result_analyzer import ResultAnalyzer

from .screening_utils import (
    FormatConverter,
    SimilarityAnalyzer,
    LibraryFilter,
    ResultPostProcessor,
    ConfigManager,
    BatchProcessor,
    validate_library_format,
    estimate_screening_time
)

__all__ = [
    # 核心类
    "ScreeningEngine",
    "ScreeningConfig", 
    "ScreeningResult",
    "BoltzApiClient",
    "ResultAnalyzer",
    
    # 分子库相关
    "Molecule",
    "MoleculeLibrary",
    "PeptideLibrary", 
    "SmallMoleculeLibrary",
    "LibraryProcessor",
    
    # 评分和批处理
    "ScoringSystem",
    "BatchManager",
    
    # 工具类
    "FormatConverter",
    "SimilarityAnalyzer", 
    "LibraryFilter",
    "ResultPostProcessor",
    "ConfigManager",
    "BatchProcessor",
    
    # 工具函数
    "validate_library_format",
    "estimate_screening_time",
]
