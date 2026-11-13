from __future__ import annotations

from pathlib import Path

import gemmi
import yaml
import pytest

from backend.runtime import rfd4_adapter as rb
from backend.runtime import bicyclic_linkers as bcl
from backend.runtime.run_single_prediction import (
    BICYCLIC_LINKER_ATOM_MAP,
    _linker_ccd_mmcif_bundle,
)


class TestLinkerRegistry:
    def test_atom_map_matches_registry(self):
        assert BICYCLIC_LINKER_ATOM_MAP == bcl.atom_map()

    def test_arms_exist_in_ccd_bundle(self):
        # every registered arm atom must exist in the linker_ccd bundle's
        # atom table and participate in at least one bond
        import gemmi
        for code, linker in bcl.LINKERS.items():
            blk = gemmi.cif.read(
                str(rb.Path(rb.__file__).parent / "linker_ccd" / f"{code}.cif")
            ).sole_block()
            atoms = {r[0] for r in blk.find("_chem_comp_atom.", ["atom_id"])}
            bonded = set()
            for r in blk.find("_chem_comp_bond.", ["atom_id_1", "atom_id_2"]):
                bonded.update((r[0], r[1]))
            for arm in linker.arm_atoms:
                assert arm in atoms, f"{code} arm {arm} missing from atom table"
                assert arm in bonded, f"{code} arm {arm} has no bond entry"

    def test_unknown_linker_rejected(self):
        with pytest.raises(ValueError, match="未知双环"):
            bcl.get_linker("NOPE")

    def test_bs3_not_registered(self):
        # symmetric arm ("BI" x3) cannot express three distinct covalent bonds
        with pytest.raises(ValueError, match="BS3"):
            bcl.get_linker("BS3")

    def test_engine_mismatch_rejected(self):
        with pytest.raises(ValueError, match="不支持后端 mol1"):
            bcl.arms_for_backend("SEZ", "mol1")
        assert bcl.arms_for_backend("ZBR", "mol1") == ["C7", "C8", "C9"]


class TestVerifyPayloadGuards:
    SEQ = "CALGCC"

    def test_arm_count_guard(self):
        with pytest.raises(ValueError, match="exactly 3 arm atoms"):
            rb.build_verify_yaml("MKV", self.SEQ, [], [1, 4, 6], "ZBR", ["C7", "C8"])

    def test_ascending_cys_guard(self):
        with pytest.raises(ValueError, match="ascending"):
            rb.build_verify_yaml("MKV", self.SEQ, [], [6, 4, 1], "ZBR", ["C7", "C8", "C9"])

    def test_cys_within_sequence_guard(self):
        with pytest.raises(ValueError, match="outside binder sequence"):
            rb.build_verify_yaml("MKV", self.SEQ, [], [1, 4, 9], "ZBR", ["C7", "C8", "C9"])

    def test_three_constraints_emitted(self):
        y = yaml.safe_load(rb.build_verify_yaml(
            "MKV", self.SEQ, [], [1, 4, 6], "ZBR", ["C9", "C8", "C7"]))
        assert len(y["constraints"]) == 3


class TestBinderExtraction:
    def _cif(self, comps):
        rows = []
        aid = 0
        for i, comp in enumerate(comps, start=1):
            for atom in ("N", "CA", "C", "O", "CB"):
                aid += 1
                rows.append(f"ATOM {aid} C {atom} . {comp} B 1 {i} ? 0 0 0 1.00 20.00 {i} {comp} B {atom} 1")
        lines = ["data_g", "#", "loop_"] + [f"_atom_site.{t}" for t in
                                            ["group_PDB", "id", "type_symbol", "label_atom_id", "label_alt_id",
                                             "label_comp_id", "label_asym_id", "label_entity_id",
                                             "label_seq_id", "pdbx_PDB_ins_code", "Cartn_x",
                                             "Cartn_y", "Cartn_z", "occupancy",
                                             "B_iso_or_equiv", "auth_seq_id", "auth_comp_id",
                                             "auth_asym_id", "auth_atom_id", "pdbx_PDB_model_num"]]
        return "\n".join(lines + rows) + "\n"

    def test_unknown_residue_rejected(self):
        # silent ALA mapping would shift every downstream mod/bond position
        with pytest.raises(ValueError, match="unrecognized residues"):
            rb.binder_from_cif_text(self._cif(["CYS", "XYZ", "CYS", "GLY", "CYS", "CYS"]))

    def test_ambiguous_shortest_chain_rejected(self):
        rows = []
        aid = 0
        for chain in ("B", "C"):
            for i in range(1, 7):
                for atom in ("N", "CA", "C", "O", "CB"):
                    aid += 1
                    rows.append(f"ATOM {aid} C {atom} . CYS {chain} 1 {i} ? 0 0 0 1.00 20.00 {i} CYS {chain} {atom} 1")
        lines = ["data_g", "#", "loop_"] + [f"_atom_site.{t}" for t in
                                            ["group_PDB", "id", "type_symbol", "label_atom_id", "label_alt_id",
                                             "label_comp_id", "label_asym_id", "label_entity_id",
                                             "label_seq_id", "pdbx_PDB_ins_code", "Cartn_x",
                                             "Cartn_y", "Cartn_z", "occupancy",
                                             "B_iso_or_equiv", "auth_seq_id", "auth_comp_id",
                                             "auth_asym_id", "auth_atom_id", "pdbx_PDB_model_num"]]
        with pytest.raises(ValueError, match="ambiguous binder"):
            rb.binder_from_cif_text("\n".join(lines + rows) + "\n")


def test_linker_bundle_has_descriptor_category():
    # AF3's CCD parser unconditionally reads _pdbx_chem_comp_descriptor;
    # bundles lacking it crash featurization (the SEZ KeyError lesson).
    # ZBR is the only linker injected into AF3 and its bundle must carry it.
    blk = gemmi.cif.read(
        str(Path(rb.__file__).parent / "linker_ccd" / "ZBR.cif")).sole_block()
    assert blk.find_value("_chem_comp.id") == "ZBR"
    assert len(blk.find("_pdbx_chem_comp_descriptor.", ["type"])) > 0
    assert _linker_ccd_mmcif_bundle(["ZBR"])
