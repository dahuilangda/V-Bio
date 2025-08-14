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

Lead Optimization模块是一个集成了MMPDB和Boltz-WebUI，采用遗传算法的智能先导化合物优化系统。

- **🧪 MMPDB化学变换引擎** - 基于化学知识的分子设计
- **🔬 Boltz-WebUI结构预测** - GPU加速的蛋白质-配体结构预测
- **🧬 进化优化算法** - 多代迭代的智能优化策略
- **📊 实时监控系统** - 实时CSV输出和进度追踪

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
# 确保Boltz-WebUI服务器正在运行
cd /data/boltz_webui
python api_server.py &  # 启动后端API服务器

# 切换到lead_optimization目录
cd lead_optimization

# 创建简单的目标配置
cat > simple_target.yaml << EOF
sequences:
- protein:
    id: A
    sequence: MKLLILTCLVAVALARPKHPIKHQGLPQEVLNENLLRFFVAPFPEVFGKEKVNELSKDIGSESTEDQAMEDIKQMEEGKVSQIGEMVWQSGHVAFEQFLPKAQAPVKH
EOF

# 运行单次优化（3个候选化合物）
python run_optimization.py \
  --input_compound "CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5" \
  --target_config simple_target.yaml \
  --max_candidates 5 \
  --output_dir my_first_optimization \
  --generate_report

# 查看实时结果
tail -f my_first_optimization/optimization_results.csv
```

### 迭代进化优化示例

```bash
# 运行多代进化优化（推荐用于深度优化）
python run_optimization.py \
  --input_compound "CC(C)NC1=NC=NC2=C1C=C(C=C2)C3=CC=CC=C3" \
  --target_config simple_target.yaml \
  --iterations 3 \
  --max_candidates 8 \
  --batch_size 4 \
  --top_k_per_iteration 3 \
  --max_chiral_centers 2 \
  --generate_report \
  --output_dir evolution_optimization
```

### 生成HTML报告

```bash
# 添加 --generate_report 参数生成详细的HTML分析报告
python run_optimization.py \
  --input_compound "CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5" \
  --target_config simple_target.yaml \
  --max_candidates 5 \
  --generate_report \
  --output_dir optimization_with_report

# 打开HTML报告查看结果
firefox optimization_with_report/optimization_report.html
```

## 📖 详细使用说明

### 前置条件

1. **确保Boltz-WebUI服务器运行**
```bash
# 在主目录启动API服务器
cd /data/boltz_webui  
python api_server.py &

# 验证服务器状态
curl http://localhost:5000/health
```

2. **验证MMPDB数据库**
```bash
# 检查MMPDB数据库是否存在
ls -la data/chembl_*.mmpdb

# 如果不存在，运行安装脚本
python setup_mmpdb.py
```

### 命令行界面

```bash
python run_optimization.py [OPTIONS]
```

### 基本用法模式

#### 1. 单化合物优化（最常用）
```bash
python run_optimization.py \
  --input_compound "SMILES_STRING" \
  --target_config target.yaml \
  --output_dir results_dir

# 完整示例
python run_optimization.py \
  --input_compound "CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5" \
  --target_config simple_target.yaml \
  --max_candidates 10 \
  --generate_report \
  --output_dir my_optimization
```

#### 2. 批量文件优化
```bash
# 创建输入CSV文件
cat > input_compounds.csv << EOF
compound_id,smiles
compound_1,CN1CCN(CC1)c2ccc(cc2)n3c5c(cn3)cnc(c4c(cccc4F)OC)c5
compound_2,CC(C)NC1=NC=NC2=C1C=C(C=C2)C3=CC=CC=C3
EOF

# 批量优化
python run_optimization.py \
  --input_file input_compounds.csv \
  --target_config simple_target.yaml \
  --output_dir batch_results
```

#### 3. 迭代进化优化（高级用法）
```bash
# 多代进化寻找最优解
python run_optimization.py \
  --input_compound "SMILES_STRING" \
  --target_config target.yaml \
  --iterations 3 \              # 3代进化
  --max_candidates 12 \         # 每代12个候选
  --batch_size 4 \              # GPU批次大小4
  --top_k_per_iteration 4 \     # 每代保留4个最优
  --diversity_weight 0.4 \      # 多样性权重40%
  --generate_report \           # 生成HTML报告
  --output_dir evolution_run
```

### 实时监控和结果查看

#### 监控运行进度
```bash
# 查看实时结果CSV
tail -f output_dir/optimization_results.csv

# 监控日志输出
tail -f optimization.log

# 检查任务状态
watch -n 5 'ls -la output_dir/results/ | wc -l'
```

#### 快速结果查看
```bash
# 查看顶级候选化合物
head -5 output_dir/top_candidates.csv

# 查看优化摘要
cat output_dir/optimization_summary.json | jq '.'

