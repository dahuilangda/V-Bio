"""退化序列检测(peplm.sequence_quality)与提案器批次过滤。

退化模式能骗过置信度排序、污染 elite 池与 GRPO 奖励(动机见
peplm.sequence_quality 模块注释)。本组测试锁定四类检测:同聚物连续段、
精确周期重复(窗口 6/6/8)、低复杂度窗口、单残基占比,以及复合分惩罚、
提案器过滤、固定段豁免与用户排除名单接线的测试。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PEPLM_ROOT = REPO_ROOT / "capabilities" / "peptide_lm"
for p in (str(REPO_ROOT), str(PEPLM_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from peplm import sequence_quality as sq  # noqa: E402
from peplm.integrate.esm3_proposer import _diversity_filter  # noqa: E402


class TestDegeneracyFlags:
    @pytest.mark.parametrize("seq,flag_substr", [
        ("AQAQAQAQ", "period_2"),           # v21 跑批实测(全池 458 标 65): D-TLE 周期 2
        ("ARVLARVL", "period_4"),           # v21 实测: beta-Catenin 周期 4
        ("AEKAAKAA", "period_3"),           # 6/8 精确周期 3(KAAKAA)
        ("RKAKKKKR", "homopolymer_run_4"),  # v21 实测: 4 连 K
        ("LQQQQRQQ", "homopolymer_run_4"),  # v21 实测: Q 占 6/8 + 4 连 Q
        ("AABBAABB", "2_residue_window"),
    ])
    def test_flagged(self, seq, flag_substr):
        flags = sq.degeneracy_flags(seq)
        assert flags, f"{seq} 应被标记"
        assert any(flag_substr in f for f in flags), (seq, flags)

    @pytest.mark.parametrize("seq", [
        "RGERARRL",          # v21 L-AR-V7 top1 的可变面
        "CREEECRGERARRLC",   # 完整双环肽(含固定 CREEEC 面)
        "CREEEC",            # 固定面本身(EEE 连 3 不触发)
        "LAKDSSRL",
        "SYNTFLLL",          # LLL 连 3 不触发
        "KYVIDDDL",          # DDD 连 3 不触发
        "EVQLKKAI",
        "LMEKYLNS",
    ])
    def test_clean(self, seq):
        assert sq.degeneracy_flags(seq) == [], (seq, sq.degeneracy_flags(seq))

    def test_empty_sequence(self):
        assert sq.degeneracy_flags("") == []

    def test_short_periodic_motif_not_flagged(self):
        # AQAQ 是基序不是塌缩模式: 周期 2 需要跨 6 个残基
        assert sq.degeneracy_flags("LAQAQGS") == []

    @pytest.mark.parametrize("seq", [
        "CREEECESEEFRELC",   # CREEEC 面的 E 泄漏进窗口曾致误判(E 实际 4/8)
        "CREEECAEEEFRELC",
        "CREEECLMEKYLNSC",
    ])
    def test_fixed_prefix_not_counted(self, seq):
        # 固定 CREEEC 面是设计不是退化, 窗口/周期检查必须剥离它
        assert sq.degeneracy_flags(seq) == [], (seq, sq.degeneracy_flags(seq))

    def test_prefix_strip_keeps_period_detection(self):
        # 剥离前缀后可变面内的周期仍要抓到
        assert any("period_2" in f
                   for f in sq.degeneracy_flags("CREEECAQAQAQAQC"))

    def test_fixed_suffix_strip(self):
        # 掩码固定末位时, 末位残基不参与判定
        assert sq.degeneracy_flags(
            "RGERARRLE", fixed_prefix="", fixed_suffix="E") == []

    def test_short_variable_flag_naming(self):
        # 短可变面的标记名与实际残基数一致(曾一律叫 two_residue_window)
        assert sq.degeneracy_flags("CREEECAAA") == ["1_residue_window"]
        assert sq.degeneracy_flags("CREEECAK") == ["2_residue_window"]

    def test_user_mask_prefix_exempt(self):
        # 掩码固定面非 CREEEC(如 EEEE)时按掩码豁免, 不对每个候选误扣
        assert sq.degeneracy_flags(
            "EEEELRKRDL", fixed_prefix="EEEE") == []


class TestDegeneracyPenalty:
    def test_no_flags_no_penalty(self):
        assert sq.degeneracy_penalty([]) == 0.0

    def test_scales_per_flag(self):
        assert sq.degeneracy_penalty(["a"]) == pytest.approx(0.15)
        assert sq.degeneracy_penalty(["a", "b"]) == pytest.approx(0.30)

    def test_capped(self):
        assert sq.degeneracy_penalty(["a"] * 10) == pytest.approx(0.45)


class TestDiversityFilter:
    def _prop(self, seq, group="g"):
        return (seq, [], [], group)

    def test_drops_degenerate_keeps_clean(self):
        flagged = self._prop("CREEECAQAQAQAQC")
        clean = self._prop("CREEECRGERARRLC")
        kept = _diversity_filter([flagged, clean])
        assert [p[0] for p in kept] == ["CREEECRGERARRLC"]

    def test_drops_near_duplicates(self):
        a = self._prop("CREEECRGERARRLC")
        b = self._prop("CREEECRGERAQRLC")  # 单点突变, 恒等度 14/15
        kept = _diversity_filter([a, b])
        assert len(kept) == 1

    def test_homopolymer_rejected(self):
        # 历史事故: R7 提案带 inter_min 2.08 A / 27 clash 对
        kept = _diversity_filter([self._prop("CREEECRRRRRRRC")])
        assert kept == []


class TestExcludedAasWiring:
    def test_proposer_normalization(self):
        from peplm.integrate.esm3_proposer import ESM3Proposer
        p = ESM3Proposer(receptor_sequence="ACDEFG",
                         excluded_aas=["c", "M", "X", "", "zz"])
        assert sorted(p._excluded_aas) == ["C", "M"]

    def test_filter_keeps_ring_anchors_and_drops_free_violation(self):
        from peplm.integrate.esm3_proposer import ESM3Proposer, _diversity_filter
        p = ESM3Proposer(receptor_sequence="ACDEFG", excluded_aas=["M"],
                         fixed_prefix="CREEEC", fixed_suffix="C")
        props = [("CREEECRGERARRLC", [], [], "a"),   # 环锚点 C 不在名单, 保留
                 ("CREEECMMMMAAAC", [], [], "b")]    # 自由位 M → 拒
        kept = _diversity_filter(props, excluded_aas=p._excluded_aas,
                                 fixed_prefix=p._fixed_prefix,
                                 fixed_suffix=p._fixed_suffix)
        assert [x[0] for x in kept] == ["CREEECRGERARRLC"]

    def test_normalize_rejects_invalid_letters(self):
        import backend.runtime.run_single_prediction as rsp
        assert rsp._normalize_excluded_aas(["C", "m"]) == ["C", "M"]
        assert rsp._normalize_excluded_aas("C,M") == ["C", "M"]
        assert rsp._normalize_excluded_aas("C M") == ["C", "M"]
        assert rsp._normalize_excluded_aas(None) == []
        with pytest.raises(ValueError):
            rsp._normalize_excluded_aas(["B"])


class TestAdaptiveMask:
    """长度窗口下掩码自由段伸缩(固定两端布局)。"""

    def test_adapt_mask_to_length(self):
        import backend.runtime.run_single_prediction as rsp
        mask = "CREEECXXXXXXXXC"
        assert rsp._adapt_mask_to_length(mask, 15) == mask
        assert rsp._adapt_mask_to_length(mask, 12) == "CREEECXXXXXC"
        assert rsp._adapt_mask_to_length(mask, 18) == "CREEECXXXXXXXXXXXC"
        # 掩码无自由位(固定段吃满整条): 无法伸缩
        assert rsp._adapt_mask_to_length("CREEEC", 10) is None
        # 中部固定残基(C 不与两端连续): 无法伸缩
        assert rsp._adapt_mask_to_length("CREECXXCXXEEC", 10) is None
        # 全固定掩码: 无法伸缩
        assert rsp._adapt_mask_to_length("CREEECRRRC", 10) is None

    def test_apply_sequence_mask_variable_length(self):
        import backend.runtime.run_single_prediction as rsp
        mask = "CREEECXXXXXXXXC"
        # 13 aa 候选: 布局保持 CREEEC+自由+C, 自由位 Cys 降为 S
        out = rsp._apply_sequence_mask("cREEECaptcaek", mask)
        assert len(out) == 13
        assert out.startswith("CREEEC")
        assert out.endswith("C")
        assert "C" not in out[6:-1]
        # 同长行为不变
        assert rsp._apply_sequence_mask("CREEECRGERARRLC", mask) == "CREEECRGERARRLC"


class TestProposerFixes:
    """三个提案器修复的回归锁定。"""

    def test_identity_computed_on_variable_region(self):
        from peplm.integrate.esm3_proposer import _diversity_filter
        # 自由区差 2 步(全长 13/15=0.867 会被旧全长逻辑剔除), 可变区 6/8=0.75 保留
        a = "CREEECRLKDAEQC"
        b = "CREEECRLKDAQEC"
        kept = _diversity_filter([(a, [], [], "g"), (b, [], [], "g")],
                                 fixed_prefix="CREEEC", fixed_suffix="C")
        assert [x[0] for x in kept] == [a, b]

    def test_single_step_mutation_near_threshold(self):
        from peplm.integrate.esm3_proposer import _diversity_filter
        # 可变区单步 7/8=0.875 仍超 0.85 阈值: 该兄弟由 mutate 通道的
        # round-robin 配额保证, 不由过滤器负责; 两步差异(0.75)必须存活
        a = "CREEECRLKDAEQC"
        b = "CREEECRLKDAQEC"
        c = "CREEECRLKDEEYC"
        kept = _diversity_filter([(a, [], [], "g"), (b, [], [], "g"), (c, [], [], "g")],
                                 fixed_prefix="CREEEC", fixed_suffix="C")
        assert [x[0] for x in kept] == [a, b, c]
