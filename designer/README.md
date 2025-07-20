# 🧬 De Novo 多肽/糖肽设计器

这是一款专为科研人员打造的高效多肽与糖肽从头设计 (de novo design) 命令行工具。它能够调用本地部署的 `Boltz-WebUI` 预测服务作为计算引擎，通过智能化的演化算法，在庞大的序列空间中探索并发现具有高亲和潜力的新分子。

## 核心特性

- **生物学感知设计**: 在设计糖肽时，工具会自动验证并确保糖基连接到化学上兼容的氨基酸残基上（例如，N-连锁聚糖连接到天冬酰胺'N'）。
- **pLDDT指导的突变**: 演化过程优先在结构预测置信度较低的区域引入突变，从而高效地探索构象空间。
- **并行评估**: 在每个演化代数中，利用多线程并行提交和评估多个候选序列，显著加快设计速度。
- **专业的日志系统**: 集成了详细的日志记录，包括时间戳、事件级别和模块来源，便于监控和调试。
- **灵活的配置**: 通过命令行参数可以轻松控制演化算法的各项参数、目标分子属性以及输出选项。

## 环境准备

1.  **Boltz-WebUI 服务**: 确保您的 `Boltz-WebUI` 平台正在本地运行且网络可访问。
2.  **Python 依赖**: 安装所有必需的 Python 库。
    ```bash
    pip install requests pyyaml numpy
    ```

## 🚀 使用指南

设计流程通过在终端运行 `run_design.py` 脚本来启动。

### 步骤 1: 准备模板YAML文件

创建一个YAML文件，用于描述系统中静态不变的部分（例如，受体蛋白）。**待设计的链（binder）不应包含在此模板中**，脚本会根据命令行参数动态生成它。

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
````

#### **示例 2: 糖肽设计**

对于糖肽设计，模板甚至更简单，通常只包含受体蛋白。

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

### 步骤 2: 设置API令牌

为了安全，强烈建议将您的API密钥设置为环境变量。

```bash
# Linux / macOS
export API_SECRET_TOKEN='your-super-secret-and-long-token'

# Windows (Command Prompt)
set API_SECRET_TOKEN="your-super-secret-and-long-token"
```

您也可以通过 `--api_token` 参数直接提供，但不推荐。

### 步骤 3: 运行设计脚本

#### **命令示例: 蛋白Binder设计**

```bash
python run_design.py \
    --yaml_template /path/to/your/template_protein.yaml \
    --binder_chain "B" \
    --binder_length 100 \
    --iterations 50 \
    --population_size 16 \
    --num_elites 4 \
    --weight-iptm 0.6 \
    --weight-plddt 0.4 \
    --output_csv "protein_design_run_summary.csv"
```

#### **命令示例: 糖肽设计**

此示例设计一个长度为15个残基的肽，并在其**第3个位置**连接一个甘露糖（'MAN'）。

```bash
python run_design.py \
    --yaml_template /path/to/your/template_glycopeptide.yaml \
    --binder_chain "B" \
    --binder_length 15 \
    --iterations 30 \
    --population_size 12 \
    --num_elites 3 \
    --glycan_ccd "MAN" \
    --glycosylation_site 3 \
    --glycan_chain "C" \
    --weight-iptm 0.7 \
    --weight-plddt 0.3 \
    --output_csv "glycopeptide_design_run_summary.csv" \
    --keep_temp_files
```

## 📄 命令行参数详解

#### 输入与目标定义

  - `--yaml_template` **(必需)**: 模板YAML文件的路径。
  - `--binder_chain` **(必需)**: 待设计肽链的链ID (例如, "B")。
  - `--binder_length` **(必需)**: 待设计肽链的长度。

#### 演化算法控制

  - `--iterations`: 优化循环（代数）的次数。 (默认: `20`)
  - `--population_size`: 每代并行评估的候选数量。 (默认: `8`)
  - `--num_elites`: 保留到下一代的顶级候选（精英）数量。必须小于`population_size`。 (默认: `2`)

#### 糖肽设计 (可选)

  - `--glycan_ccd`: 激活糖肽设计模式。提供聚糖的3字母PDB CCD代码 (例如, `MAN`, `NAG`, `GAL`)。 (默认: `None`)
  - `--glycosylation_site`: 在binder序列上共价连接聚糖的位置 **(1-based索引)**。如果使用了`--glycan_ccd`，则此项为**必需**。
  - `--glycan_chain`: 分配给聚糖配体的唯一链ID。 (默认: `C`)

#### 输出与日志

  - `--output_csv`: 保存所有已评估设计结果的CSV汇总文件的路径。 (默认: `design_summary_<timestamp>.csv`)
  - `--keep_temp_files`: 若设置，则在运行结束后不删除临时工作目录，便于调试。

#### API 连接

  - `--server_url`: 正在运行的Boltz-WebUI预测API的URL。 (默认: `http://127.0.0.1:5000`)
  - `--api_token`: 您的API密钥。推荐通过 `API_SECRET_TOKEN` 环境变量设置。

## 📈 输出解读

  - **控制台日志**: 实时显示演化进程，包括每一代的最佳分数、警告和错误信息。
  - **CSV汇总文件 (`--output_csv`)**: 包含所有已评估序列的详细信息，按最终分数（ipTM）从高到低排序，便于分析和筛选最佳候选者。
  - **临时文件 (`--keep_temp_files`)**: 如果选择保留，工作目录中将包含每次API调用的YAML输入、PDB/CIF输出和置信度JSON文件，可用于深入的结构分析。