"""Ligand SMILES extraction for the Boltz2Score affinity handoff."""

from backend.runtime.run_single_prediction import _extract_first_ligand_smiles


def test_string_form():
    yaml_data = {
        "sequences": [
            {"proteinChain": {"sequence": "AAA"}},
            {"ligand": "COc1ccccc1"},
        ]
    }
    assert _extract_first_ligand_smiles(yaml_data) == "COc1ccccc1"


def test_dict_form():
    yaml_data = {"sequences": [{"ligand": {"ligand": "CC(O)C", "id": "C"}}]}
    assert _extract_first_ligand_smiles(yaml_data) == "CC(O)C"


def test_ccd_code_not_treated_as_smiles():
    assert _extract_first_ligand_smiles({"sequences": [{"ligand": "ATP"}]}) is None
    assert _extract_first_ligand_smiles({"sequences": [{"ligand": "L01"}]}) is None
    # 全大写短串即使能解析成 SMILES 也按 CCD 处理（误判方向保守：退回有界坐标路径）
    assert _extract_first_ligand_smiles({"sequences": [{"ligand": "CCO"}]}) is None


def test_empty_and_missing():
    assert _extract_first_ligand_smiles(None) is None
    assert _extract_first_ligand_smiles({}) is None
    assert _extract_first_ligand_smiles({"sequences": []}) is None


def test_skips_non_ligand_entries():
    yaml_data = {
        "sequences": [
            {"proteinChain": {"sequence": "AAA", "id": "A"}},
            {"proteinChain": {"sequence": "BBB", "id": "B"}},
            {"ligand": {"ligand": "c1ccccc1C", "id": "C"}},
        ]
    }
    assert _extract_first_ligand_smiles(yaml_data) == "c1ccccc1C"


def test_whitespace_and_blank_ligand_ignored():
    yaml_data = {
        "sequences": [
            {"ligand": "   "},
            {"ligand": {"ligand": " C(F)(F)F "}},
        ]
    }
    assert _extract_first_ligand_smiles(yaml_data) == "C(F)(F)F"


def test_lowercase_short_smiles_accepted():
    # 小写芳香原子是大写 CCD 不会出现的形态
    assert _extract_first_ligand_smiles({"sequences": [{"ligand": "c1ccn"}]}) == "c1ccn"
