"""protenix2dock celery task: six-mode protein-ligand workflow on the
Protenix engine, submitted through the V-Bio task system.

Routes: `/api/boltz2score` with `backend=protenix` (frontend gateway
forwards the multipart form verbatim). The capability is registered as
`protenix2dock` but consumes the existing `cap.protenix.*` queues so the
protenix GPU worker picks it up after a restart (new task function).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import zipfile
from typing import Any

from celery.exceptions import Ignore

from backend.core import config
from backend.core.celery_app import celery_app
from gpu_manager import get_gpu_total_memory_mib, get_redis_client, release_gpu

import logging

logger = logging.getLogger(__name__)

P2D_SCRIPT = "/workspace/vbio/capabilities/protenix2dock/protenix2dock.py"

VALID_P2D_MODES = {"score", "pose", "refine", "interface", "dock", "peptide"}


def _coerce_opt_bool(value: Any) -> bool | None:
    """Tri-state bool: True/False from common encodings, None if absent/unparseable.

    Form fields arrive as strings, so "false" must not fall through as truthy.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    token = str(value).strip().lower()
    if token in {"1", "true", "yes", "on"}:
        return True
    if token in {"0", "false", "no", "off"}:
        return False
    return None


def _resolve_low_vram(score_args: dict, gpu_id: int) -> bool:
    """low_vram: ONLY an explicit user request enables it (default off).

    The engine's default mode (fp32 trunk, cuequivariance kernels, all
    diffusion samples batched) is markedly faster but needs roughly double the
    memory of low_vram — the measured OOM driving the old auto path was a
    ~2150-token complex on a 24 GB card. Policy (2026-09-04): no silent
    performance/precision degradation; an OOM on an undersized card surfaces
    loudly instead. Pass low_vram=true explicitly to opt in.
    """
    requested = _coerce_opt_bool(score_args.get("low_vram"))
    return bool(requested)


def _build_protenix2dock_command(
    *,
    task_id: str,
    gpu_id: int,
    task_temp_dir: str,
    entry_args: list[str],
) -> tuple[list[str], str]:
    """Docker command for the protenix runtime (protenix2dock capability)."""
    from backend.worker import docker_cmd
    from backend.worker import tasks as _tasks

    command, container_name = docker_cmd.build_task_docker_skeleton(
        task_id=task_id, gpu_id=gpu_id, runtime_label="protenix2dock",
    )
    command.extend(["--volume", f"{task_temp_dir}:{task_temp_dir}"])
    docker_cmd.protenix_runtime_mounts(command)

    # The affinity checkpoint lives on the HOST; this worker may not share its
    # mount namespace, so mount unconditionally — a stale path surfaces as a
    # load error inside the task container.
    affinity_ckpt = str(getattr(config, "PROTENIX2DOCK_AFFINITY_CKPT", "") or "").strip()
    if affinity_ckpt:
        command.extend(["--volume", f"{affinity_ckpt}:{affinity_ckpt}:ro"])
        command.extend(["--env", f"PROTENIX_AFFINITY_CKPT={affinity_ckpt}"])

    command.extend(_tasks._sanitize_docker_extra_args(shlex.split(
        str(getattr(config, "PROTENIX_DOCKER_EXTRA_ARGS", "") or "")
    )))
    image, python_bin = docker_cmd.image_and_python()
    command.append(image)
    command.append(python_bin)
    command.append(P2D_SCRIPT)
    command.extend(entry_args)
    return command, container_name


