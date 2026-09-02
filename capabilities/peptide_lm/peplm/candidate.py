"""Candidate object shared by the loop, oracle and reward."""

from __future__ import annotations

from dataclasses import dataclass, field

from peplm.vocab import residue_tokens, sequence_key


@dataclass
class Candidate:
    tokens: list[str]                      # full token list incl. <lin>/<cyc> and tags
    cyclic: bool = False
    origin: str = "agent"                  # agent | edit | mut | seed
    parent: str | None = None              # parent sequence key (GRPO grouping)
    cond_prompt: list[str] | None = None   # conditioned-trajectory prompt tokens
    props: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)   # oracle results
    reward: float | None = None
    final_reward: float | None = None

    @property
    def key(self) -> str:
        return sequence_key(self.tokens, self.cyclic)

    @property
    def residues(self) -> list[str]:
        return residue_tokens(self.tokens)

    @property
    def seq_str(self) -> str:
        return "".join(self.residues)
