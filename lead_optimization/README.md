# 🧬 Lead Optimization Module

基于MMPDB和Boltz-WebUI的智能先导化合物优化系统，支持迭代进化算法和GPU批次处理。

## 📋 目录
- [概述](#概述)
- [核心特性](#核心特性)
- [安装指南](#安装指南)
- [快速开始](#快速开始)
- [详细使用说明](#详细使用说明)
- [参数说明](#参数说明)
- [输出结果](#输出结果)
- [案例分析](#案例分析)
- [最佳实践](#最佳实践)

## 🎯 概述

Lead Optimization模块是一个现代化的药物先导化合物优化系统，集成了：

- **🧪 MMPDB化学变换引擎** - 基于化学知识的分子设计
- **🔬 Boltz-WebUI结构预测** - GPU加速的蛋白质-配体结构预测
- **🧬 进化优化算法** - 多代迭代的智能优化策略
- **📊 实时监控系统** - 实时CSV输出和进度追踪
- **⚡ 批次处理引擎** - GPU资源优化的并行计算

## ✨ 核心特性

### 🚀 **迭代进化优化**
- **多代遗传算法**: 模拟生物进化过程
- **精英保留策略**: 保持最优化合物传递
- **多样性保护**: 防止过早收敛到局部最优
- **自适应参数调整**: 根据进化进程动态调整策略

### 🔄 **批次处理系统**
- **GPU资源管理**: 控制同时提交的任务数量
- **批次并行处理**: 先提交整批，再等待完成
- **负载均衡**: 避免GPU资源过载
- **错误恢复**: 单个任务失败不影响整批处理

### 📈 **实时监控**
- **实时CSV输出**: 每个化合物完成即写入结果
- **进度实时追踪**: 详细的日志和状态更新  
- **中间结果保存**: 程序中断也不丢失已有结果
- **多维度指标**: 包含binding_probability、plddt、iptm等详细指标

### 🎯 **智能策略选择**
- **化合物类型识别**: 根据分子特征选择最适合的优化策略
- **互补策略组合**: 多种策略协同作用
- **进化引导**: 基于上一代结果指导下一代策略

### 核心模块说明

| 模块 | 功能 | 特点 |
|------|------|------|
| `OptimizationEngine` | 主优化引擎 | 协调各模块，控制优化流程 |
| `BatchEvaluator` | 批次评估器 | GPU批次管理，并行处理 |
| `MolecularEvolutionEngine` | 分子进化引擎 | 遗传算法，进化策略 |
| `MMPEngine` | 分子变换引擎 | MMPDB驱动的化学变换 |
| `MultiObjectiveScoring` | 多目标评分系统 | 综合评估候选化合物 |

## 📦 安装指南

### 系统要求
- **操作系统**: Linux (推荐 Ubuntu 18.04+)
- **Python**: 3.8+
- **内存**: 8GB+ RAM
- **存储**: 50GB+ 可用空间（用于MMPDB数据库）
- **GPU**: 可选，用于Boltz-WebUI加速

### 1. 环境准备

```bash
# 1. 克隆仓库
git clone https://github.com/dahuilangda/Boltz-WebUI.git
cd Boltz-WebUI/lead_optimization

# 2. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac
# venv\Scripts\activate  # Windows

# 3. 安装依赖
pip install -r requirements.txt
```

### 2. MMPDB数据库安装

```bash
# 安装MMPDB
pip install mmpdb

# 下载ChEMBL数据库（推荐使用ChEMBL 35），下载和构建时间非常久，请耐心等待...
python setup_mmpdb.py
```

### 3. 验证安装

```bash
cd lead_optimization
python run_optimization.py --help
```

如果看到完整的参数说明，说明安装成功！

## 🚀 快速开始

### 第一个优化任务

```bash
# 激活虚拟环境
source venv/bin/activate

# 创建简单的目标配置
cat > simple_target.yaml << EOF
sequences:
- protein:
    id: A
    sequence: MKLLILTCLVAVALARPKHPIKHQGLPQEVLNENLLRFFVAPFPEVFGKEKVNELSKDIGSESTEDQAMEDIKQMEEGKVSQIGEMVWQSGHVAFEQFLPKAQAPVKH
EOF

# 运行单次优化
python run_optimization.py \
  --input_compound "CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5" \
  --target_config simple_target.yaml \
  --max_candidates 5 \
  --output_dir my_first_optimization

# 查看实时结果
tail -f my_first_optimization/optimization_results_live.csv
```

### 迭代进化优化示例

```bash
# 运行多代进化优化
python run_optimization.py \
  --input_compound "CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5" \
  --target_config simple_target.yaml \
  --iterations 3 \
  --max_candidates 10 \
  --batch_size 4 \
  --top_k_per_iteration 3 \
  --output_dir evolution_optimization
```

## 📖 详细使用说明

### 命令行界面

```bash
python run_optimization.py [OPTIONS]
```

### 基本用法

1. **单化合物优化**
```bash
python run_optimization.py \
  --input_compound "SMILES_STRING" \
  --target_config target.yaml \
  --output_dir results/
```

2. **批量优化**
```bash
python run_optimization.py \
  --input_file compounds.csv \
  --target_config target.yaml \
  --output_dir batch_results/
```

3. **迭代进化优化**
```bash
python run_optimization.py \
  --input_compound "SMILES_STRING" \
  --target_config target.yaml \
  --iterations 5 \
  --max_candidates 20 \
  --batch_size 4 \
  --top_k_per_iteration 5 \
  --output_dir evolution_results/
```

## 📋 配置文件

### 目标蛋白配置 (target.yaml)

```yaml
# 基础配置
sequences:
  - protein:
      id: A  # 必须使用简单字母ID (A, B, C, D)
      sequence: "MKLLILTCLVAVAL..."  # 蛋白质序列
      
properties:
  - affinity:
      binder: B  # 与配体ID对应
```

### 完整配置示例

```yaml
# 完整的目标蛋白配置
sequences:
  - protein:
      id: A
      sequence: "MKLLILTCLVAVALARPKHPIKHQGLPQEVLNENLLRFFVAPFPEVFGKEKVNELS..."
```

## 📊 输出结果

### 目录结构

```
output_directory/
├── 📄 optimization_results.csv         # 实时结果CSV（主要输出）
├── 📄 optimization_summary.json        # 完整结果JSON
├── 📁 results/                         # Boltz预测详细结果
│   ├── cand_0001/
│   │   ├── affinity_data.json
│   │   ├── confidence_data_model_0.json
│   │   ├── data_model_0.cif
│   │   └── ...
│   ├── cand_0002/
│   └── ...
├── 📁 temp_configs/                    # 临时YAML配置（调试用）
│   ├── cand_0001_config.yaml
│   └── ...
└── 📄 optimization_report.html         # HTML报告（可选）
```

### 实时CSV字段说明

| 字段 | 说明 | 示例值 | 数据来源 |
|------|------|--------|----------|
| `timestamp` | 完成时间 | `2025-08-13 14:42:10` | 系统时间 |
| `compound_id` | 化合物ID | `cand_0001` | 自动生成 |
| `original_smiles` | 原始化合物 | `CN1CCN(CC1)...` | 输入 |
| `optimized_smiles` | 优化后化合物 | `COc1cccc(F)...` | MMPDB生成 |
| `mmp_transformation` | 化学变换ID | `mmp_id_3279` | MMPDB |
| `status` | 任务状态 | `completed` | 系统 |
| `task_id` | Boltz任务ID | `118b971b-...` | Boltz-WebUI |
| `combined_score` | 综合评分 | `0.7017` | 多目标评分 |
| `binding_affinity` | 结合亲和力评分 | `0.8729` | Boltz结果 |
| `drug_likeness` | 药物相似性评分 | `0.7518` | RDKit计算 |
| `synthetic_accessibility` | 合成可及性评分 | `0.8356` | SAScore |
| `novelty` | 新颖性评分 | `0.0000` | 分子相似性 |
| `stability` | 稳定性评分 | `0.3255` | Boltz confidence |
| `plddt` | 结构置信度 | `0.4664` | Boltz预测 |
| `iptm` | 界面预测置信度 | `0.8729` | Boltz预测 |
| `binding_probability` | 结合概率 | `0.5872` | Boltz预测 |
| `ic50_um` | IC50预测值 (μM) | `3.52` | 亲和力转换 |
| `molecular_weight` | 分子量 | `456.78` | RDKit计算 |
| `logp` | 脂溶性 | `2.34` | RDKit计算 |
| `lipinski_violations` | Lipinski违规数 | `1` | RDKit计算 |
| `qed_score` | QED药物相似性 | `0.67` | RDKit计算 |

## 📖 详细使用说明

### 命令行界面

```bash
python run_optimization.py [OPTIONS]
```

### 基本用法

1. **单化合物优化**
```bash
python run_optimization.py \
  --input_compound "SMILES_STRING" \
  --target_config target.yaml \
  --output_dir results/
```

2. **批量优化**
```bash
python run_optimization.py \
  --input_file compounds.csv \
  --target_config target.yaml \
  --output_dir batch_results/
```

3. **迭代进化优化**
```bash
python run_optimization.py \
  --input_compound "SMILES_STRING" \
  --target_config target.yaml \
  --iterations 5 \
  --max_candidates 20 \
  --batch_size 4 \
  --top_k_per_iteration 5 \
  --output_dir evolution_results/
```

## 🔧 参数说明

### 必需参数

| 参数 | 说明 | 示例 |
|------|------|------|
| `--target_config` | 目标蛋白配置文件路径 | `target.yaml` |

### 输入选项

| 参数 | 说明 | 默认值 | 示例 |
|------|------|--------|------|
| `--input_compound` | 输入化合物SMILES | - | `"CN1CCN(CC1)c2ccc..."` |
| `--input_file` | 输入文件（CSV格式） | - | `compounds.csv` |

### 优化参数

| 参数 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `--optimization_strategy` | 优化策略 | `scaffold_hopping` | `scaffold_hopping`, `fragment_replacement`, `multi_objective` |
| `--max_candidates` | 每轮最大候选数 | `50` | 1-1000 |
| `--iterations` | 迭代次数（遗传代数） | `1` | 1-20 |
| `--batch_size` | 批次大小（GPU限制） | `4` | 1-10 |
| `--top_k_per_iteration` | 每轮保留种子数 | `5` | 1-50 |

### 输出选项

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--output_dir` | 输出目录 | 自动生成 |
| `--generate_report` | 生成HTML报告 | `False` |

### 系统选项

| 参数 | 说明 | 默认值 | 范围 |
|------|------|--------|------|
| `--parallel_workers` | 并行工作进程数 | `1` | 1-8 |
| `--verbosity` | 日志级别 | `1` | 0-2 |

### 优化策略详解

| 策略 | 适用场景 | 变化程度 | 推荐用于 |
|------|----------|----------|----------|
| `scaffold_hopping` | 探索新骨架 | 大 | 活性悬崖跨越 |
| `fragment_replacement` | 精细优化 | 中 | 局部结构改进 |
| `multi_objective` | 平衡优化 | 中-大 | 多目标同时优化 |

## 📊 输出结果

### 目录结构

```
output_directory/
├── 📄 optimization_results_live.csv    # 实时结果CSV（主要输出）
├── 📄 optimization_summary.json        # 完整结果JSON
├── 📁 results/                         # Boltz预测详细结果
│   ├── cand_0001/
│   │   ├── affinity_data.json
│   │   ├── confidence_data_model_0.json
│   │   ├── data_model_0.cif
│   │   └── ...
│   ├── cand_0002/
│   └── ...
├── 📁 temp_configs/                    # 临时YAML配置（调试用）
│   ├── cand_0001_config.yaml
│   └── ...
└── 📄 optimization_report.html         # HTML报告（可选）
```

### 实时CSV字段说明

| 字段 | 说明 | 示例值 | 数据来源 |
|------|------|--------|----------|
| `timestamp` | 完成时间 | `2025-08-13 14:42:10` | 系统时间 |
| `compound_id` | 化合物ID | `cand_0001` | 自动生成 |
| `original_smiles` | 原始化合物 | `CN1CCN(CC1)...` | 输入 |
| `optimized_smiles` | 优化后化合物 | `COc1cccc(F)...` | MMPDB生成 |
| `mmp_transformation` | 化学变换ID | `mmp_id_3279` | MMPDB |
| `status` | 任务状态 | `completed` | 系统 |
| `task_id` | Boltz任务ID | `118b971b-...` | Boltz-WebUI |
| `combined_score` | 综合评分 | `0.7017` | 多目标评分 |
| `binding_affinity` | 结合亲和力评分 | `0.8729` | Boltz结果 |
| `drug_likeness` | 药物相似性评分 | `0.7518` | RDKit计算 |
| `synthetic_accessibility` | 合成可及性评分 | `0.8356` | SAScore |
| `novelty` | 新颖性评分 | `0.0000` | 分子相似性 |
| `stability` | 稳定性评分 | `0.3255` | Boltz confidence |
| `plddt` | 结构置信度 | `0.4664` | Boltz预测 |
| `iptm` | 界面预测置信度 | `0.8729` | Boltz预测 |
| `binding_probability` | 结合概率 | `0.5872` | Boltz预测 |
| `ic50_um` | IC50预测值 (μM) | `3.52` | 亲和力转换 |
| `molecular_weight` | 分子量 | `456.78` | RDKit计算 |
| `logp` | 脂溶性 | `2.34` | RDKit计算 |
| `lipinski_violations` | Lipinski违规数 | `1` | RDKit计算 |
| `qed_score` | QED药物相似性 | `0.67` | RDKit计算 |

### JSON摘要格式

```json
{
  "original_compound": "CN1CCN(CC1)c2ccc...",
  "strategy": "scaffold_hopping",
  "iterations": 2,
  "total_candidates": 8,
  "successful_evaluations": 6,
  "success_rate": 0.75,
  "execution_time": 45.2,
  "best_candidate": {
    "compound_id": "cand_0001",
    "smiles": "COc1cccc(F)c1-c1cc2c...",
    "combined_score": 0.7017,
    "improvements": {
      "binding_affinity": "+15%",
      "drug_likeness": "+8%"
    }
  },
  "top_candidates": [...]
}
```

## 💡 案例分析

### 案例1：单次优化

**目标**: 优化一个蛋白酶抑制剂

```bash
# 输入化合物：经典苯并咪唑结构
python run_optimization.py \
  --input_compound "CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5" \
  --target_config protease_target.yaml \
  --optimization_strategy fragment_replacement \
  --max_candidates 20 \
  --output_dir protease_optimization
```

**结果分析**:
- 生成了20个候选化合物
- 最佳化合物提升结合亲和力23%
- 保持了良好的药物相似性
- 合成可及性评分改善15%

### 案例2：迭代进化优化

**目标**: 多代进化寻找最优化合物

```bash
# 3代进化，每代10个候选，批次大小4
python run_optimization.py \
  --input_compound "CC1=CC=C(C=C1)C2=CC=C(C=C2)C3=NN=C(N3)C4=CC=CC=C4" \
  --target_config kinase_target.yaml \
  --iterations 3 \
  --max_candidates 10 \
  --batch_size 4 \
  --top_k_per_iteration 3 \
  --optimization_strategy scaffold_hopping \
  --output_dir kinase_evolution
```

**进化过程**:
- **第1代**: 从原始化合物生成10个候选，选择3个最佳
- **第2代**: 从3个种子生成10个新候选，选择3个最佳  
- **第3代**: 继续进化，发现2个显著改善的化合物

**进化效果**:
- 综合评分从0.62提升到0.84
- 种群多样性保持在0.75以上
- 发现了全新骨架结构

### 案例3：批量化合物优化

**输入文件** (compounds.csv):
```csv
compound_id,smiles
lead_001,CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5
lead_002,CC1=CC=C(C=C1)C2=CC=C(C=C2)C3=NN=C(N3)C4=CC=CC=C4
lead_003,COc1ccc(cc1)C2=CC=C(C=C2)C3=NN=C(N3)N
```

```bash
# 批量优化多个先导化合物
python run_optimization.py \
  --input_file compounds.csv \
  --target_config multi_target.yaml \
  --optimization_strategy multi_objective \
  --max_candidates 15 \
  --generate_report \
  --output_dir batch_optimization
```

**批量结果**:
- 3个原始化合物共生成45个候选
- 成功评估42个候选（93.3%成功率）
- 发现5个优于原始化合物的候选
- 生成详细HTML报告便于比较

## 🎯 最佳实践

### 1. 参数选择指南

#### 迭代次数 (`--iterations`)
- **1代**: 快速探索，适合初步评估
- **2-3代**: 平衡优化，大多数场景推荐
- **4-5代**: 深度优化，复杂分子系统
- **>5代**: 避免过拟合，除非有特殊需求

#### 候选数量 (`--max_candidates`)
- **小分子**: 10-20个候选
- **中等复杂度**: 20-50个候选  
- **复杂分子**: 50-100个候选
- **资源充足**: 可达200-500个

#### 批次大小 (`--batch_size`)
- **GPU内存4GB**: batch_size=2
- **GPU内存8GB**: batch_size=4
- **GPU内存16GB+**: batch_size=6-8
- **CPU模式**: batch_size=1-2

#### 种子数量 (`--top_k_per_iteration`)
- **快速收敛**: top_k = 20-30% of max_candidates
- **平衡探索**: top_k = 30-50% of max_candidates  
- **多样性保护**: top_k = 50%+ of max_candidates

### 2. 优化策略选择

| 分子特征 | 推荐策略 | 理由 |
|----------|----------|------|
| 小分子 (<300 Da) | `structural_elaboration` | 需要增加复杂度 |
| 大分子 (>600 Da) | `fragment_replacement` | 需要简化结构 |
| 多环化合物 | `side_chain_modification` | 保持核心结构 |
| 线性分子 | `scaffold_hopping` | 引入环状结构 |
| 已知活性化合物 | `fragment_replacement` | 精细优化 |
| 新颖骨架探索 | `scaffold_hopping` | 大胆变换 |