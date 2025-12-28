# /data/boltz_webui/lead_optimization/config.py

"""
Configuration management for lead optimization
"""

import os
import yaml
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field, asdict
from pathlib import Path

# 加载 .env 文件
def load_env_file():
    """加载 .env 文件中的环境变量"""
    # 尝试从项目根目录加载 .env 文件
    env_file = Path(__file__).parent.parent / ".env"
    if env_file.exists():
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    # 跳过注释和空行
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        # 只设置未被系统环境变量覆盖的变量
                        if key not in os.environ:
                            os.environ[key.strip()] = value.strip()
        except Exception as e:
            print(f"警告: 无法加载 .env 文件: {e}")

# 加载 .env 文件
load_env_file()

@dataclass
class BoltzAPIConfig:
    """Boltz-WebUI API configuration"""
    server_url: str = "http://localhost:5000"
    api_token: str = ""
    backend: str = "boltz"
    timeout: int = 1800  # 30 minutes
    retry_attempts: int = 3
    retry_delay: int = 10  # seconds
    max_concurrent_jobs: int = 10
    poll_interval: int = 30  # seconds
    
    def __post_init__(self):
        # 尝试从环境变量获取API token
        if not self.api_token:
            self.api_token = os.getenv('BOLTZ_API_TOKEN', '')
    
@dataclass
class MMPDatabaseConfig:
    """MMP database configuration"""
    database_path: str = str(Path(__file__).parent / "data" / "chembl_32.mmpdb")
    reference_data_path: str = str(Path(__file__).parent / "data" / "reference_compounds.smi")
    max_heavy_atoms: int = 50
    min_heavy_atoms: int = 5
    max_pairs_per_transform: int = 1000
    similarity_threshold: float = 0.8
    
@dataclass
class OptimizationParameters:
    """Core optimization parameters"""
    max_candidates: int = 100
    max_generations: int = 50
    population_size: int = 200
    mutation_rate: float = 0.1
    crossover_rate: float = 0.8
    selection_pressure: float = 2.0
    diversity_penalty: float = 0.1
    
    # Scaffold hopping parameters
    scaffold_similarity_threshold: float = 0.3
    scaffold_diversity_weight: float = 0.2
    
    # Fragment replacement parameters
    fragment_size_min: int = 3
    fragment_size_max: int = 8
    fragment_replacement_rate: float = 0.3
    
@dataclass
class ScoringWeights:
    """Scoring system weights"""
    binding_affinity: float = 0.4
    selectivity: float = 0.2
    drug_likeness: float = 0.15
    synthetic_accessibility: float = 0.1
    novelty: float = 0.1
    stability: float = 0.05
    
    def normalize(self) -> 'ScoringWeights':
        """Normalize weights to sum to 1.0"""
        total = sum(asdict(self).values())
        if total <= 0:
            raise ValueError("Total weight must be positive")
        
        return ScoringWeights(
            binding_affinity=self.binding_affinity / total,
            selectivity=self.selectivity / total,
            drug_likeness=self.drug_likeness / total,
            synthetic_accessibility=self.synthetic_accessibility / total,
            novelty=self.novelty / total,
            stability=self.stability / total
        )

@dataclass
class FilterCriteria:
    """Compound filtering criteria"""
    # Lipinski Rule of Five
    mw_max: float = 500.0
    logp_max: float = 5.0
    hbd_max: int = 5  # Hydrogen bond donors
    hba_max: int = 10  # Hydrogen bond acceptors
    
    # Additional drug-likeness criteria
    tpsa_max: float = 140.0  # Topological polar surface area
    rotatable_bonds_max: int = 10
    formal_charge_range: tuple = (-2, 2)
    
    # Synthetic accessibility
    sa_score_max: float = 6.0
    
    # Similarity constraints
    parent_similarity_min: float = 0.3
    parent_similarity_max: float = 0.9
    
    # PAINS filtering
    filter_pains: bool = True
    filter_reactive: bool = True
    
