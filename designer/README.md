# 🧬 De Novo 多肽/糖肽/双环肽设计器

一款用于多肽、糖肽及双环肽从头设计 (`de novo design`) 的命令行工具，专为科研人员设计。该工具通过调用本地部署的 `Boltz-WebUI` 预测服务作为计算后端，在序列空间中进行探索，以发现具有高结合潜力的新分子。

## 🚀 快速开始

### 🔨 基础肽设计

```bash
# 设置API密钥
export API_SECRET_TOKEN='your-token'

# 运行基础肽设计
python run_design.py \
    --design_type "linear" \
    --yaml_template template.yaml \
    --binder_chain "B" \
    --binder_length 20 \
    --iterations 30
```

### 🎯 约束肽设计（使用序列掩码）

```bash
# 固定关键位置的氨基酸进行设计
python run_design.py \
    --design_type "linear" \
    --yaml_template template.yaml \
    --binder_chain "B" \
    --binder_length 15 \
    --sequence_mask "X-W-X-F-X-X-X-R-X-D-X-X-X-P-X" \
    --iterations 25
```

### 🍬 糖肽设计（使用boltz1模型）

```bash
# 初始化糖肽CCD缓存（首次使用）
python designer/glycopeptide_generator.py --generate-all

# 运行糖肽设计
python run_design.py \
    --design_type "glycopeptide" \
    --yaml_template template.yaml \
    --binder_chain "B" \
    --binder_length 15 \
    --glycan_modification "MANS" \
    --modification_site 5 \
    --iterations 25
```

### 🚲 双环肽设计

```bash
# 运行双环肽设计
python run_design.py \
    --design_type "bicyclic" \
    --yaml_template template.yaml \
    --binder_chain "B" \
    --binder_length 25 \
    --cys_positions 4 15 \
    --linker_ccd "SEZ" \
    --iterations 10
```

## ✨ 核心特性

### 🔬 基础功能

  - **生物学约束**: 设计糖肽时，工具会自动验证并确保糖基连接到化学上兼容的氨基酸残基上（例如，N-连锁聚糖连接到天冬酰胺'N'）。
  - **pLDDT指导的突变**: 演化过程优先在结构预测置信度较低 (pLDDT分数低) 的区域引入突变，以高效探索构象空间。
  - **并行评估**: 在每个演化代数中，通过多线程并行提交和评估多个候选序列，显著加快设计周期。
  - **🎯 序列掩码约束**: **(新增)** 支持指定序列中固定位置的氨基酸，允许保持已知的活性位点或结构motif，同时优化其他可变位置。

### 🚀 增强版功能

  - **自适应突变策略**: 5种智能突变策略自动选择和学习
      - 保守突变：基于BLOSUM62矩阵的高分替换
      - 激进突变：大范围序列空间探索
      - Motif导引：基于有益模式的定向突变
      - 能量导引：基于能量景观的温度控制
      - 多样性驱动：防止群体过度收敛
  - **Pareto多目标优化**: 同时优化ipTM和pLDDT，避免单一目标偏向
  - **智能收敛检测**: 自动早停机制，防止过度训练和资源浪费
  - **序列模式学习**: 位置特异性偏好学习和motif自动发现
  - **群体多样性维护**: 实时监测和调整探索策略

### 🍬 糖肽设计功能

  - **自动模型选择**: 检测到糖肽修饰时自动使用 `boltz1` 模型，无需手动指定。
  - **化学正确性验证**: 确保糖苷键形成遵循正确的化学原理。

### 🚲 双环肽设计功能

  - **自动环化约束**: 自动在YAML中生成连接体（ligand）和键约束（constraints），将肽链中的三个半胱氨酸（Cys）与指定连接体共价连接。
  - **位置可变的半胱氨酸**: 保证一个Cys残基固定在肽链末端，另外两个的位置可在设计过程中动态优化，探索最佳环化构象。
  - **可扩展的连接体库**: 支持多种连接体（当前实现`SEZ`），并为未来添加新连接体提供了接口。
  - **专用模型支持**: 与糖肽设计类似，自动调用`boltz1`模型以准确预测非标准拓扑结构。

## 🔧 环境准备

1.  **Boltz-WebUI 服务**: 确保 `Boltz-WebUI` 平台正在本地运行且网络可访问。
2.  **Python 依赖**: 安装所需的 Python 库。
    ```bash
    pip install requests pyyaml numpy
    ```

