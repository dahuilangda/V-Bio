# V-Bio API Access 技能

[English](README.md)

在自己的电脑或服务器上运行助手，从当前工作区准备、审阅、提交和跟踪 V-Bio 任务。

| 工作流 | 技能支持的操作 |
| --- | --- |
| 结构预测（Prediction） | 准备受支持的分子输入，提交结构预测任务并下载结果。 |
| 虚拟筛选（Virtual Screening） | 针对项目已配置的靶标提交化合物库，并获取筛选结果。 |
| 分子对接（Docking / Affinity） | 按项目支持的模式提交蛋白和配体输入，运行对接或评估已有构象，并下载结果。 |

可用方法和输入组合以你的 V-Bio 部署中的 Command Builder 为准。多肽设计（Peptide Designer）和先导优化（Lead Optimization）通过 V-Bio 网页提交。涉及修饰残基、环化或立体化学时，需要核对所选方法能否保留这些特征。 [能力边界](vbio/references/capabilities.md) · [输入规则](vbio/references/command-builder.md) · [多肽兼容性](vbio/references/peptide-compatibility.md)

## 安装与连接

将完整的 [`vbio/`](vbio/) 目录复制到运行助手的机器上。按工作区安装时，Codex 使用 `.agents/skills/vbio/`，Claude Code 使用 `.claude/skills/vbio/`。安装后从客户端的技能列表中选择 `vbio`。随包客户端需要 Python 3.8 或更新版本以及 curl。提交任务、查询状态和下载结果时，这台机器需要能访问 V-Bio API Access 网关。

在 V-Bio 的 **API → Command Builder → Environment** 中查看完整网关地址和项目 ID。在运行助手的机器上设置以下变量。

```bash
export VBIO_API_BASE='<完整网关地址，含路径>'
export VBIO_PROJECT_ID='<项目 ID>'
read -rsp 'V-Bio token: ' VBIO_API_TOKEN
export VBIO_API_TOKEN
```

在终端提示时输入项目 token，再从同一个终端启动助手。Token 必须绑定到目标项目。提交新任务还需要 **Submit** 权限。 [连接配置](vbio/references/connection.md)

## 使用草稿

告诉助手你想做什么，并提供序列、结构或其他输入。助手会在当前工作区生成草稿，用表格显示输入、方法、项目、状态和待确认的设置，并说明下一步能改什么或还缺什么。草稿可以离线查看。提交成功后会收到任务 ID。在同一个工作区开启新会话时，可以恢复本地任务记录和下载路径。 [草稿交互](vbio/references/conversation.md) · [任务跟踪](vbio/references/task-follow-up.md)

## 具体案例

先调用 `vbio`，再发送下面的请求。每条请求都会从草稿或能力核对开始。

### 从序列预测泛素结构

> 预测这条泛素序列的结构：`MQIFVKTLTGKTITLEVEPSDTIENVKAKIQDKEGIPPDQQRLIFAGKQLEDGRTLSDYNIQKESTLHLVLRLRGG`。

**草稿 A · 泛素结构预测**

| 项目 | 内容 |
| --- | --- |
| 预测对象 | 人泛素，单链，76 个氨基酸 |
| 预测方法 | 待确认，取决于所选项目支持的方法 |
| V-Bio 项目 | 待连接 |
| 任务状态 | 已准备输入，未提交 |

**下一步**　要运行预测，请先[连接 V-Bio 项目](#安装与连接)。想先选方法，可以问「这个项目有哪些方法适合预测这条序列？」

[RCSB PDB 1UBQ](https://www.rcsb.org/structure/1UBQ)

### 预测已有的首尾环肽

> 请预测环六肽 cyclo-(Gly–L-Pro–Gly)₂ 的结构。A 链用 `GPGGPG`，保留第 6 位甘氨酸到第 1 位甘氨酸的首尾肽键。提交前先检查我的预测项目是否支持这个连接。

草稿应标明 A 链和首尾连接，并说明所选后端能否表示这种拓扑，或还需要核实什么。 [晶体结构论文](https://pubs.acs.org/doi/10.1021/ja00513a057)

### 保留内置 CCD 残基

下载并上传含硒代甲硫氨酸的溶菌酶坐标文件。 [下载 1CX6.cif](https://files.rcsb.org/download/1CX6.cif)

> 预测我上传的 `1CX6.cif` 中 A 链的结构，保留硒代甲硫氨酸残基，使用 CCD 编码 `MSE`。

助手应把结构文件中的残基编号映射到序列位置，并核对所选后端是否接受内置的 `MSE` 定义。

### 用 SMILES 提供自定义残基

> 我有一条线性肽，序列为 `AGKG`。A 链第 3 位实际是 L-鸟氨酸，请在结构预测中保留它。如果 Builder 里没有匹配的内置条目，请使用这个残基定义：`N[C@@H](CCCN)C(=O)O`。

草稿应显示 A 链第 3 位、L-鸟氨酸和所选表示方式。如果没有完全匹配的内置条目，且后端支持上传自定义残基，助手可以为该任务准备匹配的 Custom CCD。如果支持情况尚未核实，助手应说明具体待查项。 [多肽兼容性](vbio/references/peptide-compatibility.md)

### 理解多肽设计项目中的引擎选项

> 我在 Peptide Designer 项目中选了 D 手性和双环拓扑，Design Strategy 和 Predict & Score 的可选项跟着变了。请解释这两个字段各管什么，以及在哪里查看我的项目允许哪些选项。

**Design Strategy** 选择生成候选的引擎，**Predict & Score** 选择评估候选的引擎。两个列表分别按引擎角色和当前化学设置筛选。如果助手无法确认部署中的选项，可以请你提供当前可见的选项名称或隐去凭据的截图。设计设置的审阅和提交在项目的 Peptide Designer 网页完成。 [能力边界](vbio/references/capabilities.md)

## 验证客户端

从仓库根目录运行本地模拟网关测试。

```sh
python3 -m unittest discover -s vbio-api-access/tests
```
