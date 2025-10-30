from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


class MolType(Enum):
    RNA = ("sequence", "rna")
    DNA = ("sequence", "dna")
    CCD = ("ccdCodes", "ligand")
    SMILES = ("smiles", "ligand")

    def __init__(self, af3code: str, upperclass: str):
        self.af3code = af3code
        self.upperclass = upperclass


class AF3Utils:
    def __init__(
        self,
        name: str,
        query_seqs_unique: List[str],
        query_seqs_cardinality: List[int],
        unpairedmsa: Optional[List[str]],
        pairedmsa: Optional[List[str]],
        extra_molecules: Optional[List[Tuple[MolType, str, int]]] = None,
    ) -> None:
        content = self.make_af3_input(
            name, query_seqs_unique, query_seqs_cardinality, unpairedmsa, pairedmsa
        )
        if extra_molecules:
            content = self.add_extra_molecules(content, extra_molecules)
        self.content = content

    def _int_id_to_str_id(self, i: int) -> str:
        if i <= 0:
            raise ValueError(f"int_id_to_str_id: Only positive integers allowed, got {i}")
        i = i - 1  # convert to zero-based
        output = []
        while i >= 0:
            output.append(chr(i % 26 + ord("A")))
            i = i // 26 - 1
        return "".join(output)

    def make_af3_input(
        self,
        name: str,
        query_seqs_unique: List[str],
        query_seqs_cardinality: List[int],
        unpairedmsa: Optional[List[str]],
        pairedmsa: Optional[List[str]],
    ) -> Dict[str, object]:
        sequences: List[Dict[str, object]] = []
        chain_id_count = 0
        for i, query_seq in enumerate(query_seqs_unique):
            chain_ids = [
                self._int_id_to_str_id(chain_id_count + j + 1)
                for j in range(query_seqs_cardinality[i])
            ]
            chain_id_count += query_seqs_cardinality[i]
            protein_entry: Dict[str, object] = {
                "protein": {
                    "id": chain_ids,
                    "sequence": query_seq,
                    "modifications": [],
                    "templates": [],
                }
            }
            if unpairedmsa and unpairedmsa[i]:
                protein_entry["protein"]["unpairedMsa"] = unpairedmsa[i]
            else:
                protein_entry["protein"]["unpairedMsa"] = ""
            if pairedmsa and pairedmsa[i]:
                protein_entry["protein"]["pairedMsa"] = pairedmsa[i]
            else:
                protein_entry["protein"]["pairedMsa"] = ""
            sequences.append(protein_entry)
        content: Dict[str, object] = {
            "dialect": "alphafold3",
            "version": 1,
            "name": f"{name}",
            "sequences": sequences,
            "modelSeeds": [1],
            "bondedAtomPairs": None,
            "userCCD": None,
        }
        return content

    def add_extra_molecules(
        self, content: Dict[str, object], molecules: List[Tuple[MolType, str, int]]
    ) -> Dict[str, object]:
        chain_id_count = 0
        for sequence in content["sequences"]:
            chain_id_count += len(sequence["protein"]["id"])  # type: ignore[index]

        unique_molecules: Dict[str, Dict[Tuple[MolType, str], int]] = {}
        for moltype, sequence, copies in molecules:
            upperclass = moltype.upperclass
            unique_molecules.setdefault(upperclass, {})
            entity = (moltype, sequence)
            unique_molecules[upperclass][entity] = (
                unique_molecules[upperclass].get(entity, 0) + copies
            )

        for upperclass, entities in unique_molecules.items():
            for (moltype, sequence), copies in entities.items():
                chain_ids = [
                    self._int_id_to_str_id(chain_id_count + j + 1) for j in range(copies)
                ]
                moldict = {upperclass: {"id": chain_ids}}
                af3code = moltype.af3code

                if moltype == MolType.CCD:
                    moldict[upperclass][af3code] = [sequence]
                else:
                    moldict[upperclass][af3code] = sequence
                    if moltype == MolType.RNA:
                        moldict[upperclass]["unpairedMsa"] = None

                content["sequences"].append(moldict)  # type: ignore[arg-type]
                chain_id_count += copies
        return content