## 🚀 使用指南

通过在终端运行 `run_design.py` 脚本来启动设计流程。

### 步骤 1: 准备模板YAML文件

创建一个YAML文件，用于描述系统中静态不变的部分（例如，受体蛋白）。待设计的链（binder）不应包含在此模板中，脚本会依据命令行参数动态生成。

#### **示例 1: 标准蛋白Binder设计**

模板应包含除待设计链之外的所有链。

**`template_protein.yaml`:**

```yaml
version: 1
sequences:
- protein:
    id: A
    # 此处填写靶蛋白的完整序列
    sequence: MTEYKLVVVGAGGVGKSALTVQFVQGIFVEYDPTHFESTEKT.... 
    msa: empty
# 注意: 待设计的链（例如 B 链）将由脚本自动添加。
```

#### **示例 2: 糖肽设计**

对于糖肽设计，模板只包含受体蛋白。

**`template_glycopeptide.yaml`:**

```yaml
version: 1
sequences:
- protein:
    id: A
    # 靶点受体蛋白的完整序列
    sequence: MTEYKLVVVGAGGVGKSALTVQFVQGIFVEYDPTHFESTEKT....
    msa: empty
# 脚本将根据命令行参数自动添加肽链、聚糖以及它们之间的共价键。
```

#### **示例 3: 双环肽设计**

模板与糖肽设计类似，仅包含受体蛋白。

**`template_bicyclic.yaml`:**

```yaml
version: 1
sequences:
- protein:
    id: A
    # 靶点受体蛋白的完整序列
    sequence: MTEYKLVVVGAGGVGKSALTVQFVQGIFVEYDPTHFESTEKT....
    msa: empty
# 脚本将自动添加包含3个半胱氨酸的肽链、连接体配体以及它们之间的成键约束。
```

### 步骤 2: 设置API令牌

建议将API密钥设置为环境变量。

```bash
# Linux / macOS
export API_SECRET_TOKEN='your-super-secret-and-long-token'

# Windows (Command Prompt)
set API_SECRET_TOKEN="your-super-secret-and-long-token"
```

也可以通过 `--api_token` 参数直接提供。

### 步骤 3: 运行设计脚本

以下为运行脚本的命令示例。

#### **命令示例 1: 基础设计（自动使用增强功能）**

```bash
python run_design.py \
    --design_type "linear" \
    --yaml_template /path/to/your/template_protein.yaml \
    --binder_chain "B" \
    --binder_length 100 \
    --iterations 50 \
    --population_size 16 \
    --num_elites 4 \
    --weight-iptm 0.6 \
    --weight-plddt 0.4 \
    --output_csv "binder_design_run_summary.csv"
```

#### **命令示例 2: 平稳优化模式（推荐）**

适用于需要稳定探索的场景，减少过度波动：

```bash
python run_design.py \
    --design_type "linear" \
    --yaml_template /path/to/your/template_protein.yaml \
    --binder_chain "B" \
    --binder_length 12 \
    --initial_binder_sequence "GGLVYEWQRWAG" \
    --iterations 20 \
    --population_size 16 \
    --num_elites 4 \
    --convergence-window 5 \
    --convergence-threshold 0.001 \
    --max-stagnation 3 \
    --initial-temperature 1.0 \
    --output_csv "binder_optimization_stable.csv"
```

#### **命令示例 3: 激进探索模式**

适用于需要快速突破局部最优的场景：

```bash
python run_design.py \
    --design_type "linear" \
    --yaml_template /path/to/your/template_protein.yaml \
    --binder_chain "B" \
    --binder_length 15 \
    --iterations 30 \
    --population_size 12 \
    --num_elites 2 \
    --convergence-window 3 \
    --max-stagnation 2 \
    --initial-temperature 2.0 \
    --min-temperature 0.2 \
    --output_csv "aggressive_design_run.csv"
```

#### **命令示例 4: 糖肽设计（增强版）**

此示例设计一个长度为15个残基的肽，并在其第3个位置连接一个甘露糖（通过`MANS`修饰）：

```bash
python run_design.py \
    --design_type "glycopeptide" \
    --yaml_template /path/to/your/template_glycopeptide.yaml \
    --binder_chain "B" \
    --binder_length 15 \
    --iterations 30 \
    --population_size 12 \
    --num_elites 3 \
    --glycan_modification "MANS" \
    --modification_site 3 \
    --glycan_chain "C" \
    --weight-iptm 0.7 \
    --weight-plddt 0.3 \
    --convergence-window 4 \
    --max-stagnation 3 \
    --output_csv "glycopeptide_enhanced_design.csv" \
    --keep_temp_files
```

