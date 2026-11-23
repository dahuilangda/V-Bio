from __future__ import annotations

import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ClassVar, Iterable, Optional

from backend.runtime import candidate_pool, rfd4_strategy
from backend.scheduling.capability_router import build_capability_queue


@dataclass
class Candidate:
    sequence: str
    modifications: list = field(default_factory=list)
    cys_positions: list = field(default_factory=list)
    group: str = "ungrouped"


class DesignStrategy(ABC):
    name: ClassVar[str]
    generations: ClassVar[Optional[int]] = None
    uses_sequence_mask: ClassVar[bool] = True
    oversample: ClassVar[int] = 1

    @abstractmethod
    def propose(self, generation: int, elites: list, count: int) -> Iterable[Candidate]:
        raise NotImplementedError

    @abstractmethod
    def prior_score(self, sequence: str) -> Optional[float]:
        raise NotImplementedError

    @abstractmethod
    def learn(self, elites: list, rows: list) -> None:
        raise NotImplementedError

    def artifacts(self) -> dict[str, bytes]:
        return {}


class Esm3Strategy(DesignStrategy):
    name = "esm3"
    oversample = 2

    def __init__(self, proposer, natural_pool: list, unnatural_pool: list) -> None:
        self._proposer = proposer
        self._natural_pool = natural_pool
        self._unnatural_pool = unnatural_pool

    def propose(self, generation: int, elites: list, count: int) -> Iterable[Candidate]:
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


class Rfd4Strategy(DesignStrategy):
    name = "rfd4"
    generations = 1
    uses_sequence_mask = False

    def __init__(
        self,
        *,
        temp_dir: str,
        yaml_content: str,
        design_mode: str,
        options: dict,
        target_sequence: str,
        pocket_author_contacts,
        template_inputs,
        spec: Optional[str],
        parent_task_id: str,
        queue: str,
    ) -> None:
        self._temp_dir = temp_dir
        self._yaml_content = yaml_content
        self._design_mode = design_mode
        self._options = options
        self._target_sequence = target_sequence
        self._pocket_author_contacts = pocket_author_contacts
        self._template_inputs = template_inputs
        self._spec = spec
        self._parent_task_id = parent_task_id
        self._queue = queue
        self._generated: dict[str, bytes] = {}

    def propose(self, generation: int, _elites: list, count: int) -> Iterable[Candidate]:
        jobs = rfd4_strategy.generation_jobs(
            temp_dir=self._temp_dir,
            design_mode=self._design_mode,
            options=self._options,
            target_sequence=self._target_sequence,
            pocket_author_contacts=self._pocket_author_contacts,
            template_inputs=self._template_inputs,
            spec=self._spec,
            generation=generation,
            num_designs=count,
        )
        self._completed: list[dict] = []

        def submit(job: dict):
            from backend.core.celery_app import celery_app

            payload = {
                "generation": generation,
                "candidate_index": 0,
                "sequence": "",
                "candidate_yaml": self._yaml_content,
                "candidate_dir": str(job["dir"]),
                "archive_path": str(job["archive"]),
                "predict_args": {
                    "rfd4_spec": job["spec"],
                    "rfd4_num_designs": job["num_designs"],
                    "template_inputs": job["template_inputs"],
                },
                "model_name": None,
                "backend": "rfd4",
                "worker_args_path": str(job["dir"] / "worker_args.json"),
                "parent_task_id": self._parent_task_id,
            }
            return celery_app.send_task(
                "tasks.peptide_candidate_worker_task", args=[payload], queue=self._queue)

        def collect(job: dict, worker_result, state: str) -> None:
            if state == "SUCCESS" and isinstance(worker_result, dict):
                job["archive"] = worker_result.get("archive_path") or job["archive"]
                self._completed.append(job)

        candidate_pool.drain(jobs, submit, self._parent_task_id, collect, candidate_pool.SCORE_CANDIDATES)
        return self._read_completed()

    def _read_completed(self) -> list[Candidate]:
        candidates: list[Candidate] = []
        for job in self._completed:
            archive = Path(job["archive"])
            with zipfile.ZipFile(archive) as generated:
                for name in generated.namelist():
                    if name.endswith(".cif"):
                        self._generated[f"rfd4_generated/{job['dir'].name}/{Path(name).name}"] = (
                            generated.read(name))
            for row in rfd4_strategy.read_generation_archive(
                    archive, bicyclic=self._design_mode == "bicyclic"):
                candidates.append(Candidate(
                    sequence=row["sequence"],
                    modifications=list(row["modifications"]),
                    cys_positions=list(row["cys_positions"]),
                    group="rfd4",
                ))
        return candidates

    def prior_score(self, sequence: str) -> Optional[float]:
        return None

    def learn(self, elites: list, rows: list) -> None:
        return None

    def artifacts(self) -> dict[str, bytes]:
        return dict(self._generated)


STRATEGIES: dict[str, type[DesignStrategy]] = {"esm3": Esm3Strategy, "rfd4": Rfd4Strategy}


def strategy_class(name: str) -> type[DesignStrategy]:
    strategy = STRATEGIES.get(name)
    if strategy is None:
        raise ValueError(f"不支持的设计策略: {name}")
    return strategy


def rfd4_generation_queue(priority: str = "default") -> str:
    return build_capability_queue("rfd4", priority)


def keep_best_by_prior(score: Callable[[str], Optional[float]], rows: list, keep: int) -> list:
    scored = sorted(
        ((score(str(row.get("sequence") or "")), row) for row in rows),
        key=lambda pair: pair[0] if pair[0] is not None else float("inf"))
    return [row for _value, row in scored[:keep]]
