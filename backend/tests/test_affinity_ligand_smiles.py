"""Ligand SMILES extraction for the Boltz2Score affinity handoff."""

from backend.runtime.run_single_prediction import _extract_ligand_smiles_list


def test_string_form():
    yaml_data = {
        "sequences": [
            {"proteinChain": {"sequence": "AAA"}},
            {"ligand": "COc1ccccc1"},
        ]
    }
    assert _extract_ligand_smiles_list(yaml_data) == ["COc1ccccc1"]


def test_dict_form():
    yaml_data = {"sequences": [{"ligand": {"ligand": "CC(O)C", "id": "C"}}]}
    assert _extract_ligand_smiles_list(yaml_data) == ["CC(O)C"]


def test_multiple_ligands_keep_yaml_order():
    """Protenix 以出现顺序命名配体实体（l01、l02…），顺序即实体序号。"""
    yaml_data = {
        "sequences": [
            {"ligand": "C(F)(F)F"},
            {"proteinChain": {"sequence": "AAA", "id": "A"}},
            {"ligand": {"ligand": "c1ccccc1C", "id": "C"}},
        ]
    }
    assert _extract_ligand_smiles_list(yaml_data) == ["C(F)(F)F", "c1ccccc1C"]


def test_smiles_key_form_is_explicit():
    """本后端解析器的 `smiles:` 键是显式声明，短大写串也原样采用。"""
    yaml_data = {"sequences": [{"ligand": {"id": "C", "smiles": "CCO"}}]}
    assert _extract_ligand_smiles_list(yaml_data) == ["CCO"]


def test_user_rittac_yaml_form():
    """DDTools 实际提交形态：蛋白+蛋白+smiles 配体，必须能取到拓扑。"""
    yaml_data = {
        "sequences": [
            {"protein": {"id": "A", "sequence": "AAA"}},
            {"protein": {"id": "B", "sequence": "BBB"}},
            {"ligand": {"id": "C", "smiles": "COc1cc(N2CCN(CC3(O))CC3)ccc1"}},
        ]
    }
    assert _extract_ligand_smiles_list(yaml_data) == ["COc1cc(N2CCN(CC3(O))CC3)ccc1"]


def test_ccd_code_not_treated_as_smiles():
    assert _extract_ligand_smiles_list({"sequences": [{"ligand": "ATP"}]}) == []
    assert _extract_ligand_smiles_list({"sequences": [{"ligand": "L01"}]}) == []
    # 全大写短串即使能解析成 SMILES 也按 CCD 处理（误判方向保守：退回有界
    # 坐标感知路径）。
    assert _extract_ligand_smiles_list({"sequences": [{"ligand": "CCO"}]}) == []


def test_empty_and_missing():
    assert _extract_ligand_smiles_list(None) == []
    assert _extract_ligand_smiles_list({}) == []
    assert _extract_ligand_smiles_list({"sequences": []}) == []


def test_skips_non_ligand_entries():
    yaml_data = {
        "sequences": [
            {"proteinChain": {"sequence": "AAA", "id": "A"}},
            {"proteinChain": {"sequence": "BBB", "id": "B"}},
            {"ligand": {"ligand": "c1ccccc1C", "id": "C"}},
        ]
    }
    assert _extract_ligand_smiles_list(yaml_data) == ["c1ccccc1C"]


def test_whitespace_and_blank_ligand_ignored():
    yaml_data = {
        "sequences": [
            {"ligand": "   "},
            {"ligand": {"ligand": " C(F)(F)F "}},
        ]
    }
    assert _extract_ligand_smiles_list(yaml_data) == ["C(F)(F)F"]


def test_lowercase_short_smiles_accepted():
    # 小写芳香原子是大写 CCD 不会出现的形态
    assert _extract_ligand_smiles_list({"sequences": [{"ligand": "c1ccn"}]}) == ["c1ccn"]
