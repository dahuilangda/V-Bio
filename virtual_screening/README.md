# 🧬 虚拟筛选工具

基于 Boltz-WebUI 的分子虚拟筛选工具，支持多肽和小分子的高通量筛选。

## ✨ 主要特性

- 🔄 **智能续算功能**: 自动检测并跳过已完成的分子，支持中断后继续
- ⚡ **实时结果处理**: 每完成一个任务立即下载和评分，确保结果不丢失
- 🎯 **自动亲和力计算**: 小分子筛选时自动启用亲和力预测
- 📊 **丰富的分析报告**: 自动生成CSV结果、可视化图表和HTML报告
- 🔧 **灵活的参数控制**: 支持自定义评分权重、分子数量限制等
- 📈 **仅报告生成模式**: 快速重新生成报告而无需重新计算

## 📦 安装依赖

```bash
pip install requests pyyaml numpy pandas matplotlib seaborn
```

**前置条件**: 确保 Boltz-WebUI 服务正在运行（默认地址: `http://localhost:5000`）

## 🚀 快速开始

### 基本用法

```bash
# 小分子筛选（推荐）
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --library_type small_molecule \
    --output_dir small_molecule_results \
    --max_molecules 10 \
    --api_token your_token

# 多肽筛选
python run_screening.py \
    --target data/target.yaml \
    --library data/peptides.fasta \
    --library_type peptide \
    --output_dir peptide_results \
    --max_molecules 10 \
    --api_token your_token
```

### 准备输入文件

#### 1. 目标蛋白文件 (YAML格式)
```yaml
version: 1
sequences:
  - protein:
      id: A
      sequence: "MKLLLLLLLLLLLLLLLLLLLLLLLL..."  # 蛋白质序列
      msa: your_msa_url  # 请使用 Boltz-WebUI 提供的结构预测功能获得 MSA URL
```

#### 2. 分子库文件

**多肽库 (FASTA格式)**
```fasta
>peptide_1
MKLLLLLLLLLLLLLLLL
>peptide_2
AVCDEFGHIKLMNPQRST
```

**小分子库 (CSV格式)**
```csv
molecule_id,molecule_name,smiles
mol_1,Aspirin,CC(=O)OC1=CC=CC=C1C(=O)O
mol_2,Caffeine,CN1C=NC2=C1C(=O)N(C(=O)N2C)C
```

## 📋 命令行参数

### 必需参数
- `--target, -t`: 目标蛋白YAML文件路径
- `--library, -l`: 分子库文件路径
- `--output_dir, -o`: 结果输出目录
- `--api_token`: API访问令牌

