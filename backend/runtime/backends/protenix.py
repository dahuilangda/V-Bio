"""Protenix 后端：bond/pocket 原生支持，由适配器映射。"""
from __future__ import annotations

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class ProtenixBackend(StructureBackend):
    name = "protenix"

    def post_validate(self, request: PredictionRequest) -> PredictionRequest:
        from backend.runtime.run_single_prediction import (
            _remap_constraints_by_template_alignment,
            _sanitize_constraints_for_chain_lengths,
            _validate_amidated_terminal_constraints,
        )
        yaml_text = request.source_yaml
        yaml_text = _remap_constraints_by_template_alignment(yaml_text)
        yaml_text = _sanitize_constraints_for_chain_lengths(yaml_text)
        _validate_amidated_terminal_constraints(yaml_text, request.raw.get("custom_ccd_molecules") or [])
        return PredictionRequest.from_yaml(yaml_text)

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request)
        request = self.post_validate(request)
        ctx.yaml_content = request.source_yaml
        from backend.runtime.run_single_prediction import run_protenix_backend
        run_protenix_backend(
            temp_dir=ctx.temp_dir,
            yaml_content=ctx.yaml_content,
            output_archive_path=ctx.output_archive_path,
            seed=ctx.seed,
            msa_mode=str(ctx.options.get("msa_mode") or "auto"),
            task_id=ctx.task_id,
            custom_ccd_molecules=ctx.options.get("custom_ccd_molecules")
            if isinstance(ctx.options.get("custom_ccd_molecules"), list) else [],
            low_vram=bool(ctx.options.get("low_vram", False)),
            ipsae_ligand_chain_id=ctx.options.get("ipsae_ligand_chain_id"),
            template_payloads=ctx.options.get("template_payloads")
            if isinstance(ctx.options.get("template_payloads"), list) else None,
        )
