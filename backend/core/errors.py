"""领域错误基类：路由层可统一捕获映射（400/500），
具体引擎/工作流异常按需继承，不再各自为政。"""
from __future__ import annotations


class DomainError(ValueError):
    """平台可预期的领域错误基类。"""


class UserInputError(DomainError, ValueError):
    """调用方输入问题——对应 400。"""


class TransientError(DomainError, RuntimeError):
    """暂时性故障（外部进程/网络/GPU 占用）——对应 503 语义。"""