@dataclass
class ParallelConfig:
    """Parallel processing configuration"""
    max_workers: int = 8
    chunk_size: int = 100
    use_multiprocessing: bool = True
    memory_limit_gb: float = 16.0
    
@dataclass
class OutputConfig:
    """Output and reporting configuration"""
    save_structures: bool = True
    generate_plots: bool = True
    generate_html_report: bool = True
    save_optimization_tree: bool = True
    structure_format: str = "sdf"  # sdf, mol2, pdb
    plot_format: str = "png"  # png, svg, pdf
    
@dataclass
class OptimizationConfig:
    """Main configuration class"""
    # Sub-configurations
    boltz_api: BoltzAPIConfig = field(default_factory=BoltzAPIConfig)
    mmp_database: MMPDatabaseConfig = field(default_factory=MMPDatabaseConfig)
    optimization: OptimizationParameters = field(default_factory=OptimizationParameters)
    scoring_weights: ScoringWeights = field(default_factory=ScoringWeights)
    filters: FilterCriteria = field(default_factory=FilterCriteria)
    parallel: ParallelConfig = field(default_factory=ParallelConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    
    # Global settings
    random_seed: int = 42
    verbosity: int = 1  # 0=quiet, 1=info, 2=debug
    # temp_dir: str = "tmp"
    
    @classmethod
    def from_yaml(cls, config_path: str) -> 'OptimizationConfig':
        """Load configuration from YAML file"""
        with open(config_path, 'r') as f:
            config_dict = yaml.safe_load(f)
        
        # Create config object with nested dictionaries
        config = cls()
        
        for section, values in config_dict.items():
            if hasattr(config, section) and isinstance(values, dict):
                section_obj = getattr(config, section)
                for key, value in values.items():
                    if hasattr(section_obj, key):
                        setattr(section_obj, key, value)
        
        return config
    
    def to_yaml(self, config_path: str):
        """Save configuration to YAML file"""
        config_dict = asdict(self)
        
        with open(config_path, 'w') as f:
            yaml.dump(config_dict, f, default_flow_style=False, indent=2)
    
    def validate(self) -> List[str]:
        """Validate configuration and return list of errors"""
        errors = []
        
        # Validate API configuration
        if not self.boltz_api.server_url:
            errors.append("Boltz API server URL is required")
        # API token验证改为警告而非错误，支持本地开发
        if not self.boltz_api.api_token and not os.getenv('BOLTZ_API_TOKEN'):
            print("警告: 未设置Boltz API token，如需连接Boltz服务请设置环境变量 BOLTZ_API_TOKEN")
        
        # Validate MMP database
        if not os.path.exists(self.mmp_database.database_path):
            errors.append(f"MMP database not found: {self.mmp_database.database_path}")
        
        # Validate optimization parameters
        if self.optimization.max_candidates <= 0:
            errors.append("max_candidates must be positive")
        if not (0 < self.optimization.mutation_rate < 1):
            errors.append("mutation_rate must be between 0 and 1")
        
        # Validate scoring weights
        try:
            self.scoring_weights.normalize()
        except ValueError as e:
            errors.append(f"Invalid scoring weights: {e}")
        
        # Validate filter criteria
        if self.filters.mw_max <= 0:
            errors.append("mw_max must be positive")
        if self.filters.parent_similarity_min >= self.filters.parent_similarity_max:
            errors.append("parent_similarity_min must be less than parent_similarity_max")
        
        return errors
    
    def setup_directories(self):
        """Create necessary directories"""
        dirs_to_create = [
            # self.temp_dir,
            os.path.dirname(self.mmp_database.database_path),
            os.path.dirname(self.mmp_database.reference_data_path)
        ]
        
        for dir_path in dirs_to_create:
            if dir_path and not os.path.exists(dir_path):
                os.makedirs(dir_path, exist_ok=True)

# Default configuration instance
DEFAULT_CONFIG = OptimizationConfig()

def load_config(config_path: Optional[str] = None) -> OptimizationConfig:
    """Load configuration from file or return default"""
    if config_path and os.path.exists(config_path):
        return OptimizationConfig.from_yaml(config_path)
    return DEFAULT_CONFIG