@dataclass
class ProteinComponent:
    ids: List[str]
    sequence: str


@dataclass
class AF3Preparation:
    jobname: str
    proteins: List[ProteinComponent]
    chain_id_to_sequence: Dict[str, str]
    sequence_to_chain_ids: Dict[str, List[str]]
    header_labels: List[str]
    sequence_parts: List[str]
    other_molecules: List[Tuple[MolType, str, int]]
    query_sequences_unique: List[str]
    query_sequences_cardinality: List[int]
    chain_id_label_map: Dict[str, str]


def _sanitize_label(label: Optional[str], fallback: str) -> str:
    text = (label or "").strip()
    if not text:
        text = fallback
    safe = []
    for ch in text:
        if ch.isalnum() or ch in ("_", "-"):
            safe.append(ch)
        else:
            safe.append("_")
    sanitized = "".join(safe)
    return sanitized or fallback


def parse_yaml_for_af3(
    yaml_content: str, default_jobname: str = "boltz_af3"
) -> AF3Preparation:
    data = yaml.safe_load(yaml_content) or {}
    if "sequences" not in data or not data["sequences"]:
        raise ValueError("YAML content must contain at least one sequence entry.")

    jobname = data.get("name") or data.get("title") or default_jobname
    jobname = _sanitize_label(str(jobname), default_jobname)

    proteins: List[ProteinComponent] = []
    chain_id_to_sequence: Dict[str, str] = {}
    sequence_to_chain_ids: Dict[str, List[str]] = {}
    header_labels: List[str] = []
    sequence_parts: List[str] = []
    other_molecules: List[Tuple[MolType, str, int]] = []
    query_sequences_unique: List[str] = []
    query_sequences_cardinality: List[int] = []
    sequence_index_lookup: Dict[str, int] = {}
    chain_id_label_map: Dict[str, str] = {}

    molecule_counters: Dict[str, int] = {"ligand": 0, "dna": 0, "rna": 0}

    for entry in data["sequences"]:
        if not entry:
            continue
        entity_type = next(iter(entry)).lower()
        info = entry[entity_type]

        raw_ids = info.get("id")
        if raw_ids is None:
            ids = []
        elif isinstance(raw_ids, list):
            ids = [str(x) for x in raw_ids]
        else:
            ids = [str(raw_ids)]

        if entity_type == "protein":
            sequence = str(info.get("sequence", "")).strip().upper()
            if not sequence:
                raise ValueError("Protein entries must include a non-empty sequence.")
            proteins.append(ProteinComponent(ids=ids, sequence=sequence))

            index = sequence_index_lookup.get(sequence)
            if index is None:
                sequence_index_lookup[sequence] = len(query_sequences_unique)
                query_sequences_unique.append(sequence)
                query_sequences_cardinality.append(len(ids) or 1)
            else:
                query_sequences_cardinality[index] += len(ids) or 1

            if not ids:
                placeholder_id = f"CHAIN_{len(chain_id_to_sequence) + 1}"
                ids = [placeholder_id]

            for chain_id in ids:
                sanitized = _sanitize_label(chain_id, f"CHAIN_{len(header_labels)+1}")
                header_labels.append(sanitized)
                sequence_parts.append(sequence)
                chain_id_label_map[chain_id] = sanitized
                chain_id_to_sequence[chain_id] = sequence
                sequence_to_chain_ids.setdefault(sequence, []).append(chain_id)

        elif entity_type in {"dna", "rna"}:
            sequence = str(info.get("sequence", "")).strip().upper()
            if not sequence:
                raise ValueError(f"{entity_type.upper()} entries require a sequence.")
            moltype = MolType.DNA if entity_type == "dna" else MolType.RNA
            copies = len(ids) if ids else 1
            other_molecules.append((moltype, sequence, copies))

            molecule_counters[entity_type] += 1
            fallback_label = f"{entity_type.upper()}{molecule_counters[entity_type]}"
            label = ids[0] if ids else fallback_label
            sanitized = _sanitize_label(label, fallback_label)
            header_labels.append(sanitized)

            seq_repr = f"{entity_type}|{sequence}"
            if copies > 1:
                seq_repr += f"|{copies}"
            sequence_parts.append(seq_repr)

        elif entity_type == "ligand":
            copies = len(ids) if ids else 1
            if "ccd" in info:
                moltype = MolType.CCD
                sequence = str(info["ccd"]).strip().upper()
                seq_repr = f"ccd|{sequence}"
            elif "smiles" in info:
                moltype = MolType.SMILES
                sequence = str(info["smiles"]).strip()
                seq_repr = f"smiles|{sequence.replace(':', ';')}"
            else:
                raise ValueError("Ligand entries must define either 'ccd' or 'smiles'.")

            other_molecules.append((moltype, sequence, copies))
            molecule_counters["ligand"] += 1
            fallback_label = f"LIG{molecule_counters['ligand']}"
            label = ids[0] if ids else fallback_label
            sanitized = _sanitize_label(label, fallback_label)
            header_labels.append(sanitized)
            if copies > 1:
                seq_repr += f"|{copies}"
            sequence_parts.append(seq_repr)

        else:
            raise ValueError(f"Unsupported entity type '{entity_type}' in YAML.")

    if not proteins:
        raise ValueError("AlphaFold3 preparation requires at least one protein entry.")

    if not query_sequences_unique:
        raise ValueError("Failed to collect protein sequences for AF3 preparation.")

    return AF3Preparation(
        jobname=jobname,
        proteins=proteins,
        chain_id_to_sequence=chain_id_to_sequence,
        sequence_to_chain_ids=sequence_to_chain_ids,
        header_labels=header_labels,
        sequence_parts=sequence_parts,
        other_molecules=other_molecules,
        query_sequences_unique=query_sequences_unique,
        query_sequences_cardinality=query_sequences_cardinality,
        chain_id_label_map=chain_id_label_map,
    )


