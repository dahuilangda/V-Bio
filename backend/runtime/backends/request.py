from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


@dataclass
class Modification:
    position: int
    ccd: str


@dataclass
class SequenceEntity:
    kind: str
    chain_id: str
    ids: list[str]
    sequence: str = ""
    modifications: list[Modification] = field(default_factory=list)
    cyclic: bool = False
    smiles: str = ""
    ccd: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class BondSpec:
    chain1: str
    residue1: int
    atom1: str
    chain2: str
    residue2: int
    atom2: str


@dataclass
class PredictionRequest:
    entities: list[SequenceEntity]
    bonds: list[BondSpec]
    other_constraints: list[dict]
    source_yaml: str
    custom_ccd_molecules: list[dict] = field(default_factory=list)

    def entity_by_chain(self, chain: str) -> SequenceEntity | None:
        return next((e for e in self.entities if e.chain_id == chain), None)

    def polymer_chains(self) -> dict[str, SequenceEntity]:
        chains: dict[str, SequenceEntity] = {}
        for e in self.entities:
            if e.kind not in ("protein", "rna", "dna") or not e.sequence:
                continue
            for chain in e.ids or ([e.chain_id] if e.chain_id else []):
                chains[chain] = e
        return chains

    def sequence_lengths(self) -> dict[str, int]:
        return {chain: len(e.sequence) for chain, e in self.polymer_chains().items()}

    @classmethod
    def from_yaml(cls, yaml_text: str) -> "PredictionRequest":
        data = yaml.safe_load(yaml_text) or {}
        entities: list[SequenceEntity] = []
        bonds: list[BondSpec] = []
        other: list[dict] = []

        for item in data.get("sequences") or []:
            if not isinstance(item, dict):
                continue
            for kind in ("protein", "rna", "dna", "ligand", "ion"):
                entity = item.get(kind)
                if not isinstance(entity, dict):
                    continue
                entities.append(_parse_entity(kind, entity))
                break

        for constraint in data.get("constraints") or []:
            if not isinstance(constraint, dict):
                continue
            bond = constraint.get("bond")
            if isinstance(bond, dict):
                bonds.append(_parse_bond(bond))
            else:
                other.append(constraint)

        return cls(entities=entities, bonds=bonds, other_constraints=other,
                   source_yaml=yaml_text,
                   custom_ccd_molecules=data.get("custom_ccd_molecules")
                   if isinstance(data.get("custom_ccd_molecules"), list) else [])

    def to_yaml(self) -> str:
        sequences = []
        for e in self.entities:
            body: dict[str, Any] = {"id": e.ids or e.chain_id}
            if e.kind in ("protein", "rna", "dna"):
                body["sequence"] = e.sequence
                if e.modifications:
                    body["modifications"] = [
                        {"position": m.position, "ccd": m.ccd} for m in e.modifications]
                if e.cyclic:
                    body["cyclic"] = True
            elif e.smiles:
                body["smiles"] = e.smiles
            elif e.ccd:
                body["ccd"] = e.ccd
            sequences.append({e.kind: body})
        constraints = [{"bond": {
            "atom1": [b.chain1, b.residue1, b.atom1],
            "atom2": [b.chain2, b.residue2, b.atom2],
        }} for b in self.bonds] + self.other_constraints
        data = {"sequences": sequences}
        if constraints:
            data["constraints"] = constraints
        if self.custom_ccd_molecules:
            data["custom_ccd_molecules"] = self.custom_ccd_molecules
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def _parse_entity(kind: str, entity: dict) -> SequenceEntity:
    raw_ids = entity.get("id")
    if isinstance(raw_ids, list) and raw_ids:
        ids = [str(x) for x in raw_ids]
    elif raw_ids:
        ids = [str(raw_ids)]
    else:
        ids = []
    modifications = [
        Modification(position=int(m["position"]), ccd=str(m["ccd"]))
        for m in (entity.get("modifications") or [])
        if isinstance(m, dict) and m.get("ccd")
    ]
    return SequenceEntity(
        kind=kind,
        chain_id=ids[0] if ids else "",
        ids=ids,
        sequence=str(entity.get("sequence") or ""),
        modifications=modifications,
        cyclic=bool(entity.get("cyclic")),
        smiles=str(entity.get("smiles") or ""),
        ccd=[c for c in (entity.get("ccd") if isinstance(entity.get("ccd"), list)
                         else [entity.get("ccd")]) if c],
        raw=entity,
    )


def _parse_bond(bond: dict) -> BondSpec:
    a1, a2 = bond.get("atom1") or [], bond.get("atom2") or []
    return BondSpec(
        chain1=str(a1[0]).strip(), residue1=int(a1[1]), atom1=str(a1[2]).strip(),
        chain2=str(a2[0]).strip(), residue2=int(a2[1]), atom2=str(a2[2]).strip(),
    )