def _pocket_center_and_size(score_args, protein_file, task_temp_dir):
    """Resolve pocket_residues / pocket_ligand to a (center, size) box.

    pocket_residues: centroid of the named residues' atoms on the protein
    structure, box = their extent + 4 A. pocket_ligand: centroid and extent
    of the reference ligand's heavy atoms (SDF via RDKit, PDB via gemmi).
    Returns None when neither definition is present.
    """
    import numpy as np

    residues = str(score_args.get("pocket_residues") or "").strip()
    ligand_content = str(score_args.get("pocket_ligand_content") or "").strip()
    if not (residues or ligand_content):
        return None

    pts = []
    if residues:
        if not protein_file:
            return None
        import gemmi

        wanted = set()
        for token in residues.split(","):
            chain_part, _, num_part = token.strip().partition(":")
            if chain_part and num_part.isdigit():
                wanted.add((chain_part.strip(), int(num_part)))
        st = gemmi.read_structure(protein_file)
        st.setup_entities()
        for chain in st[0]:
            for res in chain:
                if (chain.name, int(res.seqid.num)) in wanted:
                    for atom in res:
                        if atom.element != gemmi.Element("H"):
                            pts.append([atom.pos.x, atom.pos.y, atom.pos.z])
    else:
        from werkzeug.utils import secure_filename as _sf

        ligand_path = os.path.join(
            task_temp_dir,
            _sf(str(score_args.get("pocket_ligand_filename") or "pocket_ligand.sdf")))
        with open(ligand_path, "w", encoding="utf-8") as fh:
            fh.write(ligand_content)
        if ligand_path.endswith((".sdf", ".mol")):
            from rdkit import Chem

            mol = next(iter(Chem.SDMolSupplier(ligand_path, removeHs=True)), None)
            if mol is None:
                raise ValueError("pocket_ligand SDF could not be parsed.")
            conf = mol.GetConformer()
            pts = [[conf.GetAtomPosition(i).x, conf.GetAtomPosition(i).y,
                    conf.GetAtomPosition(i).z] for i in range(mol.GetNumAtoms())]
        else:
            import gemmi

            st = gemmi.read_structure(ligand_path)
            st.setup_entities()
            pts = [[a.pos.x, a.pos.y, a.pos.z] for ch in st[0]
                   for r in ch for a in r if a.element != gemmi.Element("H")]
    if not pts:
        return None
    arr = np.array(pts, dtype=float)
    center = arr.mean(axis=0)
    size = arr.max(axis=0) - arr.min(axis=0) + 8.0
    return [float(v) for v in center], [float(v) for v in size]


@celery_app.task(bind=True, name="backend.worker.tasks.protenix2dock_task")

