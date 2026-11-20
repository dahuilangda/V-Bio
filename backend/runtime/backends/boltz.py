"""Boltz-2 后端：平台 yaml 原生透传（bond/contact/pocket）。"""
from __future__ import annotations

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class BoltzBackend(StructureBackend):
    name = "boltz"

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request)
        from backend.runtime.run_single_prediction import run_boltz_backend
        run_boltz_backend(
            ctx.temp_dir,
            ctx.yaml_content,
            ctx.output_archive_path,
            ctx.options.get("predict_args") or {},
            ctx.options.get("model_name"),
            task_id=ctx.task_id,
            strict_ligand_confidence_contract=bool(
                ctx.options.get("strict_ligand_confidence_contract", False)),
            custom_ccd_molecules=ctx.options.get("custom_ccd_molecules")
            if isinstance(ctx.options.get("custom_ccd_molecules"), list) else [],
            low_vram=bool(ctx.options.get("low_vram", False)),
            ipsae_ligand_chain_id=ctx.options.get("ipsae_ligand_chain_id"),
        )
