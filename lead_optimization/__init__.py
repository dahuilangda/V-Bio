# /data/boltz_webui/lead_optimization/__init__.py

"""
Lead Optimization Module

基于 mmpdb 和 Boltz-WebUI 的智能化合物优化平台
支持骨架跃迁和片段替换的生产级别化合物优化工具

主要模块:
- api_client: Boltz-WebUI API 客户端
- mmp_engine: MMP (Matched Molecular Pair) 引擎
- optimization_engine: 化合物优化核心引擎
- scaffold_hopper: 骨架跃迁模块
- fragment_replacer: 片段替换模块
- scoring_system: 多目标评分系统
- result_analyzer: 结果分析和可视化
- compound_library: 化合物库管理
"""

# 导入核心模块
from optimization_engine import OptimizationEngine
from api_client import BoltzOptimizationClient
from mmp_engine import MMPEngine
from scoring_system import MultiObjectiveScoring
from result_analyzer import OptimizationAnalyzer

# 导入配置和异常
from .config import OptimizationConfig
from .exceptions import (
    OptimizationError,
    MMPDatabaseError,
    BoltzAPIError,
    InvalidCompoundError
)

__all__ = [
    'OptimizationEngine',
    'BoltzOptimizationClient', 
    'MMPEngine',
    'MultiObjectiveScoring',
    'OptimizationAnalyzer',
    'OptimizationConfig',
    'OptimizationError',
    'MMPDatabaseError', 
    'BoltzAPIError',
    'InvalidCompoundError'
]