#### **命令示例 5: 双环肽设计**

此示例设计一个长度为25的肽链，其第4位、第15位和末位（第25位）为半胱氨酸，并通过`SEZ`连接体形成双环结构。

```bash
python run_design.py \
    --design_type "bicyclic" \
    --yaml_template /path/to/your/template_bicyclic.yaml \
    --binder_chain "B" \
    --binder_length 25 \
    --iterations 40 \
    --population_size 16 \
    --num_elites 4 \
    --linker_ccd "SEZ" \
    --cys_positions 4 15 \
    --output_csv "bicyclic_peptide_design.csv" \
    --keep_temp_files
```

> **🤖 自动模型选择**: 当使用 `--design_type "glycopeptide"` 或 `--design_type "bicyclic"` 时，系统会自动采用 `boltz1` 模型进行预测，无需手动指定。这确保了包含非标准残基或拓扑的分子能够得到正确的结构预测。

#### **命令示例 6: 序列掩码约束设计** 🎯

此示例展示如何在设计过程中固定特定位置的氨基酸（如保持活性位点或结构motif）：

```bash
python run_design.py \
    --design_type "linear" \
    --yaml_template /path/to/your/template_protein.yaml \
    --binder_chain "B" \
    --binder_length 12 \
    --sequence_mask "X-W-X-X-F-X-X-R-G-D-X-X" \
    --iterations 25 \
    --population_size 10 \
    --num_elites 3 \
    --output_csv "constrained_design.csv"
```

在此例中：
- 位置2固定为色氨酸(W) - 可能的疏水相互作用位点
- 位置5固定为苯丙氨酸(F) - 芳香环相互作用
- 位置8固定为精氨酸(R) - 带正电荷，可能的静电相互作用
- 位置9固定为甘氨酸(G) - 提供构象灵活性
- 位置10固定为天冬氨酸(D) - 带负电荷，可能的氢键或盐桥
- 其他位置(X)允许自由优化

**序列掩码格式支持:**
```bash
# 使用连字符 (推荐)
--sequence_mask "X-W-X-X-F-X-X-R-G-D-X-X"

# 使用下划线
--sequence_mask "X_W_X_X_F_X_X_R_G_D_X_X"

# 使用空格
--sequence_mask "X W X X F X X R G D X X"

# 不使用分隔符
--sequence_mask "XWXXFXXRGDXX"
```

#### **命令示例 7: 传统模式（兼容性）**

如需使用传统算法（不推荐，仅用于对比）：

```bash
python run_design.py \
    --design_type "linear" \
    --yaml_template /path/to/your/template_protein.yaml \
    --binder_chain "B" \
    --binder_length 20 \
    --iterations 25 \
    --disable-enhanced \
    --output_csv "traditional_design.csv"
```

## ⚙️ 命令行参数详解

#### 设计模式

  - `--design_type`: **(必需)** 选择设计类型。可选值: `linear` (线性多肽), `glycopeptide` (糖肽), `bicyclic` (双环肽)。 (默认: `linear`)

#### 输入与目标定义

  - `--yaml_template` **(必需)**: 模板YAML文件的路径。
  - `--binder_chain` **(必需)**: 待设计肽链的链ID (例如, "B")。
  - `--binder_length` **(必需)**: 待设计肽链的长度。
  - `--initial_binder_sequence`: (可选) 提供一个初始肽链序列作为优化的起点。如果未提供，将从随机序列开始。
  - `--sequence_mask`: **(新增)** 序列掩码，用于指定固定位置的氨基酸。格式：`X-A-X-L-X`，其中X表示可变位置，字母表示固定氨基酸。长度必须与 `binder_length` 匹配。支持多种分隔符（`-`、`_`、空格）。
  - `--user_constraints`: (可选) 用户定义约束的JSON文件路径。约束将应用于生成的结合肽。

#### 演化算法控制

  - `--iterations`: 优化循环（代数）的次数。 (默认: `20`)
  - `--population_size`: 每代并行评估的候选数量。 (默认: `8`)
  - `--num_elites`: 保留到下一代的顶级候选（精英）数量。必须小于`population_size`。 (默认: `2`)
  - `--weight-iptm` / `--weight-plddt`: 分别是复合评分函数中 `ipTM` 和 `pLDDT` 的权重。 (默认: `0.7` / `0.3`)

