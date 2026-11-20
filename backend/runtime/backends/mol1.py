"""Mol1 后端：pocket SITE 条件 + bond 约束（环肽共折叠验证）。"""
from __future__ import annotations

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class Mol1Backend(StructureBackend):
    name = "mol1"

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request)
        from backend.runtime.run_single_prediction import run_mol1_backend
        run_mol1_backend(
            temp_dir=ctx.temp_dir,
            yaml_content=ctx.yaml_content,
            output_archive_path=ctx.output_archive_path,
            use_msa_server=bool(ctx.options.get("use_msa_server", False)),
            pocket_sequence_contacts=ctx.options.get("pocket_sequence_contacts"),
            seed=ctx.seed,
            task_id=ctx.task_id,
        )
