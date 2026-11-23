from __future__ import annotations


class DomainError(ValueError):
    pass


class UserInputError(DomainError):
    pass


class TransientError(DomainError, RuntimeError):
    pass


class DpeptideStaleWorkerError(TransientError):
    pass
