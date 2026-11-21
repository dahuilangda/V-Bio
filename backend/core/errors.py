"""领域错误基类：路由层可统一捕获映射（400/500），
具体引擎/工作流异常按需继承，不再各自为政。"""
from __future__ import annotations


class DomainError(ValueError):
    """平台可预期的领域错误基类。"""


class UserInputError(DomainError):
    """调用方输入问题——对应 400（DomainError 已是 ValueError 子类）。"""


class TransientError(DomainError, RuntimeError):
    """暂时性故障（外部进程/网络/GPU 占用）——对应 503 语义。"""


class DpeptideStaleWorkerError(TransientError):
    """A refine dispatch flag was silently dropped by the executing worker.

    The worker task contract echoes the flags it honored into the dpeptide
    contract; a missing/mismatched echo means the celery process predates the
    flag (long-running worker, code updated on disk after its start) — the
    dropped ``blind_peptide`` flag silently downgrades to a clashing
    staged-local refine and every candidate gets chirality-rejected. Raised
    task-level (not per-candidate) so the run fails fast with the actionable
    fix: restart the GPU worker container.
    """