### 基本配置
- `--library_type`: 分子类型 (`peptide`/`small_molecule`/`auto`) [默认: auto]
- `--server_url`: Boltz-WebUI服务地址 [默认: http://localhost:5000]
- `--max_molecules`: 最大筛选分子数 [默认: -1 (全部)]
- `--batch_size`: 批处理大小 [默认: 50]
- `--max_workers`: 并行工作线程数 [默认: 4]
- `--timeout`: 单个任务超时时间（秒）[默认: 1800]

### 评分权重参数
- `--binding_affinity_weight`: 结合亲和力权重 [默认: 0.6]
- `--structural_stability_weight`: 结构稳定性权重 [默认: 0.2]
- `--confidence_weight`: 预测置信度权重 [默认: 0.2]

### 高级选项
- `--auto_enable_affinity`: 自动启用亲和力计算（基于分子类型检测）[默认: True]
- `--enable_affinity`: 强制启用亲和力计算 [默认: False]
- `--min_binding_score`: 最小结合评分阈值 [默认: 0.0]
- `--top_n`: 保留顶部结果数量 [默认: 100]
- `--use_msa_server`: 使用MSA服务器 [默认: False]

### 输出控制
- `--save_structures`: 保存3D结构 [默认: True]
- `--generate_plots`: 生成分析图表 [默认: True]
- `--report_only`: 仅重新生成报告和CSV文件（基于现有结果）[默认: False]

### 其他选项
- `--log_level`: 日志级别 (`DEBUG`/`INFO`/`WARNING`/`ERROR`) [默认: INFO]
- `--force`: 强制覆盖现有输出目录 [默认: False]
- `--dry_run`: 仅验证配置，不执行筛选 [默认: False]

查看完整参数：`python run_screening.py --help`

## 🔄 智能续算功能

本工具具备强大的智能续算功能，能够自动检测和继续未完成的筛选任务。

### 自动检测机制
- 运行相同命令时自动检测输出目录中的现有结果
- 智能跳过已完成的分子，只处理剩余分子
- 自动合并新旧结果，重新生成完整的分析报告
- 支持实时结果下载，每完成一个任务立即保存，确保续算可靠性

### 工作原理
```bash
# 第一次运行（处理前3个分子）
python run_screening.py --target data/target.yaml --library data/molecules.csv --output_dir small_molecule_results --max_molecules 3 --api_token your_token

# 扩展到5个分子（自动跳过前3个，只处理新的2个）
python run_screening.py --target data/target.yaml --library data/molecules.csv --output_dir small_molecule_results --max_molecules 5 --api_token your_token

# 如果中断后，直接重新运行相同命令即可自动续算
python run_screening.py --target data/target.yaml --library data/molecules.csv --output_dir small_molecule_results --max_molecules 5 --api_token your_token
```

### 检测条件
系统会自动检测以下文件来判断是否需要续算：
- 结果文件（`screening_results_complete.csv`、`top_hits.csv`）
- 任务目录（`tasks/task_*.json` 和结果目录）
- 配置文件（`screening_config.json`）

### 强制重新开始
如需重新开始筛选（忽略现有结果），使用 `--force` 选项：
```bash
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --output_dir small_molecule_results \
    --api_token your_token \
    --force
```

## 📊 仅重新生成报告

如果筛选已完成，但需要重新生成报告或CSV文件（例如中断后报告未生成），可以使用 `--report_only` 选项：

```bash
# 基于现有结果重新生成所有报告文件
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --output_dir results \
    --report_only \
    --api_token your_token
```

此模式会：
- 快速加载现有的筛选结果
- 重新生成 CSV 文件（`screening_results_complete.csv`、`top_hits.csv`）
- 重新生成所有分析图表
- 重新生成 HTML 报告
- 重新生成筛选摘要文件

## 💡 使用示例

### 多肽筛选
```bash
python run_screening.py \
    --target data/protein_target.yaml \
    --library data/peptides.fasta \
    --library_type peptide \
    --output_dir peptide_results \
    --max_molecules 100 \
    --api_token your_token
```

### 小分子筛选
```bash
python run_screening.py \
    --target data/protein_target.yaml \
    --library data/small_molecules.csv \
    --library_type small_molecule \
    --output_dir drug_results \
    --binding_affinity_weight 0.8 \
    --max_molecules 1000 \
    --api_token your_token
```

### 快速测试（少量分子）
```bash
python run_screening.py \
    --target data/example_target.yaml \
    --library data/example_library.csv \
    --output_dir test_run \
    --max_molecules 5 \
    --batch_size 1 \
    --max_workers 1 \
    --api_token your_token
```

### 续算示例（增加筛选数量）
```bash
# 第一次运行100个分子
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --output_dir results \
    --max_molecules 100 \
    --api_token your_token

# 扩展到500个分子（自动跳过已完成的100个）
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --output_dir results \
    --max_molecules 500 \
    --api_token your_token
```

### 重新生成报告
```bash
# 仅重新生成报告，不重新计算
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --output_dir results \
    --report_only \
    --api_token your_token
```

### 自定义评分权重
```bash
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --output_dir custom_scoring \
    --binding_affinity_weight 0.7 \
    --structural_stability_weight 0.2 \
    --confidence_weight 0.1 \
    --api_token your_token
```

## 📁 输出文件说明

筛选完成后，输出目录将包含以下文件：

### 主要结果文件
- `screening_results_complete.csv`: 完整的筛选结果，包含所有分子的详细评分
- `top_hits.csv`: 按评分排序的顶部结果
- `screening_summary.json`: 筛选任务的总体统计信息
- `screening_config.json`: 本次筛选使用的配置参数

### 可视化图表 (`plots/` 目录)
- `score_distribution.png`: 评分分布图
- `screening_funnel.png`: 筛选漏斗图
- `top_molecules.png`: 前20名分子图表
- `screening_radar.png`: 筛选雷达图
- `ic50_binding_analysis.png`: IC50和结合概率分析
- `affinity_analysis.png`: 亲和力分析图
- `molecular_complexity.png`: 分子复杂度分析

### 详细数据 (`tasks/` 目录)
- `task_*.json`: 每个任务的记录文件
- `task_*/`: 每个任务的详细结果目录，包含：
  - `confidence_data_model_0.json`: 置信度数据
  - `affinity_data.json`: 亲和力预测数据
  - `data_model_0.cif`: 预测的3D结构文件
  - 其他结构和分析文件

### 报告文件
- `screening_report.html`: 交互式HTML报告，包含所有图表和分析

## 🔧 故障排除

### 常见问题

1. **服务器连接失败**
   ```
   确保 Boltz-WebUI 服务正在运行：http://localhost:5000
   ```

2. **续算功能未生效**
   ```
   检查输出目录是否包含 tasks/ 文件夹或 screening_results_complete.csv
   ```

3. **任务超时**
   ```
   增加 --timeout 参数（默认1800秒）
   ```

### 日志调试
```bash
# 启用详细日志
python run_screening.py \
    --target data/target.yaml \
    --library data/molecules.csv \
    --output_dir results \
    --log_level DEBUG \
    --api_token your_token
```