def build_af3_fasta(prep: AF3Preparation) -> str:
    header = f">{prep.jobname}|{':'.join(prep.header_labels)}"
    sequence_line = ":".join(prep.sequence_parts)
    return f"{header}\n{sequence_line}\n"


def collect_chain_msa_paths(
    prep: AF3Preparation, temp_dir: str, cache_dir: Optional[str] = None
) -> Dict[str, Path]:
    temp_path = Path(temp_dir)
    cache_path = Path(cache_dir) if cache_dir else None
    chain_to_path: Dict[str, Path] = {}

    for chain_id, sequence in prep.chain_id_to_sequence.items():
        candidate = temp_path / f"{chain_id}_msa.a3m"
        if candidate.exists():
            chain_to_path[chain_id] = candidate
            continue

        if cache_path:
            hash_path = cache_path / f"msa_{_sequence_hash(sequence)}.a3m"
            if hash_path.exists():
                chain_to_path[chain_id] = hash_path

    return chain_to_path


def _sequence_hash(sequence: str) -> str:
    return hashlib.md5(sequence.encode("utf-8")).hexdigest()


def load_unpaired_msa(
    prep: AF3Preparation, chain_msa_paths: Dict[str, Path]
) -> List[str]:
    unpaired: List[str] = []
    for sequence in prep.query_sequences_unique:
        msa_content: Optional[str] = None
        for chain_id in prep.sequence_to_chain_ids.get(sequence, []):
            path = chain_msa_paths.get(chain_id)
            if path and path.exists():
                msa_content = path.read_text()
                break
        unpaired.append(msa_content or "")
    return unpaired


def build_af3_json(prep: AF3Preparation, unpaired_msa: List[str]) -> Dict[str, object]:
    af3 = AF3Utils(
        prep.jobname,
        prep.query_sequences_unique,
        prep.query_sequences_cardinality,
        unpaired_msa,
        None,
        prep.other_molecules,
    )
    return af3.content


def serialize_af3_json(content: Dict[str, object]) -> str:
    return json.dumps(content, indent=2, ensure_ascii=False)


def safe_filename(name: str) -> str:
    return _sanitize_label(name, "file")
