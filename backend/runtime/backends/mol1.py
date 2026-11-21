"""Mol1 后端：环肽头尾酰胺/二硫原生支持，pocket SITE 条件。

原 run_mol1_backend（run_single_prediction.py）整体迁入：
docker 执行、pocket→SITE 约束、ipSAE/表位后处理、归档。
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import yaml

from backend.runtime.backends.base import BackendRunContext, StructureBackend
from backend.runtime.backends.request import PredictionRequest
from backend.runtime.custom_ccd_builder import load_standard_ccd_codes

MOL1_COMMON_DIR = "/data/mol1/common/common"


class Mol1Backend(StructureBackend):
    name = "mol1"

    def execute(self, request: PredictionRequest, ctx: BackendRunContext) -> None:
        self.validate(request, ctx.options.get("custom_ccd_molecules"))
        request = self.post_validate(request)
        yaml_content = request.source_yaml

        from backend.core.config import MOL1_DOCKER_IMAGE, RESULTS_BASE_DIR
        from backend.runtime.backends.support import (
            determine_docker_gpu_arg,
            resolve_backend_results_root,
            resolve_backend_work_root,
        )
        from backend.runtime.custom_ccd_builder import (
            _normalize_custom_ccd_molecules,
            apply_custom_ccd_code_remap,
            resolve_custom_ccd_code_collisions,
        )
        from backend.worker.docker_cmd import ensure_image, image_and_python

        mol1_image = str(MOL1_DOCKER_IMAGE or "").strip()
        ensure_image(mol1_image)
        print("Using Mol-1 backend", file=sys.stderr)

        results_root = resolve_backend_results_root(self.name, ctx.task_id, ctx.temp_dir)
        work_root = str(resolve_backend_work_root(results_root))
        input_dir = os.path.join(work_root, "input")
        output_dir = os.path.join(results_root, "output")
        os.makedirs(input_dir, exist_ok=True)
        os.makedirs(output_dir, exist_ok=True)

        custom_ccd_molecules = _normalize_custom_ccd_molecules(
            ctx.options.get("custom_ccd_molecules"))
        if custom_ccd_molecules:
            custom_ccd_molecules, ccd_remap = resolve_custom_ccd_code_collisions(
                custom_ccd_molecules, load_standard_ccd_codes())
            if ccd_remap:
                yaml_content = apply_custom_ccd_code_remap(yaml_content, ccd_remap)
                print(f"[WARN] 自定义 CCD 编码与标准冲突，已自动重命名: {ccd_remap}",
                      file=sys.stderr)
            import yaml as _yaml_mod
            yaml_data = _yaml_mod.safe_load(yaml_content) or {}
            yaml_data["custom_ccd_molecules"] = custom_ccd_molecules
            yaml_content = _yaml_mod.safe_dump(yaml_data, sort_keys=False, allow_unicode=True)
        yaml_path = os.path.join(input_dir, "input.yaml")
        with open(yaml_path, "w", encoding="utf-8") as handle:
            handle.write(yaml_content)

        use_msa_server = bool(ctx.options.get("use_msa_server", False))
        pocket_sequence_contacts = ctx.options.get("pocket_sequence_contacts")
        seed = int(ctx.seed or 101)

        msa_server_url = self._assert_msa_server_configured() if use_msa_server else ""

        image, _ = image_and_python(mol1_image)
        container_name = f"mol1-predict-{uuid.uuid4().hex[:12]}"
        gpu_arg = determine_docker_gpu_arg(os.environ.get("CUDA_VISIBLE_DEVICES"))
        command = [
            "docker", "run", "--rm", "--name", container_name, "--entrypoint=",
            "--runtime", "nvidia", "--shm-size", "16g",
            "--gpus", gpu_arg,
            "--add-host", "host.docker.internal:host-gateway",
            "--volume", f"{MOL1_COMMON_DIR}:/cache/common:ro",
            "--volume", "/data/mol1/checkpoint:/cache/checkpoint:ro",
            "--volume", "/data/msa_cache:/data/msa_cache:ro",
            "--volume", "/data/V-Bio:/workspace/vbio:ro",
            "--volume", "/data/V-Bio/capabilities/mol1/Mol-1-source:/workspace/Mol-1:ro",
            "--volume", f"{RESULTS_BASE_DIR}:{RESULTS_BASE_DIR}",
            "--volume", f"{yaml_path}:{yaml_path}",
            "-w", "/workspace/vbio/capabilities/mol1",
            "--env", "PYTHONPATH=/workspace/Mol-1",
            "--env", "MOL_ROOT_DIR=/cache",
            "--env", "LAYERNORM_TYPE=torch",
            image, "python3.12", "mol1_predict.py",
            "--yaml", yaml_path,
            "--output-dir", output_dir,
            "--work-dir", work_root,
            "--seed", str(seed),
            "--msa-server-url", msa_server_url,
        ]
        constraints_path = self._write_pocket_site_constraints(
            work_root, yaml_content, pocket_sequence_contacts)
        if not use_msa_server:
            command.append("--no-msa")
        if constraints_path:
            command += ["--constraints-json", str(constraints_path)]
        ipsae_enabled = False
        if str(os.environ.get("MOL1_ATOM_CONFIDENCE", "")).strip().lower() in ("1", "true", "yes"):
            # 输出 token/原子级置信（含 PAE），供 ipSAE 等后处理使用；归档体积显著增大
            command.append("--atom-confidence")
            ipsae_enabled = True
        print(f"Mol-1 docker: {shlex.join(command)}", file=sys.stderr)
        started = time.time()
        completed = subprocess.run(command, check=False)
        print(f"Mol-1 推理完成（{time.time() - started:.0f}s, exit={completed.returncode}）",
              file=sys.stderr)
        if completed.returncode != 0:
            raise RuntimeError(f"Mol-1 inference failed (exit {completed.returncode})")

        if ipsae_enabled:
            self._ipsae_and_epitope_postprocess(output_dir)

        self._write_archive(work_root, output_dir, yaml_content,
                            ctx.output_archive_path)

    def _assert_msa_server_configured(self) -> str:
        from backend.runtime.run_single_prediction import _assert_msa_server_configured
        return _assert_msa_server_configured("mol1")

    def _write_pocket_site_constraints(self, work_root: str, yaml_content: str,
                                       pocket_sequence_contacts) -> Path | None:
        if not pocket_sequence_contacts:
            return None
        data = yaml.safe_load(yaml_content) or {}
        binder_chain = "B"
        for c in data.get("constraints") or []:
            if isinstance(c, dict) and "pocket" in c:
                binder_chain = str((c["pocket"] or {}).get("binder") or binder_chain)
                break
        contacts = [[c, int(n)] for c, n in pocket_sequence_contacts]
        constraints_obj = {"site": {
            "source_chains": sorted({c for c, _n in contacts}),
            "partner_chains": [binder_chain],
            "residue_ids": contacts,
        }}
        constraints_path = Path(work_root) / "mol1_constraints.json"
        constraints_path.write_text(json.dumps(constraints_obj), encoding="utf-8")
        return constraints_path

    def _ipsae_and_epitope_postprocess(self, output_dir: str) -> None:
        # atom-confidence 运行后计算逐样本 ipSAE（与 protenix 同源评估器）。
        # best_ipsae.json / ipsae_*.json 写入输出树进归档——前端 IPSAE 列与
        # 结果视图的数据源；失败仅告警不阻断。
        repo = Path(__file__).resolve().parents[3]
        p2d = str(repo / "capabilities" / "protenix2dock")
        if p2d not in sys.path:
            sys.path.insert(0, p2d)
        try:
            import numpy as np
            from core.ipsae import compute_ipsae_for_output

            ipsae_by_sample = compute_ipsae_for_output(Path(output_dir))
        except Exception as ipsae_err:
            print(f"[WARN] mol1 ipSAE 后处理失败(不影响结果): {ipsae_err}", file=sys.stderr)
            return

        for full_path in sorted(Path(output_dir).rglob("*full_data_sample_*.json")):
            fd = json.loads(full_path.read_text())
            asym = np.asarray(fd.get("token_asym_id") or [], dtype=int)
            pae_m = np.asarray(fd.get("token_pair_pae") or [], dtype=float)
            a2t = np.asarray(fd.get("atom_to_token_idx") or [], dtype=int)
            apl = np.asarray(fd.get("atom_plddt") or [], dtype=float)
            if not (len(asym) and pae_m.shape[0] == len(asym) and len(a2t) == len(apl)):
                continue
            tok_plddt = {}
            for ai, ti in enumerate(a2t):
                tok_plddt.setdefault(int(ti), []).append(float(apl[ai]))
            tok_plddt = {t: float(np.mean(v)) for t, v in tok_plddt.items()}
            groups = {}
            for ti, a in enumerate(asym):
                groups.setdefault(int(a), []).append(ti)
            if len(groups) < 2:
                continue
            ordered = sorted(groups, key=lambda g: min(groups[g]))
            src_g, part_g = ordered[0], ordered[-1]
            pairs = []
            epitope = {int(src_g): set(), int(part_g): set()}
            for ti in groups[src_g]:
                for tj in groups[part_g]:
                    lo_ok = pae_m[ti, tj] < 10.0
                    hi_ok = pae_m[tj, ti] < 10.0
                    pl_ok = tok_plddt.get(ti, 0) >= 70 and tok_plddt.get(tj, 0) >= 70
                    if (lo_ok or hi_ok) and pl_ok:
                        pairs.append([int(ti), int(tj)])
                        epitope[int(src_g)].add(int(ti))
                        epitope[int(part_g)].add(int(tj))
            contact = {
                "source_chain": int(src_g), "partner_chain": int(part_g),
                "high_conf_pairs": pairs,
                "epitope_tokens": {
                    str(chain): sorted(tokens) for chain, tokens in epitope.items()
                },
                "n_pairs": len(pairs),
            }
            out = full_path.parent / full_path.name.replace(
                "_full_data_", "_epitope_contacts_")
            out.write_text(json.dumps(contact, default=str))

        if not ipsae_by_sample:
            return
        best_idx = max(ipsae_by_sample, key=lambda s: ipsae_by_sample[s].get("ipsae_dom") or 0)
        best_payload = None
        for sample_idx, fields in sorted(ipsae_by_sample.items()):
            conf_files = sorted(Path(output_dir).rglob(
                f"*summary_confidence_sample_{sample_idx}.json"))
            payload = dict(fields)
            payload["sample"] = sample_idx
            if conf_files:
                try:
                    payload.update(json.loads(conf_files[0].read_text()))
                except Exception:
                    pass
                stem = conf_files[0].name.split("_summary_confidence_")[0]
                (conf_files[0].parent /
                 f"ipsae_{stem}_sample_{sample_idx}.json").write_text(
                    json.dumps(payload, default=str))
            if best_payload is None and sample_idx == best_idx:
                best_payload = payload
        if best_payload is not None:
            (Path(output_dir) / "best_ipsae.json").write_text(
                json.dumps(best_payload, default=str))
        best_dom = (best_payload or {}).get("ipsae_dom")
        print(f"[mol1] ipSAE: {len(ipsae_by_sample)} 样本, best ipsae_dom={best_dom}",
              file=sys.stderr)

    def _write_archive(self, work_root: str, output_dir: str, yaml_content: str,
                       output_archive_path: str) -> None:
        # 归档：mol1/<name>.json + input.yaml + output/ 树 + README
        input_json_path = os.path.join(work_root, "mol1_input.json")
        input_name = "mol1_input"
        if os.path.exists(input_json_path):
            try:
                jobs = json.loads(Path(input_json_path).read_text())
                if jobs:
                    input_name = str(jobs[0].get("name") or input_name)
            except Exception:
                pass
        try:
            with zipfile.ZipFile(output_archive_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                if os.path.exists(input_json_path):
                    zipf.write(input_json_path, f"mol1/{input_name}.json")
                zipf.writestr("mol1/input.yaml", yaml_content)
                for root, _, files in os.walk(output_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        arcname = os.path.join(
                            "mol1/output",
                            os.path.relpath(file_path, output_dir))
                        zipf.write(file_path, arcname)
                zipf.writestr("mol1/README.txt", (
                    "Mol-1 input assets generated by V-Bio.\n"
                    "Files included:\n"
                    f" - {input_name}.json: Mol-1 inference JSON\n"
                    " - input.yaml: original request payload\n"
                    " - output/: cif + confidence + summary.json (if succeeded)\n"))
            print(f"Mol-1 归档创建完成: {output_archive_path}", file=sys.stderr)
        except Exception as exc:
            raise RuntimeError(f"Failed to create Mol-1 archive: {exc}") from exc
