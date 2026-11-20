"""Boltz-2 后端：平台 yaml 原生透传（bond/contact/pocket）。"""
from __future__ import annotations

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest


class BoltzBackend(StructureBackend):
    name = "boltz"

    def post_validate(self, request: PredictionRequest) -> PredictionRequest:
        from backend.runtime.run_single_prediction import (
            _normalize_ligand_chain_collisions,
            _validate_unique_sequence_chain_ids,
            _remap_constraints_by_template_alignment,
            _sanitize_constraints_for_chain_lengths,
            _validate_amidated_terminal_constraints,
        )
        yaml_text = request.source_yaml
        yaml_text = _normalize_ligand_chain_collisions(yaml_text)
        _validate_unique_sequence_chain_ids(yaml_text)
        yaml_text = _remap_constraints_by_template_alignment(yaml_text)
        yaml_text = _sanitize_constraints_for_chain_lengths(yaml_text)
        _validate_amidated_terminal_constraints(yaml_text, request.raw.get("custom_ccd_molecules") or [])
        return PredictionRequest.from_yaml(yaml_text)

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request)
        request = self.post_validate(request)
        ctx.yaml_content = request.source_yaml
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