# 统计成功率
awk -F, '$8=="completed" {success++} {total++} END {print "Success rate:", success/total*100"%"}' output_dir/optimization_results.csv
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
```

## 📊 输出结果

### 目录结构

```
output_directory/
├── 📄 optimization_results.csv          # 主要结果CSV文件
├── 📄 optimization_summary.json         # 完整结果JSON摘要
├── � top_candidates.csv               # 顶级候选化合物详细信息
├── �📁 results/                         # Boltz预测详细结果
│   ├── cand_0001/
│   │   ├── affinity_data.json
│   │   ├── confidence_data_model_0.json
│   │   ├── data_model_0.cif
│   │   └── ...
│   ├── cand_0002/
│   └── ...
├── 📁 structures/                       # 分子结构图片（用于HTML报告）
│   ├── compound_1.png
│   ├── compound_2.png
│   └── ...
├── 📁 plots/                           # 分析图表（用于HTML报告）
│   ├── score_distribution.png
│   ├── property_comparison.png
│   ├── confidence_analysis.png
│   └── ...
├── 📄 optimization_report.html          # HTML可视化报告（可选）
└── 📁 temp_configs/                    # 临时YAML配置（调试用）
    ├── cand_0001_config.yaml
    └── ...
```

### 主要结果文件说明

#### 1. optimization_results.csv（核心输出）

包含所有候选化合物的详细评估结果：

| 字段名称 | 说明 | 示例值 | 数据来源 |
|----------|------|--------|----------|
| `timestamp` | 完成时间 | `2025-08-14 03:45:58` | 系统时间 |
| `compound_id` | 化合物唯一标识 | `cand_2132` | 自动生成 |
| `generation` | 进化代数 | `1` | 迭代算法 |
| `parent_compound` | 父代化合物SMILES | `CC(C)NC1=NC...` | 输入/遗传 |
| `optimized_smiles` | 优化后化合物SMILES | `CC(C)N[C@@H](C)...` | MMPDB变换 |
| `transformation_rule` | 化学变换规则ID | `mmp_rule_2192` | MMPDB数据库 |
| `combined_score` | **综合评分** | `0.7435` | 多目标加权 |
| `binding_probability` | **结合概率** | `0.5617` | Boltz预测 |
| `ic50_um` | **IC50预测值(μM)** | `6.796` | 亲和力转换 |
| `plddt` | 结构置信度 | `0.4919` | Boltz confidence |
| `iptm` | 界面预测置信度 | `0.8904` | Boltz confidence |
| `molecular_weight` | 分子量 | `306.413` | RDKit计算 |
| `logp` | 脂水分配系数 | `4.053` | RDKit计算 |
| `status` | 评估状态 | `completed` | 系统状态 |
| `task_id` | Boltz任务ID | `07abc3ea-7875...` | API返回 |

#### 2. top_candidates.csv（精选结果）

只包含评分最高的候选化合物，用于快速查看最佳结果：

```csv
rank,compound_id,smiles,combined_score,binding_probability,ic50_um,molecular_weight,logp,transformation_rule
1,cand_2132,CC(C)N[C@@H](C)Nc1ncnc2ccc(-c3ccccc3)cc12,0.7435,0.5617,6.796,306.413,4.053,mmp_rule_2192
2,cand_2057,CC(C)NC(C)Nc1ncnc2ccc(-c3ccccc3)cc12,0.7400,0.5542,6.477,306.413,4.053,mmp_rule_2114
3,cand_2157,CC(C)Nc1ncccc1ONc1ncnc2ccc(-c3ccccc3)cc12,0.6638,0.5578,4.804,371.444,4.918,mmp_rule_2217
```

#### 3. HTML可视化报告（--generate_report）

生成包含以下内容的交互式HTML报告：

- **🏆 顶级候选化合物展示**
  - 分子结构图（RDKit生成）
  - 详细性质表格
  - 排名和评分可视化
  
- **📈 分析图表**
  - 评分分布图
  - 性质对比图  
  - 置信度分析图
  - 多维散点图
  
- **📊 统计摘要**
  - 优化成功率
  - 性质改善统计
  - 执行时间分析

### 评分系统说明

#### 综合评分计算（combined_score）

```
综合评分 = 0.4 × 结合亲和力评分 + 0.25 × 药物相似性评分 + 0.2 × 合成可及性评分 + 0.1 × 新颖性评分 + 0.05 × 稳定性评分
```

#### 各项评分解释

- **结合亲和力评分**: 基于Boltz预测的binding_probability和iptm
- **药物相似性评分**: 基于Lipinski规则、QED评分等
- **合成可及性评分**: 基于SAScore算法评估
- **新颖性评分**: 与已知化合物的相似性对比
- **稳定性评分**: 基于Boltz结构预测置信度
