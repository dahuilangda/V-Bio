"""Complex multi-turn simulation suite — the upgrade's exposed-problem regression tests.

These encode the structural gaps a simulated gemma4-strength planner exposed when driving the
FULL loop (model call → audit → skill execution → replan → final-answer verification) through
realistic complex conversations. The model's JSON turns are scripted to be CORRECT planner
behavior; each test pins that the harness/planner machinery lets a competent plan complete:

  - OutlineBudgetScalingTests: a correctly-planned 6-step outline needs ~13 rounds — the fixed
    8-round budget used to kill it. The budget now scales with the outline.
  - DataAnswerEnrichmentTests: lookup answers used to leave the grammar-constrained short
    message; they now get phase-2 free-text enrichment, grounded on the observations.
  - GroundedFinalAnswerTests: hallucinated multi-record answers used to pass; small record sets
    are now audited, with one correction round and enrichment re-verification.
  - ContextCompactionTests: a 15-step plan with 4000-char sequences used to blow the 64k
    message cap and crash the turn; older feedback rounds are elided (ledger preserves data).
  - RedundantCallAuditTests: repeating a succeeded lookup is rejected; retries of failures and
    $fromObservation consumption are not.
  - OutlineMidroundGroundingTests: mid-outline transition messages are not user answers and are
    exempt from the grounding audit (the final message is verified at completion instead).
  - ComputeSkillUpgradeTests: the new local unit operations (unit conversion, sequence stats).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import CopilotAssistant, _observation_ledger
from management_api.copilot_skill_harness import CopilotSkillHarness
from management_api.copilot_skills.compute_skills import convert_units, sequence_stats
from management_api.copilot_skills.online_databases import OnlineSkillDefinition
from tests.helpers import FakeResponse, NullLogger


class SequenceModelSession:
    def __init__(self, responses: List[str]) -> None:
        self.responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    def post(self, url: str, headers: Dict[str, str] | None = None, json: Any = None, timeout: float | None = None, **kwargs: Any) -> FakeResponse:
        self.requests.append({"url": url, "json": json})
        if not self.responses:
            from tests.helpers import is_phase2_request
            if is_phase2_request(json):
                return FakeResponse("")
            raise AssertionError("Unexpected model request.")
        return FakeResponse(self.responses.pop(0))


def make_assistant(responses: List[Any], max_rounds: int = 8) -> Tuple[CopilotAssistant, SequenceModelSession]:
    session = SequenceModelSession([r if isinstance(r, str) else json.dumps(r) for r in responses])
    assistant = CopilotAssistant(
        chat_api_url="http://model.invalid/v1/chat/completions",
        chat_api_key="test-key", chat_model="test-model",
        timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=max_rounds,
    )
    return assistant, session


def turn(message: str, operations: List[Dict[str, Any]] | None = None,
         questions: List[Dict[str, Any]] | None = None,
         goal_steps: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    t: Dict[str, Any] = {"message": message, "questions": questions or [], "operations": operations or []}
    if goal_steps is not None:
        t["goal_steps"] = goal_steps
    return t


def rop(op_id: str, skill: str, arguments: Dict[str, Any], depends: List[str] | None = None) -> Dict[str, Any]:
    return {"id": op_id, "skill": skill, "arguments": arguments, "depends_on": depends or []}


def inject_read_skill(
    assistant: CopilotAssistant,
    name: str,
    handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    schema: Dict[str, Any] | None = None,
) -> None:
    definition = OnlineSkillDefinition(
        name=name, description=f"{name} skill.",
        input_schema=schema or {
            "type": "object",
            "properties": {"q": {"type": "string", "minLength": 1}},
            "required": ["q"], "additionalProperties": False,
        },
    )
    # Registry is the single source of truth: one register() makes the skill executable
    # AND auditable (the harness derives its catalog live).
    assistant.skill_harness.skills.register(definition, handler)


SIMPLE_CTX = {"project": {"id": "p1"}}


# --------------------------------------------------------------------------- #
# 1. Outline round-budget scaling
# --------------------------------------------------------------------------- #


class OutlineBudgetScalingTests(unittest.TestCase):
    """A correct 6-step research plan needs ~13 rounds; the budget must scale with it."""

    def _run_research_plan(self) -> Tuple[Dict[str, Any], SequenceModelSession]:
        steps = [
            {"description": "查找 DHODH 的 UniProt 条目"},
            {"description": "解析 DHODH 的完整序列"},
            {"description": "检索 ChEMBL 生物活性数据"},
            {"description": "统计 IC50 均值"},
            {"description": "检索相关 PubMed 文献"},
            {"description": "汇总调研结论"},
        ]
        scripted: List[Any] = [turn("我先制定计划。", goal_steps=steps)]
        reads = [
            ("s1", "uniprot.search", {"q": "DHODH"}),
            ("s2", "uniprot.resolve", {"q": "Q02127"}),
            ("s3", "chembl.bioactivity", {"q": "DHODH"}),
            ("s4", "compute.aggregate", {"values": [1, 2, 3]}),
            ("s5", "pubmed.search", {"q": "DHODH inhibitor"}),
        ]
        for sid, skill, args in reads:
            scripted.append(turn(f"执行 {skill}。", operations=[rop(sid, skill, args)]))
            scripted.append(turn("数据已返回。"))
        scripted.append(turn("调研完成,共 6 步。"))
        scripted.append("完整调研总结:DHODH (Q02127),IC50 均值 5.0 nM,相关文献 3 篇。")

        assistant, session = make_assistant(scripted, max_rounds=8)
        inject_read_skill(assistant, "uniprot.search", lambda a: {
            "source": "uniprot", "results": [{"accession": "Q02127", "proteinName": "DHODH", "organism": "Human"}]})
        inject_read_skill(assistant, "uniprot.resolve", lambda a: {
            "source": "uniprot", "accession": "Q02127", "sequence": "MSEQ" * 30})
        inject_read_skill(assistant, "chembl.bioactivity", lambda a: {
            "source": "chembl", "results": [
                {"target": "DHODH", "activityType": "IC50", "value": 5.0, "units": "nM"}] * 5})
        inject_read_skill(assistant, "pubmed.search", lambda a: {
            "source": "pubmed", "results": [{"pmid": "123", "title": "DHODH inhibitors"}] * 3})
        inject_read_skill(
            assistant, "compute.aggregate", lambda a: {"source": "compute", "results": [{"count": 5, "mean": 5.0}]},
            schema={"type": "object", "properties": {"values": {"type": "array", "items": {"type": "number"}, "minItems": 1}}, "required": ["values"], "additionalProperties": False},
        )
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice",
            content="帮我做 DHODH 药物调研:查 UniProt、解析序列、查 ChEMBL 活性、算 IC50 均值、查文献、总结",
        )
        return result, session

    def test_six_step_outline_completes_beyond_simple_budget(self):
        result, session = self._run_research_plan()
        self.assertEqual(result["state"], "complete")
        # 13 planner rounds exceed the configured budget of 8 — the outline scaled it.
        self.assertGreater(len(session.requests), 8)
        self.assertIn("Q02127", result["content"])

    def test_step_drive_messages_carry_progress_and_ledger(self):
        result, session = self._run_research_plan()
        self.assertEqual(result["state"], "complete")
        # Some later request must contain the completed-steps recap and the observation ledger.
        saw_progress = saw_ledger = False
        for request in session.requests:
            messages = request["json"]["messages"]
            for message in messages:
                content = str(message.get("content") or "")
                if "COMPLETED STEPS:" in content:
                    saw_progress = True
                if "OBSERVATION LEDGER" in content or "OBSERVATIONS AVAILABLE" in content:
                    saw_ledger = True
        self.assertTrue(saw_progress, "step rounds should carry the completed-steps recap")
        self.assertTrue(saw_ledger, "step rounds should carry the observation ledger")


# --------------------------------------------------------------------------- #
# 2. Data-answer phase-2 enrichment
# --------------------------------------------------------------------------- #


class DataAnswerEnrichmentTests(unittest.TestCase):
    def test_lookup_answer_is_enriched_with_free_text(self):
        assistant, session = make_assistant([
            turn("查询 DHODH 活性。", operations=[rop("a1", "chembl.bioactivity", {"q": "DHODH"})]),
            turn("已查到 5 条 DHODH 活性记录。"),
            "完整总结:DHODH 共 5 条 IC50 记录,最低 1.2 nM,最高 9.0 nM,均值 5.4 nM。",
        ])
        inject_read_skill(assistant, "chembl.bioactivity", lambda a: {
            "source": "chembl", "results": [
                {"target": "DHODH", "activityType": "IC50", "value": v, "units": "nM"}
                for v in (1.2, 3.4, 5.6, 7.8, 9.0)]})
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="查 DHODH 的 ChEMBL 活性并给我完整总结")
        self.assertEqual(result["state"], "complete")
        self.assertIn("均值 5.4 nM", result["content"])
        # The phase-2 request is free-text (response_format text) and carries the data context.
        phase2 = session.requests[-1]["json"]
        self.assertEqual(phase2["response_format"], {"type": "text"})
        joined = "".join(str(m.get("content") or "") for m in phase2["messages"])
        self.assertIn("DHODH", joined)

    def test_phase2_failure_falls_back_to_planner_message(self):
        assistant, session = make_assistant([
            turn("查询完成。", operations=[rop("a1", "chembl.bioactivity", {"q": "DHODH"})]),
            turn("查到 DHODH 活性 IC50 5.0 nM。"),
            "",  # phase-2 returns empty -> fall back to the audited planner message
        ])
        inject_read_skill(assistant, "chembl.bioactivity", lambda a: {
            "source": "chembl", "results": [{"target": "DHODH", "activityType": "IC50", "value": 5.0, "units": "nM"}]})
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="查 DHODH 活性")
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["content"], "查到 DHODH 活性 IC50 5.0 nM。")

    def test_phase2_planner_json_envelope_is_not_shown_to_user(self):
        # A quirky model returns the planner JSON envelope to the free-text request: the raw
        # object must not become the user-facing answer; the inner message (or the planner's) is.
        assistant, session = make_assistant([
            turn("查询完成。", operations=[rop("a1", "chembl.bioactivity", {"q": "DHODH"})]),
            turn("查到 DHODH 活性 IC50 5.0 nM。"),
            json.dumps({"message": "查到 DHODH 活性 IC50 5.0 nM,共 1 条记录。", "questions": [], "operations": []}, ensure_ascii=False),
        ])
        inject_read_skill(assistant, "chembl.bioactivity", lambda a: {
            "source": "chembl", "results": [{"target": "DHODH", "activityType": "IC50", "value": 5.0, "units": "nM"}]})
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="查 DHODH 活性")
        self.assertEqual(result["state"], "complete")
        self.assertFalse(result["content"].lstrip().startswith("{"))


# --------------------------------------------------------------------------- #
# 3. Grounded final answers (multi-record audit + correction + enrichment re-check)
# --------------------------------------------------------------------------- #


class GroundedFinalAnswerTests(unittest.TestCase):
    def _two_record_obs(self) -> Any:
        return lambda a: {
            "source": "chembl", "results": [
                {"target": "COX1", "activityType": "IC50", "value": 5.0, "units": "nM"},
                {"target": "COX2", "activityType": "IC50", "value": 8.0, "units": "nM"}]}

    def test_hallucinated_multirecord_answer_is_corrected(self):
        assistant, session = make_assistant([
            turn("查询完成。", operations=[rop("c1", "chembl.bioactivity", {"q": "aspirin"})]),
            turn("找到了 BRD4 和 CDK6 两个靶点的强效抑制剂。"),  # names NOTHING retrieved
            turn("找到了 COX1 和 COX2 两个靶点的活性记录。"),  # correction round
            "完整总结:aspirin 靶点 COX1 (IC50 5.0 nM) 与 COX2 (IC50 8.0 nM)。",
        ])
        inject_read_skill(assistant, "chembl.bioactivity", self._two_record_obs())
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="aspirin 的靶点活性")
        self.assertEqual(result["state"], "complete")
        self.assertIn("COX1", result["content"])
        self.assertNotIn("BRD4", result["content"])

    def test_enrichment_that_loses_grounding_falls_back(self):
        # The planner message is grounded; the phase-2 rewrite drops the anchors — the rewrite
        # is refused and the audited planner message is returned instead.
        assistant, session = make_assistant([
            turn("查询完成。", operations=[rop("c1", "chembl.bioactivity", {"q": "aspirin"})]),
            turn("COX1 IC50 5.0 nM,COX2 IC50 8.0 nM。"),
            "阿司匹林的靶点活性非常强,是很好的研究起点。",  # no anchor from the records
        ])
        inject_read_skill(assistant, "chembl.bioactivity", self._two_record_obs())
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="aspirin 的靶点活性")
        self.assertEqual(result["content"], "COX1 IC50 5.0 nM,COX2 IC50 8.0 nM。")

    def test_outline_final_message_is_verified_after_all_steps(self):
        # The outline path's final message is not seen by audit_plan's grounding check — the
        # loop verifies it at completion and runs one correction round when it fails.
        steps = [{"description": "查 aspirin 活性"}, {"description": "总结靶点"}]
        assistant, session = make_assistant([
            turn("计划。", goal_steps=steps),
            turn("检索。", operations=[rop("c1", "chembl.bioactivity", {"q": "aspirin"})]),
            turn("数据已返回。"),
            turn("找到了 BRD4 和 CDK6。"),  # final outline message, ungrounded
            turn("找到了 COX1 和 COX2。"),  # correction
            "完整总结:aspirin 靶点 COX1 (IC50 5.0 nM) 与 COX2 (IC50 8.0 nM)。",
        ])
        inject_read_skill(assistant, "chembl.bioactivity", self._two_record_obs())
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="aspirin 活性调研并总结")
        self.assertEqual(result["state"], "complete")
        self.assertNotIn("BRD4", result["content"])


# --------------------------------------------------------------------------- #
# 4. Context compaction on long plans
# --------------------------------------------------------------------------- #


class ContextCompactionTests(unittest.TestCase):
    def test_long_plan_with_large_sequences_completes(self):
        steps = [{"description": f"调研阶段 {i + 1}"} for i in range(15)]
        scripted: List[Any] = [turn("计划如下。", goal_steps=steps)]
        for i in range(14):
            scripted.append(turn(f"阶段 {i + 1} 检索。", operations=[rop(f"r{i}", "uniprot.resolve", {"q": f"P{i:04d}"})]))
            scripted.append(turn("已获得数据。"))
        scripted.append(turn("阶段 15 检索。", operations=[rop("r14", "uniprot.resolve", {"q": "P0014"})]))
        scripted.append(turn("全部完成。"))
        scripted.append("15 步调研全部完成,共解析 15 条序列。")

        assistant, session = make_assistant(scripted, max_rounds=20)
        big = "M" * 4000
        inject_read_skill(assistant, "uniprot.resolve", lambda a: {
            "source": "uniprot", "accession": a.get("q"), "sequence": big, "proteinName": "X" * 40})
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="15 步大调研")
        # Before compaction this raised ValueError("Copilot context is too large ... 65k chars").
        self.assertEqual(result["state"], "complete")
        self.assertEqual(result["content"], "15 步调研全部完成,共解析 15 条序列。")

    def test_requests_stay_under_the_hard_cap(self):
        steps = [{"description": f"阶段 {i + 1}"} for i in range(15)]
        scripted: List[Any] = [turn("计划。", goal_steps=steps)]
        for i in range(14):
            scripted.append(turn(f"检索 {i}。", operations=[rop(f"r{i}", "uniprot.resolve", {"q": f"P{i}"})]))
            scripted.append(turn("完成。"))
        scripted.append(turn("检索 15。", operations=[rop("r14", "uniprot.resolve", {"q": "P14"})]))
        scripted.append(turn("结束。"))
        assistant, session = make_assistant(scripted, max_rounds=20)
        inject_read_skill(assistant, "uniprot.resolve", lambda a: {
            "source": "uniprot", "accession": a.get("q"), "sequence": "M" * 4000})
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="大调研")
        self.assertEqual(result["state"], "complete")
        for request in session.requests:
            total = sum(len(str(m.get("content") or "")) for m in request["json"]["messages"])
            self.assertLessEqual(total, 64000, "every outbound request must fit the hard context cap")

    def test_compaction_elides_history_but_keeps_ledger(self):
        # When compaction fires, elided feedback rounds are replaced by a pointer, and the
        # latest feedback (kept verbatim) carries the observation ledger with every value.
        from management_api.copilot import CONTEXT_SOFT_LIMIT_CHARS
        messages = [{"role": "system", "content": "P" * 20000}] + [
            {"role": "system", "content": f"feedback {i} " + "F" * 12000} for i in range(4)
        ]
        assistant, _ = make_assistant([])
        compacted = assistant._compact_for_send(messages)
        total = sum(len(str(m.get("content") or "")) for m in compacted)
        self.assertLess(total, CONTEXT_SOFT_LIMIT_CHARS)
        self.assertTrue(any("elided" in str(m.get("content")) for m in compacted))
        # The most recent feedback survives verbatim; the opening prompt survives.
        self.assertEqual(compacted[0]["content"], "P" * 20000)
        self.assertTrue(any(str(m.get("content")).startswith("feedback 3") for m in compacted))

    def test_ledger_lists_every_observation_compactly(self):
        observations = {
            "a1": {"ok": True, "skill": "chembl.bioactivity", "values": [
                {"results": [{"target": "COX1", "activityType": "IC50", "value": 5.0, "units": "nM"}]}]},
            "a2": {"ok": True, "skill": "uniprot.resolve", "values": [
                {"accession": "P00533", "sequence": "M" * 500}]},
        }
        ledger = _observation_ledger(observations)
        self.assertIn("a1", ledger)
        self.assertIn("target=COX1", ledger)
        self.assertIn("accession=P00533", ledger)
        # Long fields are capped tight in the ledger.
        self.assertLess(len(ledger), 500)


# --------------------------------------------------------------------------- #
# 5. Redundant-call audit
# --------------------------------------------------------------------------- #


class RedundantCallAuditTests(unittest.TestCase):
    def _assistant_with_skill(self, handler: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Tuple[CopilotAssistant, SequenceModelSession]:
        assistant, session = make_assistant([])
        inject_read_skill(assistant, "chembl.bioactivity", handler)
        return assistant, session

    def test_exact_repeat_of_success_is_rejected(self):
        assistant, _ = self._assistant_with_skill(lambda a: {
            "source": "chembl", "results": [{"target": "COX1", "value": 5.0, "units": "nM"}]})
        observations = {
            "first": {
                "ok": True, "skill": "chembl.bioactivity",
                "items": [{"arguments": {"q": "aspirin"}, "ok": True}],
                "values": [{"results": [{"target": "COX1", "value": 5.0, "units": "nM"}]}],
            }
        }
        audit = assistant.skill_harness.audit_plan(
            turn("再查一次。", operations=[rop("second", "chembl.bioactivity", {"q": "aspirin"})]),
            assistant.skill_harness.definitions(), observations=observations)
        self.assertTrue(any("already succeeded" in issue for issue in audit.issues))

    def test_repeat_consuming_observation_via_reference_is_accepted(self):
        assistant, _ = self._assistant_with_skill(lambda a: {"source": "chembl", "results": [{"target": "COX1"}]})
        observations = {
            "first": {
                "ok": True, "skill": "chembl.bioactivity",
                "items": [{"arguments": {"q": "aspirin"}, "ok": True}],
                "values": [{"results": [{"target": "COX1"}]}],
            }
        }
        audit = assistant.skill_harness.audit_plan(
            turn(
                "消费已有结果。",
                operations=[rop("second", "chembl.bioactivity",
                                {"q": {"$fromObservation": "first", "field": "target", "index": 0}},
                                depends=["first"])]),
            assistant.skill_harness.definitions(), observations=observations)
        self.assertEqual(audit.issues, ())

    def test_retry_of_failed_call_is_not_redundant(self):
        assistant, _ = self._assistant_with_skill(lambda a: {"source": "chembl", "results": [{"target": "COX1"}]})
        observations = {
            "first": {
                "ok": False, "skill": "chembl.bioactivity",
                "items": [{"arguments": {"q": "aspirin"}, "ok": False, "error": "HTTP 503"}],
                "values": [], "errors": [{"index": 0, "error": "HTTP 503"}],
            }
        }
        audit = assistant.skill_harness.audit_plan(
            turn("源可能恢复了,重试。", operations=[rop("second", "chembl.bioactivity", {"q": "aspirin"})]),
            assistant.skill_harness.definitions(), observations=observations)
        self.assertFalse(any("already succeeded" in issue for issue in audit.issues))
        self.assertEqual(audit.state, "continue")


# --------------------------------------------------------------------------- #
# 6. Outline mid-round grounding exemption
# --------------------------------------------------------------------------- #


class OutlineMidroundGroundingTests(unittest.TestCase):
    def test_mid_outline_transition_message_is_not_grounding_rejected(self):
        # Exactly one record retrieved; the step's transition message names nothing. Mid-outline
        # this used to be rejected (the model burned rounds re-emitting) — now it advances.
        steps = [{"description": "查蛋白"}, {"description": "总结"}]
        assistant, session = make_assistant([
            turn("计划。", goal_steps=steps),
            turn("检索。", operations=[rop("r1", "uniprot.resolve", {"q": "P00533"})]),
            turn("数据已返回。"),  # transition, 1 record, names nothing
            turn("已解析 EGFR (P00533),序列 300 aa。"),
            "完整总结:EGFR (P00533) 已解析。",
        ])
        inject_read_skill(assistant, "uniprot.resolve", lambda a: {
            "source": "uniprot", "accession": "P00533", "sequence": "MSEQ" * 75})
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="查 EGFR 序列然后总结")
        self.assertEqual(result["state"], "complete")
        events = [step["event"] for step in result["trace"]]
        self.assertNotIn("audit_rejected", events)


# --------------------------------------------------------------------------- #
# 7. New compute unit operations
# --------------------------------------------------------------------------- #


class ComputeSkillUpgradeTests(unittest.TestCase):
    def test_convert_units_nm_to_um(self):
        record = convert_units({"value": 5.0, "from": "nM", "to": "µM"})["results"][0]
        self.assertAlmostEqual(record["result"], 0.005, places=6)
        self.assertIn("5 nM", record["text"])
        self.assertIn("0.005", record["text"])

    def test_convert_units_um_alias_matches_micro_sign(self):
        self.assertAlmostEqual(
            convert_units({"value": 2.0, "from": "uM", "to": "nM"})["results"][0]["result"], 2000.0)

    def test_convert_units_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            convert_units({"value": 0, "from": "nM", "to": "µM"})
        with self.assertRaises(ValueError):
            convert_units({"value": 1.0, "from": "nM", "to": "mol/L"})

    def test_sequence_stats_reports_length_weight_composition(self):
        record = sequence_stats({"sequence": "M" * 50 + "W" * 50})["results"][0]
        self.assertEqual(record["length"], 100)
        self.assertGreater(record["molecularWeightKDa"], 15.0)
        self.assertIn("W(", record["mostCommonResidues"])
        # W is aromatic: 50%.
        self.assertAlmostEqual(record["aromaticPercent"], 50.0, places=1)

    def test_sequence_stats_rejects_short_or_nonprotein(self):
        with self.assertRaises(ValueError):
            sequence_stats({"sequence": "MSEQ"})
        with self.assertRaises(ValueError):
            sequence_stats({"sequence": "12345678901"})

    def test_sequence_stats_consumes_observation_reference_in_loop(self):
        # End-to-end: resolve a sequence, then feed it to compute.sequence_stats via
        # $fromObservation — the grounded chain a "分子量多少" question should drive.
        assistant, session = make_assistant([
            turn("解析序列。", operations=[rop("r1", "uniprot.resolve", {"q": "P00533"})]),
            turn(
                "统计分子量。",
                operations=[rop(
                    "s1", "compute.sequence_stats",
                    {"sequence": {"$fromObservation": "r1", "field": "sequence", "index": 0}},
                    depends=["r1"])]),
            turn("EGFR (P00533) 分子量 134 kDa。"),
            "完整总结:EGFR (P00533) 共 300 残基,分子量约 134 kDa。",
        ])
        inject_read_skill(assistant, "uniprot.resolve", lambda a: {
            "source": "uniprot", "accession": "P00533", "sequence": "M" * 300})
        from management_api.copilot_skills.compute_skills import register_compute_skills
        # Registry is the single source of truth: registering on it is enough for both
        # execution and audit (the catalog is derived live).
        register_compute_skills(assistant.skill_harness.skills)
        result = assistant.plan_turn(
            context_type="task_detail", context_payload=dict(SIMPLE_CTX),
            user_id="u", username="alice", content="EGFR 的分子量是多少")
        self.assertEqual(result["state"], "complete")
        self.assertIn("134", result["content"])


if __name__ == "__main__":
    unittest.main()