#### 增强功能选项 🚀

  - `--enable-enhanced` / `--disable-enhanced`: 启用/禁用增强版功能。(默认: 启用)
  - `--convergence-window`: 收敛检测的滑动窗口大小。较小值更敏感。 (默认: `5`)
  - `--convergence-threshold`: 收敛检测的分数方差阈值。较小值更严格。 (默认: `0.001`)
  - `--max-stagnation`: 触发早停的最大停滞周期数。较小值更激进。 (默认: `3`)
  - `--initial-temperature`: 自适应突变的初始温度。较高值更探索性。 (默认: `1.0`)
  - `--min-temperature`: 自适应突变的最小温度。较高值保持更多随机性。 (默认: `0.1`)

#### 🎯 优化模式推荐

| 模式 | 收敛窗口 | 停滞阈值 | 初始温度 | 适用场景 |
|------|----------|----------|----------|----------|
| **平稳优化** | 5 | 3 | 1.0 | 标准设计，稳定收敛 |
| **激进探索** | 3 | 2 | 2.0 | 突破局部最优，快速探索 |
| **精细调优** | 7 | 4 | 0.8 | 已有好序列，精细优化 |
| **保守设计** | 6 | 5 | 0.5 | 对稳定性要求高的场景 |

#### 糖肽设计 (当 `--design_type=glycopeptide` 时)

  - `--glycan_modification`: 激活糖肽设计模式。提供糖肽修饰的4字母CCD代码 (例如, `MANS`, `NAGS`, `GALT`)。这些修饰已预生成到CCD缓存中。 (默认: `None`)
  - `--modification_site`: 在binder序列上应用糖肽修饰的位置 **(1-based索引)**。如果使用了`--glycan_modification`，此项为**必需**。
  - `--glycan_chain`: 分配给聚糖配体的唯一链ID。 (默认: `C`)

##### 🍬 糖肽修饰CCD缓存初始化

在使用糖肽设计功能之前，需要先初始化Boltz CCD缓存，将糖基化修饰的非标准氨基酸添加到缓存中。这基于Benjamin Fry的通用共价残基修饰方法。

**快速初始化所有糖肽修饰：**

```bash
cd designer/
python glycopeptide_generator.py --generate-all
```

这将生成24种糖肽修饰组合并添加到Boltz CCD缓存：

| 糖基 | 氨基酸 | CCD代码 | 连接类型 | 化学反应 |
|------|--------|---------|----------|----------|
| MAN (甘露糖) | N,S,T,Y | MANN,MANS,MANT,MANY | N-连接/O-连接 | Ser-OG + MAN-C1-OH → Ser-OG-MAN + H2O |
| NAG (N-乙酰葡糖胺) | N,S,T,Y | NAGN,NAGS,NAGT,NAGY | N-连接/O-连接 | Asn-ND2 + NAG-C1-OH → Asn-ND2-NAG + H2O |
| GAL (半乳糖) | N,S,T,Y | GALN,GALS,GALT,GALY | N-连接/O-连接 | Thr-OG1 + GAL-C1-OH → Thr-OG1-GAL + H2O |
| FUC (岩藻糖) | N,S,T,Y | FUCN,FUCS,FUCT,FUCY | N-连接/O-连接 | Tyr-OH + FUC-C1-OH → Tyr-OH-FUC + H2O |
| GLC (葡萄糖) | N,S,T,Y | GLCN,GLCS,GLCT,GLCY | N-连接/O-连接 | 脱水缩合反应 |
| XYL (木糖) | N,S,T,Y | XYLN,XYLS,XYLT,XYLY | N-连接/O-连接 | 脱水缩合反应 |

**生成特定修饰：**

```bash
# 生成丝氨酸-甘露糖修饰
python designer/glycopeptide_generator.py --specific S MAN

# 生成天冬酰胺-N-乙酰葡糖胺修饰
python designer/glycopeptide_generator.py --specific N NAG
```

**查看可用修饰：**

```bash
python designer/glycopeptide_generator.py --list-only
```

**注意事项：**

  - 首次使用前必须运行 `--generate-all` 初始化CCD缓存
  - 系统会自动检测Boltz缓存位置（通常在 `~/.boltz/ccd.pkl`）

