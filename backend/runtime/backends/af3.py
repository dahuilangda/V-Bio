"""AlphaFold3 后端：bond 约束经适配器映射为 bondedAtomPairs。"""
from __future__ import annotations

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class Af3Backend(StructureBackend):
    name = "alphafold3"
    supported_constraints = frozenset({"bond"})

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request)
        from backend.runtime.run_single_prediction import run_alphafold3_backend
        run_alphafold3_backend(
            ctx.temp_dir,
            ctx.yaml_content,
            ctx.output_archive_path,
            seed=ctx.seed,
            template_payloads=ctx.options.get("template_payloads")
            if isinstance(ctx.options.get("template_payloads"), list) else None,
            task_id=ctx.task_id,
            custom_ccd_molecules=ctx.options.get("custom_ccd_molecules")
            if isinstance(ctx.options.get("custom_ccd_molecules"), list) else [],
            low_vram=bool(ctx.options.get("low_vram", False)),
        )
