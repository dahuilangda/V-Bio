from __future__ import annotations

import hashlib
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, List, Tuple

import gemmi


class OverlayBusyError(RuntimeError):
    """Raised when overlay compute queue is saturated."""


class LeadOptOverlayService:
    def __init__(
        self,
        *,
        max_workers: int = 4,
        max_pending: int = 32,
        cache_size: int = 256,
        cache_ttl_seconds: float = 300.0,
        task_timeout_seconds: float = 8.0,
    ) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max(1, int(max_workers)), thread_name_prefix="lead-opt-overlay")
        self._pending_slots = threading.BoundedSemaphore(max(1, int(max_pending)))
        self._cache_size = max(1, int(cache_size))
        self._cache_ttl = max(1.0, float(cache_ttl_seconds))
        self._task_timeout = max(1.0, float(task_timeout_seconds))
        self._cache_lock = threading.Lock()
        self._cache: OrderedDict[str, Tuple[float, Tuple[str, str]]] = OrderedDict()
        self._status_lock = threading.Lock()
        self._submitted = 0
        self._completed = 0
        self._timeouts = 0
        self._rejected = 0
        self._inflight = 0

    def build_overlay(
        self,
        *,
        complex_structure_text: str,
        complex_structure_format: str,
        ligand_chain_ids: List[str],
        residue_pairs: List[tuple[str, int]],
    ) -> tuple[str, str]:
        cache_key = self._build_cache_key(
            complex_structure_text=complex_structure_text,
            complex_structure_format=complex_structure_format,
            ligand_chain_ids=ligand_chain_ids,
            residue_pairs=residue_pairs,
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached

        if not self._pending_slots.acquire(blocking=False):
            with self._status_lock:
                self._rejected += 1
            raise OverlayBusyError("pocket_overlay queue is busy, please retry shortly.")

        with self._status_lock:
            self._submitted += 1
            self._inflight += 1

        future = self._executor.submit(
            self._compute_overlay,
            complex_structure_text=complex_structure_text,
            complex_structure_format=complex_structure_format,
            ligand_chain_ids=ligand_chain_ids,
            residue_pairs=residue_pairs,
        )
        try:
            result = future.result(timeout=self._task_timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            with self._status_lock:
                self._timeouts += 1
            raise TimeoutError("pocket_overlay timed out.") from exc
        finally:
            with self._status_lock:
                self._inflight = max(0, self._inflight - 1)
            self._pending_slots.release()

        with self._status_lock:
            self._completed += 1
        self._cache_set(cache_key, result)
        return result

    def get_status(self) -> dict[str, Any]:
        with self._status_lock:
            submitted = int(self._submitted)
            completed = int(self._completed)
            timeouts = int(self._timeouts)
            rejected = int(self._rejected)
            inflight = int(self._inflight)
        with self._cache_lock:
            cache_entries = len(self._cache)
        max_pending = getattr(self._pending_slots, "_initial_value", None)
        if not isinstance(max_pending, int) or max_pending <= 0:
            max_pending = inflight + 1
        return {
            "submitted": submitted,
            "completed": completed,
            "timeouts": timeouts,
            "rejected": rejected,
            "inflight": inflight,
            "max_workers": int(getattr(self._executor, "_max_workers", 0) or 0),
            "max_pending": int(max_pending),
            "cache_entries": cache_entries,
        }

    def _cache_get(self, key: str) -> tuple[str, str] | None:
        now = time.time()
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at <= now:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return value

    def _cache_set(self, key: str, value: tuple[str, str]) -> None:
        expires_at = time.time() + self._cache_ttl
        with self._cache_lock:
            self._cache[key] = (expires_at, value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    @staticmethod
    def _build_cache_key(
        *,
        complex_structure_text: str,
        complex_structure_format: str,
        ligand_chain_ids: List[str],
        residue_pairs: List[tuple[str, int]],
    ) -> str:
        chain_tokens = sorted({str(item).strip() for item in ligand_chain_ids if str(item).strip()})
        residue_tokens = sorted({f"{str(chain).strip()}:{int(seq)}" for chain, seq in residue_pairs})
        blob = (
            f"fmt={complex_structure_format}|chains={','.join(chain_tokens)}|"
            f"residues={','.join(residue_tokens)}|"
            f"sha={hashlib.sha1(complex_structure_text.encode('utf-8')).hexdigest()}"
        )
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _serialize_gemmi_structure_text(structure: Any, fmt: str) -> tuple[str, str]:
        resolved = "pdb" if str(fmt or "").strip().lower() == "pdb" else "cif"
        if resolved == "pdb":
            return structure.make_pdb_string(), "pdb"
        with tempfile.TemporaryDirectory(prefix="vbio_lead_opt_overlay_") as temp_dir:
            output = Path(temp_dir) / "overlay.cif"
            structure.make_mmcif_document().write_file(str(output))
            return output.read_text(encoding="utf-8"), "cif"

    def _compute_overlay(
        self,
        *,
        complex_structure_text: str,
        complex_structure_format: str,
        ligand_chain_ids: List[str],
        residue_pairs: List[tuple[str, int]],
    ) -> tuple[str, str]:
        fmt = "pdb" if str(complex_structure_format or "").strip().lower() == "pdb" else "cif"
        if not str(complex_structure_text or "").strip():
            raise ValueError("complex_structure_text is empty.")

        with tempfile.TemporaryDirectory(prefix="vbio_lead_opt_overlay_src_") as temp_dir:
            source_path = Path(temp_dir) / f"complex.{fmt}"
            source_path.write_text(complex_structure_text, encoding="utf-8")
            structure = gemmi.read_structure(str(source_path))
            structure.setup_entities()
            if len(structure) == 0:
                raise ValueError("complex_structure_text has no model.")
            source_model = structure[0]

            ligand_chains = {str(item or "").strip() for item in ligand_chain_ids if str(item or "").strip()}
            residue_set = {
                (str(chain_id).strip(), int(seq_num))
                for chain_id, seq_num in residue_pairs
                if str(chain_id).strip()
            }

            overlay = gemmi.Structure()
            try:
                overlay.spacegroup_hm = structure.spacegroup_hm
                overlay.cell = structure.cell
            except Exception:
                pass
            model = gemmi.Model("1")

            for chain in source_model:
                chain_id = str(chain.name or "").strip()
                if not chain_id:
                    continue
                if chain_id in ligand_chains:
                    model.add_chain(chain.clone())
                    continue

                selected_residues: List[Any] = []
                for residue in chain:
                    residue_number = int(residue.seqid.num)
                    if (chain_id, residue_number) not in residue_set:
                        continue
                    cloned = residue.clone()
                    # Mark as non-polymer so Mol* keeps stick-like representation.
                    cloned.het_flag = "H"
                    selected_residues.append(cloned)

                if not selected_residues:
                    continue
                new_chain = gemmi.Chain(chain_id)
                for residue in selected_residues:
                    new_chain.add_residue(residue)
                model.add_chain(new_chain)

            if len(model) == 0:
                raise ValueError("No ligand/pocket residues found for overlay.")
            overlay.add_model(model)
            overlay.setup_entities()
            return self._serialize_gemmi_structure_text(overlay, fmt)