def protenix2dock_task(self, score_args: dict):
    from backend.worker import tasks as _tasks

    task_id = self.request.id
    redis_client = get_redis_client()
    tracker = _tasks.TaskProgressTracker(task_id, redis_client)
    gpu_id = -1

    try:
        tracker.update_status("waiting_gpu", "Waiting for GPU allocation")
        gpu_id = _tasks._acquire_gpu_with_non_peptide_wait_registration(task_id=task_id, timeout=3600)
        reported_gpu = gpu_id
        tracker.update_status("running", f"Acquired GPU {gpu_id}. Starting protenix2dock.")
        _tasks._raise_if_task_cancelled(self, redis_client, task_id)

        task_temp_dir = f"/data/boltz_central_results/_runtime_tmp/p2d_task_{task_id}"
        os.makedirs(task_temp_dir, exist_ok=True)
        output_dir = os.path.join(task_temp_dir, "output")
        work_dir = os.path.join(task_temp_dir, "work")
        os.makedirs(output_dir, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)

        requested_mode = str(score_args.get("mode") or "dock").strip().lower()
        if requested_mode not in VALID_P2D_MODES:
            raise ValueError(f"Unsupported protenix2dock mode {requested_mode!r}")

        msa_server_url = str(getattr(config, "MSA_SERVER_URL", "") or "").strip()

        seed_raw = score_args.get("seed")
        seed = 42 if seed_raw is None else max(0, int(seed_raw))
        entry: list[str] = [
            "--mode", requested_mode,
            "--output_dir", output_dir,
            "--work_dir", work_dir,
            "--msa_cache_dir", "/data/msa_cache",
            "--seed", str(seed),
        ]
        if msa_server_url:
            entry.extend(["--msa_server_url", msa_server_url])
        if _resolve_low_vram(score_args, gpu_id):
            entry.append("--low_vram")

        # Persist uploaded inputs (route forwards file CONTENTS, matching the
        # boltz2score task contract).
        from werkzeug.utils import secure_filename as _sf

        protein_file = None
        protein_content = score_args.get("protein_file_content")
        if protein_content:
            protein_file = os.path.join(task_temp_dir, _sf(score_args.get("protein_filename") or "protein.pdb"))
            with open(protein_file, "w", encoding="utf-8") as fh:
                fh.write(protein_content)
            entry.extend(["--protein_file", protein_file])
        ligand_file = None
        ligand_content = score_args.get("ligand_file_content")
        if ligand_content:
            ligand_file = os.path.join(task_temp_dir, _sf(score_args.get("ligand_filename") or "ligand.sdf"))
            with open(ligand_file, "w", encoding="utf-8") as fh:
                fh.write(ligand_content)
            entry.extend(["--ligand_file", ligand_file])
        if requested_mode == "dock":
            ligand_smiles = str(score_args.get("ligand_smiles") or "").strip()
            if not ligand_smiles:
                raise ValueError("protenix2dock dock mode requires ligand_smiles.")
            entry.extend(["--ligand_smiles", ligand_smiles])
            if score_args.get("center_x") is None:
                # pocket_residues / pocket_ligand define the box implicitly:
                # resolve them to a center here instead of dying at the CLI
                resolved = _pocket_center_and_size(score_args, protein_file, task_temp_dir)
                if resolved is None:
                    raise ValueError(
                        "dock mode requires a pocket definition (center "
                        "coordinates, pocket_residues, or a pocket_ligand file).")
                for axis, value in zip("xyz", resolved[0]):
                    score_args[f"center_{axis}"] = value
                if score_args.get("size_x") is None and resolved[1] is not None:
                    for axis, value in zip("xyz", resolved[1]):
                        score_args[f"size_{axis}"] = value
            for axis in ("x", "y", "z"):
                ckey, skey = f"center_{axis}", f"size_{axis}"
                if score_args.get(ckey) is not None:
                    entry.extend([f"--{ckey}", str(float(score_args[ckey]))])
                if score_args.get(skey) is not None:
                    entry.extend([f"--{skey}", str(float(score_args[skey]))])
        elif requested_mode not in ("score", "peptide"):
            if not ligand_file:
                raise ValueError(f"protenix2dock {requested_mode} mode requires ligand_file.")

        input_content = score_args.get("input_file_content")
        if input_content and not protein_content:
            combined_suffix = ".pdb" if requested_mode == "peptide" else ".cif"
            combined = os.path.join(task_temp_dir, _sf(
                score_args.get("input_filename") or f"input{combined_suffix}"))
            with open(combined, "w", encoding="utf-8") as fh:
                fh.write(input_content)
            entry.extend(["--input", combined])

        if requested_mode == "peptide":
            # Receptor-fixed peptide inpainting from a staged complex.
            peptide_chain = str(score_args.get("peptide_chain") or "B").strip()
            entry.extend(["--peptide_chain", peptide_chain])
            linker_chain = str(score_args.get("linker_chain") or "").strip()
            linker_ccd = str(score_args.get("linker_ccd") or "SEZ").strip().upper()
            bond_pairs = str(score_args.get("bond_pairs") or "").strip()
            if linker_chain or bond_pairs:
                if not linker_chain:
                    raise ValueError("peptide mode linker requires linker_chain.")
                entry.extend(["--linker_chain", linker_chain, "--linker_ccd", linker_ccd])
            if bond_pairs:
                entry.extend(["--bond_pairs", bond_pairs])
            entry.extend([
                "--bond_upper", str(float(score_args.get("bond_upper") or 2.2)),
                "--pocket_cutoff", str(float(score_args.get("pocket_cutoff") or 9.0)),
                "--pocket_upper", str(float(score_args.get("pocket_upper") or 8.0)),
            ])
            if score_args.get("peptide_sequence"):
                entry.extend(["--peptide_sequence", str(score_args["peptide_sequence"])])
            if score_args.get("score_only"):
                entry.append("--score_only")
            if score_args.get("blind_peptide"):
                # blind inpainting route: peptide denoises from pure noise
                # (receptor stays pinned) with the full noise schedule
                entry.append("--blind_peptide")

        target_chain = str(score_args.get("target_chain") or "").strip()
        if target_chain:
            entry.extend(["--target_chain", target_chain])

        for opt in ("sampling_steps", "diffusion_samples"):
            if score_args.get(opt) is not None:
                entry.extend([f"--{opt}", str(int(score_args[opt]))])

        interface_chains = str(score_args.get("interface_chains") or "").strip()
        if interface_chains:
            entry.extend(["--interface_chains", interface_chains])

        command, container_name = _build_protenix2dock_command(
            task_id=task_id, gpu_id=gpu_id, task_temp_dir=task_temp_dir, entry_args=entry,
        )
        _tasks._terminate_task_container(container_name)
        logger.info("Task %s: protenix2dock docker: %s", task_id,
                    " ".join(shlex.quote(p) for p in command))
        _tasks._raise_if_task_cancelled(self, redis_client, task_id)

        tracker.update_status("running", f"Running protenix2dock ({requested_mode})")
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True, bufsize=1,
        )
        tracker.register_process(process.pid)

        # Stream stdout for heartbeats; the CLI logs pipeline stage lines
        # ("INFO protenix2dock: <message>") plus periodic progress markers.
        tail_lines: list[str] = []
        import re as _re
        import time as _time

        _STAGE_RE = _re.compile(r"INFO protenix2dock: (.+)")
        hard_deadline = _time.time() + _tasks.SUBPROCESS_TIMEOUT
        stdout = ""
        assert process.stdout is not None
        for line in process.stdout:
            line = line.rstrip()
            tail_lines.append(line)
            if len(tail_lines) > 200:
                tail_lines.pop(0)
            m = _STAGE_RE.search(line)
            if m:
                tracker.update_status("running", m.group(1)[:200])
            _tasks._raise_if_task_cancelled(self, redis_client, task_id)
            if _time.time() > hard_deadline:
                process.kill()
                _tasks._terminate_task_container(container_name)
                raise TimeoutError(
                    f"protenix2dock task {task_id} exceeded {_tasks.SUBPROCESS_TIMEOUT}s."
                )
        process.wait()
        stdout = "\n".join(tail_lines)
        stderr = stdout

        if process.returncode != 0:
            raise RuntimeError(
                _tasks._format_subprocess_failure(
                    "protenix2dock task", task_id, process.returncode, stderr, stdout
                )
            )

        tracker.update_status("processing_output", "Packaging protenix2dock results")

        # dpeptide design-loop contract: mirror the boltz2 dpeptide task layout
        # ({contract_dir}/out/confidence.json + per-model json/cif pairs with
        # an iptm key) so _dpeptide_refine_and_validate polls and selects the
        # best sample without knowing which engine produced it.
        if score_args.get("dpeptide_contract"):
            import shutil as _shutil

            # The design loop polls {task_tmp}/out/confidence.json where
            # task_tmp = .../dpeptide_task_<celery id>; build the contract in
            # that exact location.
            contract_root = os.path.join(
                "/data/boltz_central_results/_runtime_tmp",
                f"dpeptide_task_{task_id}",
            )
            os.makedirs(contract_root, exist_ok=True)
            out_root = os.path.join(contract_root, "out")
            tag = str(score_args.get("staged_filename") or "d_space_staged").strip() or "staged"
            structure_dir = os.path.join(out_root, "fixed", tag)
            os.makedirs(structure_dir, exist_ok=True)
            scored = []
            scored_meta = []
            # engine layout: <output>/<sample_name>/seed_<seed>/predictions/
            #   <sample_name>_sample_<i>.cif + <..>_summary_confidence_sample_<i>.json
            import glob as _glob
            for cif_path in _glob.glob(
                os.path.join(output_dir, "**", "predictions", "*_sample_*.cif"),
                recursive=True,
            ):
                stem = os.path.basename(cif_path)[:-4]  # strip ".cif"
                base, _, sample = stem.rpartition("_sample_")
                conf_meta = os.path.join(
                    os.path.dirname(cif_path),
                    f"{base}_summary_confidence_sample_{sample}.json",
                )
                if not base or not os.path.exists(conf_meta):
                    continue
                try:
                    payload = json.loads(open(conf_meta, encoding="utf-8").read())
                except Exception:  # noqa: BLE001
                    continue
                iptm = float(payload.get("iptm") or 0.0)
                # No sample filtering: every produced sample ships; ranking by
                # the model's own confidence (below) orders them. A collapsed
                # sample simply sorts last — selection is the engine's
                # confidence, not a discard rule.
                model_path = os.path.join(structure_dir, f"{tag}_model_{sample}.cif")
                _shutil.copyfile(cif_path, model_path)
                conf_json = os.path.join(
                    structure_dir, f"confidence_{tag}_model_{sample}.json")
                with open(conf_json, "w", encoding="utf-8") as fh:
                    json.dump({**payload, "iptm": iptm}, fh)
                scored.append((iptm, model_path))
                # remember the engine predictions dir + sample base for the
                # per-sample ipsae lookup after the best sample is chosen
                scored_meta.append((os.path.dirname(cif_path), model_path, base))
            if not scored:
                raise RuntimeError(
                    f"protenix2dock peptide mode produced no samples under {output_dir}"
                )
            scored.sort(reverse=True)
            best_iptm, best_path = scored[0]
            sample_no = os.path.basename(best_path).rsplit("_model_", 1)[1][:-4]
            payload = {"iptm": best_iptm, "structure_dir": structure_dir,
                       "best_cif": best_path, "docked_mode": "fixed",
                       "engine": "protenix-v2"}
            try:
                # IPSAE for the best sample: the engine writes
                # {sample_name}_ipsae_sample_{n}.json next to its own
                # predictions (the copied fixed/<tag>/ dir holds only the
                # copies this loop made, so globbing THERE never matched).
                # The source predictions dir rides on the scored tuple.
                best_entry = next(
                    (row for row in scored_meta if row[1] == best_path), None)
                if best_entry is not None:
                    src_dir, src_base = best_entry[0], best_entry[2]
                    ips_path = os.path.join(
                        src_dir, f"{src_base}_ipsae_sample_{sample_no}.json")
                    meta = None
                    if os.path.exists(ips_path):
                        meta = json.loads(open(ips_path, encoding="utf-8").read())
                    if meta is None:
                        # engine also merges the display fields into the
                        # per-sample summary confidence json
                        summary = json.loads(open(
                            os.path.join(src_dir, f"{src_base}_summary_confidence_sample_{sample_no}.json"),
                            encoding="utf-8").read())
                        meta = {
                            k: summary.get(k)
                            for k in ("ligand_ipsae_max", "ipsae_dom")
                        } if summary.get("ligand_ipsae_max") is not None else None
                    if meta:
                        lig_plddt = meta.get("ligand_plddt_mean")
                        if isinstance(lig_plddt, (int, float)):
                            # the design loop expects per-chain values on the
                            # 0-1 scale (it multiplies by 100)
                            payload["chain_mean_plddt"] = {"B": float(lig_plddt)}
                        payload["ligand_ipsae_max"] = meta.get("ligand_ipsae_max")
                        payload["ipsae_dom"] = meta.get("ipsae_dom")
            except Exception:  # noqa: BLE001
                pass
            with open(os.path.join(out_root, "confidence.json"), "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            logger.info("Task %s: dpeptide contract written (best iptm=%.3f).", task_id, best_iptm)

        output_archive_path = os.path.join(task_temp_dir, f"{task_id}_results.zip")
        # Archive layout uses the `protenix/output/` prefix: the frontend result
        # bundle parser's protenix branch keys on that path (summary-confidence
        # sample selection + structure matching + plddt/ipTM/IPSAE display).
        with zipfile.ZipFile(output_archive_path, "w") as zipf:
            for root, _dirs, files in os.walk(output_dir):
                for fname in sorted(files):
                    fpath = os.path.join(root, fname)
                    rel = os.path.relpath(fpath, output_dir)
                    zipf.write(fpath, f"protenix/output/{rel}")


        tracker.update_status("uploading", "Uploading results to central API")
        if gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
            gpu_id = -1
        upload_response = _tasks.upload_result_to_central_api(
            task_id, output_archive_path, os.path.basename(output_archive_path)
        )

        summary_path = os.path.join(output_dir, "protenix2dock_summary.json")
        summary: dict[str, Any] = {}
        if os.path.exists(summary_path):
            try:
                summary = json.loads(open(summary_path, encoding="utf-8").read())
            except Exception:  # noqa: BLE001
                summary = {}

        final_meta = {
            "status": "Complete",
            "gpu_id": reported_gpu,
            "upload_info": upload_response,
            "result_file": os.path.basename(output_archive_path),
            "mode": requested_mode,
            "best": summary.get("best"),
            "best_by_interface": summary.get("best_by_interface"),
        }
        self.update_state(state="SUCCESS", meta=final_meta)
        tracker.update_status("completed", "Task completed successfully")
        return final_meta

    except Ignore:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Task {task_id} failed: {e}", exc_info=True)
        tracker.update_status("failed", _tasks._truncate_text(e, 4000))
        self.update_state(state="FAILURE", meta=_tasks._build_failure_meta(e))
        raise
    finally:
        _tasks._terminate_task_containers_by_task_id(task_id)
        if gpu_id != -1:
            release_gpu(gpu_id=gpu_id, task_id=task_id)
