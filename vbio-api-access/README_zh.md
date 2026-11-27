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

## 查看工作区统计

随包客户端把当前工作区记录的所有任务汇总成一份终端报告：状态分布、工作流与引擎成功率、成功任务耗时、提交活跃度和高频失败原因。在工作区运行 `stats`；加 `--refresh` 先刷新未完成的任务。 [任务跟踪](vbio/references/task-follow-up.md)

## 验证客户端

从仓库根目录运行本地模拟网关测试。

```sh
python3 -m unittest discover -s vbio-api-access/tests
```
