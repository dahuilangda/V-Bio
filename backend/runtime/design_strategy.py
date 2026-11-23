from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional


@dataclass
class Candidate:
    sequence: str
    modifications: list = field(default_factory=list)
    cys_positions: list = field(default_factory=list)
    group: str = "ungrouped"


class DesignStrategy(ABC):
    name: str

    @abstractmethod
    def propose(self, elites: list, count: int) -> Iterable[Candidate]:
        raise NotImplementedError

    @abstractmethod
    def prior_score(self, sequence: str) -> Optional[float]:
        raise NotImplementedError

    @abstractmethod
    def learn(self, elites: list, rows: list) -> None:
        raise NotImplementedError


class Esm3Strategy(DesignStrategy):
    name = "esm3"

    def __init__(self, proposer, natural_pool: list, unnatural_pool: list) -> None:
        self._proposer = proposer
        self._natural_pool = natural_pool
        self._unnatural_pool = unnatural_pool

    def propose(self, elites: list, count: int) -> Iterable[Candidate]:
        for sequence, modifications, anchors, group in self._proposer.propose(
                self._natural_pool, self._unnatural_pool, elites, count):
            yield Candidate(
                sequence=str(sequence or ""),
                modifications=[m for m in (modifications or []) if isinstance(m, dict)],
                cys_positions=[int(p) for p in (anchors or [])],
                group=group,
            )

    def prior_score(self, sequence: str) -> Optional[float]:
        return self._proposer.pseudo_perplexity(sequence)

    def learn(self, elites: list, rows: list) -> None:
        self._proposer.learn(elites, rows)


def keep_best_by_prior(score: Callable[[str], Optional[float]], rows: list, keep: int) -> list:
    scored = sorted(
        ((score(str(row.get("sequence") or "")), row) for row in rows),
        key=lambda pair: pair[0] if pair[0] is not None else float("inf"))
    return [row for _value, row in scored[:keep]]
