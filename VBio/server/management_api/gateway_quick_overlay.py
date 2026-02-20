from __future__ import annotations

import os
import time
from typing import Any, List, Optional, Tuple

from flask import Response, jsonify, request

from management_api.lead_opt_overlay import OverlayBusyError


def handle_lead_optimization_pocket_overlay(gateway: Any) -> Tuple[Response, int]:
    started = time.perf_counter()
    token = None
    project_id: Optional[str] = None

    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}
        raw_project_id = str(payload.get("project_id") or request.args.get("project_id") or "").strip()
        project_id = raw_project_id or None
        token_plain = (request.headers.get("X-API-Token") or "").strip()
        runtime_token = str(getattr(getattr(gateway, "runtime_proxy", None), "runtime_api_token", "") or "").strip()
        backend_token = str(os.environ.get("BOLTZ_API_TOKEN", "") or "").strip()
        runtime_env_token = str(os.environ.get("VBIO_RUNTIME_API_TOKEN", "") or "").strip()
        trusted_tokens = {token for token in (runtime_token, backend_token, runtime_env_token) if token}
        using_runtime_token = bool(token_plain and token_plain in trusted_tokens)
        if using_runtime_token:
            # Allow trusted runtime token for local UI utility routes (no project DB auth dependency).
            effective_project_id = project_id
            token = None
        else:
            token = gateway._authorize_quick_project_action(token_plain, project_id, require_submit=True)
            effective_project_id = project_id or token.project_id

        complex_structure_text = str(payload.get("complex_structure_text") or "").strip()
        complex_structure_format = str(payload.get("complex_structure_format") or "cif").strip().lower()
        ligand_chain_id_text = str(payload.get("ligand_chain_id") or "").strip()
        residues_raw = payload.get("residues")

        if not complex_structure_text:
            return jsonify({"error": "'complex_structure_text' is required."}), 400
        if complex_structure_format not in {"pdb", "cif", "mmcif"}:
            return jsonify({"error": "'complex_structure_format' must be 'pdb' or 'cif'."}), 400
        if not ligand_chain_id_text:
            return jsonify({"error": "'ligand_chain_id' is required."}), 400
        if not isinstance(residues_raw, list) or not residues_raw:
            return jsonify({"error": "'residues' must be a non-empty array."}), 400

        ligand_chain_ids = [part.strip() for part in ligand_chain_id_text.split(",") if part.strip()]
        residue_pairs: List[tuple[str, int]] = []
        seen_pairs: set[tuple[str, int]] = set()
        for row in residues_raw:
            item = row if isinstance(row, dict) else {}
            chain_id = str(item.get("chain_id") or item.get("chainId") or "").strip()
            residue_number_raw = item.get("residue_number") or item.get("residue")
            try:
                residue_number = int(residue_number_raw)
            except Exception:
                residue_number = 0
            if not chain_id or residue_number <= 0:
                continue
            key = (chain_id, residue_number)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            residue_pairs.append(key)

        if not residue_pairs:
            return jsonify({"error": "No valid residues were provided for pocket overlay."}), 400

        overlay_text, overlay_format = gateway.lead_opt_overlay_service.build_overlay(
            complex_structure_text=complex_structure_text,
            complex_structure_format=complex_structure_format,
            ligand_chain_ids=ligand_chain_ids,
            residue_pairs=residue_pairs,
        )

        status_code = 200
        response = jsonify(
            {
                "overlay_structure_text": overlay_text,
                "overlay_structure_format": overlay_format,
                "residue_count": len(residue_pairs),
            }
        )

        gateway._record_usage(
            token,
            action="lead_opt_pocket_overlay",
            status_code=status_code,
            succeeded=True,
            started_at=started,
            project_id=effective_project_id,
            task_id=None,
        )
        return response, status_code
    except PermissionError as exc:
        return gateway._forbidden(str(exc), token, "lead_opt_pocket_overlay", started, project_id)
    except OverlayBusyError as exc:
        gateway._record_usage(
            token,
            action="lead_opt_pocket_overlay",
            status_code=429,
            succeeded=False,
            started_at=started,
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 429
    except TimeoutError as exc:
        gateway._record_usage(
            token,
            action="lead_opt_pocket_overlay",
            status_code=504,
            succeeded=False,
            started_at=started,
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 504
    except Exception as exc:  # noqa: BLE001
        gateway.logger.exception("Lead optimization pocket overlay failed")
        gateway._record_usage(
            token,
            action="lead_opt_pocket_overlay",
            status_code=500,
            succeeded=False,
            started_at=started,
            project_id=project_id or (token.project_id if token else None),
            task_id=None,
        )
        return jsonify({"error": str(exc)}), 500