#### 双环肽设计 (当 `--design_type=bicyclic` 时)

  - `--linker_ccd`: 用于形成双环的连接体配体的CCD代码。 (默认: `SEZ`)
  - `--cys_positions`: (可选) 提供两个**1-based索引**，用于指定除C端残基外的另外两个半胱氨酸的**初始位置**。例如 `--cys_positions 4 10`。如果未提供，将随机选择两个初始位置。注：第三个半胱氨酸被强制固定在肽链的最后一个位置。

#### 输出与日志

  - `--output_csv`: 保存所有已评估设计结果的CSV汇总文件的路径。 (默认: `design_summary_<timestamp>.csv`)
  - `--keep_temp_files`: 若设置，则在运行结束后不删除临时工作目录。

#### API 连接

  - `--server_url`: 运行中的Boltz-WebUI预测API的URL。 (默认: `http://127.0.0.1:5000`)
  - `--api_token`: API密钥。建议通过 `API_SECRET_TOKEN` 环境变量设置。
  - `--no_msa_server`: 禁用MSA服务器。**默认情况下MSA服务器已启用**，当序列找不到MSA缓存时会自动生成MSA以提高预测精度。使用此参数可禁用MSA生成以加快预测速度（但可能降低精度）。 (默认: `False`)

## 📊 输出解读

程序运行结束后，会生成以下输出：

  - **控制台日志**: 实时显示演化进程、每一代的最佳分数、警告和错误信息。
  - **CSV汇总文件 (`--output_csv`)**: 包含所有已评估序列的详细信息及其评分指标（`composite_score`, `ipTM`, `pLDDT` 等）。
      - 🆕 **新增字段**: `mutation_strategy` (使用的突变策略), `is_pareto_optimal` (是否为Pareto最优解)
      - 文件已按综合分数从高到低排序。
  - **临时文件 (`--keep_temp_files`)**: 如果选择保留，工作目录中将包含每次API调用的输入（YAML）、输出（PDB/CIF）和置信度文件，可用于后续分析或调试。

### 📈 性能监控

增强版提供更丰富的运行状态信息：

```
[INFO] Generation 5 complete. Best score: 0.8542 (ipTM: 0.8901, pLDDT: 78.43)
[INFO] Enhanced features enabled with custom parameters
[DEBUG] Strategy success rates: conservative=0.75, aggressive=0.32, motif_guided=0.68
[DEBUG] Population diversity - similarity: 0.423, entropy: 2.87
[INFO] Convergence detected: score variance 0.0008 < threshold 0.001
[INFO] Early stopping triggered after 3 stagnation cycles
```

## 🛠️ 故障排除

### 常见问题

**问题**: 序列掩码长度不匹配错误

```bash
# 错误示例：掩码长度与肽链长度不匹配
python run_design.py --binder_length 10 --sequence_mask "X-A-X-L-X"  # 只有5个位置

# 解决方案：确保掩码长度与肽链长度完全匹配
python run_design.py --binder_length 10 --sequence_mask "X-A-X-L-X-X-X-P-X-X"  # 10个位置
```

**问题**: 序列掩码包含无效字符

```bash
# 错误示例：使用了非标准氨基酸字符
--sequence_mask "X-A-X-B-X"  # B不是标准氨基酸

# 解决方案：只使用20种标准氨基酸和X
--sequence_mask "X-A-X-L-X"  # 正确
```

**问题**: 糖肽设计报错 "CCD代码未找到"

```bash
# 解决方案：初始化糖肽CCD缓存
python designer/glycopeptide_generator.py --generate-all
```

**问题**: API连接失败

```bash
# 检查Boltz-WebUI是否运行在正确端口
curl http://127.0.0.1:5000/health

# 检查API密钥是否正确设置
echo $API_SECRET_TOKEN
```

### 最佳实践

  - **MSA设置**: 保持默认设置（MSA服务器启用）以获得最佳预测质量
  - **收敛参数**: 对于糖肽或双环肽等复杂设计，推荐使用较小的收敛窗口（3-4）以应对复杂性
  - **温度控制**: 复杂设计任务可以提高初始温度（1.5-2.0）增强探索能力
  - **🎯 序列掩码使用**:
    - 固定已知的活性位点或关键相互作用残基
    - 避免过度约束（固定位置过多会限制设计空间）
    - 对双环肽设计时注意半胱氨酸位置由系统自动管理
    - 结合初始序列（`--initial_binder_sequence`）使用时确保兼容性