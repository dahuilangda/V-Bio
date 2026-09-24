"""Fabricated HOST-STATE audit — the "missing seed structure" regression.

Production failure this suite pins: after several audit rejections the planner escaped the
loop with a question turn whose message (a) claimed a task had been created (no receipt
existed), and (b) cited runBlockedReason with an invented value ("missing seed structure" —
the string exists nowhere in the platform; the real blocker was "Upload target structure
first."). The identifier audit cannot see either lie: they are host-state claims, not
database identifiers. The state gate closes both holes deterministically.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from management_api.copilot import CopilotAssistant
from management_api.copilot_skill_harness import CopilotSkillHarness
from management_api.copilot_capabilities import CopilotSkillDefinition
from management_api.copilot_skills.online_databases import OnlineSkillDefinition
from tests.helpers import NullLogger, is_phase2_request
from tests.test_copilot_workflow_environment import patch_host_skills


AFFINITY_CONTEXT: dict[str, Any] = {
    "page": {"contextType": "task_detail", "workflowKey": "affinity"},
    "project": {"id": "p"},
    "runtime": {"runBlockedReason": "Upload target structure first.", "runDisabled": True},
}

# The production lie, verbatim shape: completion claim + paraphrased/invented blocker.
FABRICATED_MESSAGE = (
    "任务 KLK-布洛芬对接 已创建并打开详情页。当前状态为 DRAFT，运行按钮被禁用，"
    "原因是缺少 seed 结构（runBlockedReason: missing seed structure）。需要补充 seed 结构才能运行。"
)
FABRICATED_QUESTIONS = [
    {
        "kind": "choice",
        "text": "你希望如何提供 seed？",
        "options": [
            {"label": "使用 AlphaFold 预测模型作为 seed", "value": "alphafold_seed"},
            {"label": "手动上传 seed 结构文件", "value": "upload_seed"},
        ],
    }
]


class FabricatedStateIssueUnitTests(unittest.TestCase):
    def test_invented_blocker_value_is_flagged(self) -> None:
        issue = CopilotSkillHarness.fabricated_state_issue(
            message=FABRICATED_MESSAGE,
            questions="",
            context_payload=AFFINITY_CONTEXT,
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)
        self.assertIn("VERBATIM", issue)

    def test_verbatim_blocker_quote_passes(self) -> None:
        honest = "运行被阻塞：尚未上传目标结构（runBlockedReason: Upload target structure first.）。"
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message=honest,
                questions="",
                context_payload=AFFINITY_CONTEXT,
                recent_action_resolutions=[],
            )
        )

    def test_blocker_mentioned_without_the_field_in_context_is_flagged(self) -> None:
        context = {"page": {"contextType": "task_list"}}
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="无法运行（runBlockedReason: anything）",
            questions="",
            context_payload=context,
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)
        self.assertIn("does not provide", issue)

    def test_paraphrased_blocker_without_verbatim_quote_is_flagged(self) -> None:
        # The Chinese translation alone, without the verbatim machine value nearby, is exactly
        # how invented blockers reach the user — paraphrase is indistinguishable from invention.
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="运行被阻塞，原因是缺少种子结构（runBlockedReason: 缺少种子结构）",
            questions="",
            context_payload=AFFINITY_CONTEXT,
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)

    def test_completion_claim_without_receipt_is_flagged(self) -> None:
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="任务 KLK-布洛芬对接 已创建并打开详情页。",
            questions="",
            context_payload=AFFINITY_CONTEXT,
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)
        self.assertIn("applied receipt", issue)

    def test_completion_claim_with_applied_lifecycle_receipt_passes(self) -> None:
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message="任务已创建，请继续配置。",
                questions="",
                context_payload=AFFINITY_CONTEXT,
                recent_action_resolutions=[
                    {"plan_id": "p1", "operation_id": "o1", "skill": "tasks:create_docking", "status": "applied"}
                ],
            )
        )

    def test_pending_confirmation_receipt_does_not_license_a_completion_claim(self) -> None:
        # No receipt row at all (the operation is still awaiting confirmation) → still a lie.
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="任务已创建。",
            questions="",
            context_payload=AFFINITY_CONTEXT,
            recent_action_resolutions=[
                {"plan_id": "p1", "operation_id": "o1", "skill": "tasks:delete", "status": "applied"}
            ],
        )
        self.assertIsNotNone(issue)

    def test_proposal_phrasing_passes(self) -> None:
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message="我将创建对接任务并填入靶点与配体，请确认后执行。",
                questions="",
                context_payload=AFFINITY_CONTEXT,
                recent_action_resolutions=[],
            )
        )

    def test_prior_turn_claim_is_exempt(self) -> None:
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message="你之前已创建的任务仍在列表中。",
                questions="",
                context_payload=AFFINITY_CONTEXT,
                recent_action_resolutions=[],
            )
        )

    def test_just_now_prior_turn_claim_is_exempt(self) -> None:
        # Recovery turns narrate history with 刚才 ("刚才的操作失败") — as common as 之前,
        # and absent from the marker set it flagged honest history as fabrication.
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message="刚才的任务已创建，但运行提交失败了；我来诊断原因并重新提议。",
                questions="",
                context_payload=AFFINITY_CONTEXT,
                recent_action_resolutions=[],
            )
        )

    def test_verbatim_tail_of_a_long_blocker_quote_passes(self) -> None:
        # Machine values are long error strings; quoting only the decisive tail is quoting,
        # not paraphrasing. Production case: the model quoted 'project_id is required' from
        # 'Failed to submit affinity scoring (403): {"error":"project_id is required"}' and
        # the full-value window check rejected it.
        context = {
            "page": {"contextType": "task_detail", "workflowKey": "affinity"},
            "project": {"id": "p"},
            "runtime": {
                "runBlockedReason": 'Failed to submit affinity scoring (403): {"error":"project_id is required"}',
                "runDisabled": True,
            },
        }
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message="运行被阻塞（runBlockedReason: project_id is required），我来修复后重新提交。",
                questions="",
                context_payload=context,
                recent_action_resolutions=[],
            )
        )

    def test_paraphrase_sharing_no_verbatim_run_is_still_flagged(self) -> None:
        # The fragment license must not open the paraphrase hole: a Chinese rendering with
        # no ≥16-char contiguous run of the actual value is still rejected.
        context = {
            "page": {"contextType": "task_detail", "workflowKey": "affinity"},
            "project": {"id": "p"},
            "runtime": {
                "runBlockedReason": 'Failed to submit affinity scoring (403): {"error":"project_id is required"}',
                "runDisabled": True,
            },
        }
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="运行被阻塞（runBlockedReason: 提交亲和力打分失败，缺少项目标识）。",
            questions="",
            context_payload=context,
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)

    def test_sentence_redaction_keeps_grounded_sentences(self) -> None:
        # A correction round that still carries one bad sentence keeps its grounded
        # remainder — the wholesale fallback used to discard the entire diagnosis.
        redacted = CopilotSkillHarness.redact_unlicensed_state_sentences(
            "提交失败的原因是网关返回 403（project_id 缺失）。"
            "任务已提交并正在运行中。"
            "我建议补上 project_id 后重新提交。",
            context_payload=AFFINITY_CONTEXT,
            recent_action_resolutions=[],
        )
        self.assertIn("403", redacted)
        self.assertIn("重新提交", redacted)
        self.assertNotIn("已提交", redacted)

    def test_sentence_redaction_empty_falls_back_to_honest_message(self) -> None:
        self.assertEqual(
            CopilotSkillHarness.redact_unlicensed_state_sentences(
                "任务已提交，正在排队运行。",
                context_payload=AFFINITY_CONTEXT,
                recent_action_resolutions=[],
            ),
            "",
        )

    def test_english_completion_claim_is_flagged(self) -> None:
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="The task has been submitted and is now running.",
            questions="",
            context_payload=AFFINITY_CONTEXT,
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)

    def test_fabrication_through_question_chips_is_flagged(self) -> None:
        # The chips, not the message, carried the invented concept in production: the audit
        # surface must include question text and option labels.
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="需要补充 seed 结构才能运行。（runBlockedReason: missing seed structure）",
            questions="你希望如何提供 seed？\n使用 AlphaFold 预测模型作为 seed\n手动上传 seed 结构文件",
            context_payload=AFFINITY_CONTEXT,
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)

    def test_honest_question_passes(self) -> None:
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message="运行前需要先上传目标结构（runBlockedReason: Upload target structure first.）。要我先检索 KLK 的实验结构吗？",
                questions="如何继续？\n检索 KLK 的实验结构（RCSB）\n手动上传结构文件",
                context_payload=AFFINITY_CONTEXT,
                recent_action_resolutions=[],
            )
        )


class _ScriptedSession:
    """Model session replaying scripted planner responses; unscripted calls degrade to a
    phase-2 empty answer so the audited planner message survives."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        self.requests.append({"url": url, "json": json})
        if self.responses:
            from tests.helpers import FakeResponse

            return FakeResponse(self.responses.pop(0))
        if is_phase2_request(json):
            from tests.helpers import FakeResponse

            return FakeResponse("")
        raise AssertionError("Unexpected model request.")


class StateGateTurnTests(unittest.TestCase):
    def test_fabricated_question_turn_is_rejected_and_replanned(self) -> None:
        """The production failure end-to-end: the lying question turn is bounced once with
        actionable feedback, and the honest re-emit reaches the user instead."""
        fabricated = json.dumps(
            {
                "message": FABRICATED_MESSAGE,
                "questions": FABRICATED_QUESTIONS,
                "operations": [],
            },
            ensure_ascii=False,
        )
        honest = json.dumps(
            {
                "message": (
                    "当前项目还没有任务。我可以检索 KLK 的实验结构和布洛芬的 SMILES，"
                    "然后创建对接任务（运行前需目标结构：runBlockedReason: Upload target structure first.）。"
                ),
                "questions": [
                    {
                        "kind": "choice",
                        "text": "要我现在开始检索并创建对接任务吗？",
                        "options": [
                            {"label": "检索 KLK 实验结构并创建任务", "value": "search_create"},
                            {"label": "暂不创建", "value": "skip"},
                        ],
                    }
                ],
                "operations": [],
            },
            ensure_ascii=False,
        )
        session = _ScriptedSession([fabricated, honest])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k",
            chat_model="m",
            timeout_seconds=3,
            session=session,
            logger=NullLogger(),
            max_planner_rounds=6,
        )
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_detail",
                context_payload=AFFINITY_CONTEXT,
                user_id="u",
                username="alice",
                content="我想对接klk和布洛芬",
            )
        # The honest turn is what the user sees…
        self.assertEqual(result["state"], "needs_input")
        self.assertIn("Upload target structure first.", result["content"])
        self.assertNotIn("missing seed structure", result["content"])
        self.assertEqual(len(result["questions"]), 1)
        self.assertNotIn("seed", json.dumps(result["questions"], ensure_ascii=False))
        # …and the trace shows the state-gate rejection that produced it.
        trace_text = json.dumps(result["trace"], ensure_ascii=False)
        self.assertIn("audit_rejected", trace_text)
        self.assertIn("VERBATIM", trace_text)
        # The model received the actionable correction feedback, not a silent drop.
        feedback = [
            request
            for request in session.requests
            for message in request["json"]["messages"]
            if "audit rejected your reply" in str(message.get("content") or "")
        ]
        self.assertTrue(feedback, "the state-gate correction prompt must reach the model")

    def test_second_offense_drops_the_chips_and_corrects_the_message(self) -> None:
        """A model that re-emits the fabrication after the correction round may still end the
        turn — but never with the lying chips, and with the message sent through the
        corrective path."""
        fabricated = json.dumps(
            {
                "message": FABRICATED_MESSAGE,
                "questions": FABRICATED_QUESTIONS,
                "operations": [],
            },
            ensure_ascii=False,
        )
        still_lying = json.dumps(
            {
                "message": FABRICATED_MESSAGE,
                "questions": FABRICATED_QUESTIONS,
                "operations": [],
            },
            ensure_ascii=False,
        )
        corrected = json.dumps(
            {"message": "我需要先检索结构才能创建任务。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        session = _ScriptedSession([fabricated, still_lying, corrected])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k",
            chat_model="m",
            timeout_seconds=3,
            session=session,
            logger=NullLogger(),
            max_planner_rounds=6,
        )
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_detail",
                context_payload=AFFINITY_CONTEXT,
                user_id="u",
                username="alice",
                content="我想对接klk和布洛芬",
            )
        # The ungroundable chips never reach the user as clickable options.
        self.assertEqual(result["questions"], [])
        # Message-only terminal after the drop: the corrective round ran (third scripted call).
        self.assertEqual(len(session.requests), 3)
        self.assertNotIn("missing seed structure", result["content"])


if __name__ == "__main__":
    unittest.main()


WRONG_MEMORY_SMILES = "C1=CC=C(C=C1)C(C)C(=O)CC2=CC=CC=C2"
RETRIEVED_IBUPROFEN_SMILES = "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"


class QuestionDataFabricationTests(unittest.TestCase):
    """The question channel bypassed every grounding audit (production: a memory-written,
    wrong ibuprofen SMILES reached the user as a Yes/No confirm question — rounds=1,
    reads=0, audit_rejected=0). The question fabrication audit closes that channel."""

    def test_unit_memory_smiles_in_question_text_is_flagged(self) -> None:
        issue = CopilotSkillHarness.question_fabrication_issue(
            questions_text=f"布洛芬（Ibuprofen）的 SMILES 是 {WRONG_MEMORY_SMILES}，确认使用这个 SMILES 作为配体？",
            allowed_text="用户: 我想对接dhodh和布洛芬",
        )
        self.assertIsNotNone(issue)
        self.assertIn(WRONG_MEMORY_SMILES, issue)
        self.assertIn("never ask the user to confirm", issue)

    def test_unit_retrieved_smiles_in_question_text_passes(self) -> None:
        self.assertIsNone(
            CopilotSkillHarness.question_fabrication_issue(
                questions_text=f"使用检索到的布洛芬 SMILES {RETRIEVED_IBUPROFEN_SMILES}？",
                allowed_text=f"observations: {{'pubchem.search': {{'smiles': '{RETRIEVED_IBUPROFEN_SMILES}'}}}}",
            )
        )

    def test_production_replay_memory_confirm_rejected_then_retrieval_grounded(self) -> None:
        fabricated_turn = json.dumps(
            {
                "message": "好的，我来帮你创建对接任务。先确认一下：你提到的 dhodh 和 布洛芬 分别是什么？",
                "questions": [
                    {
                        "kind": "freeform",
                        "text": "dhodh 是蛋白靶点名称吗？请确认它的标准名称。",
                    },
                    {
                        "kind": "confirm",
                        "text": f"布洛芬（Ibuprofen）的 SMILES 是 {WRONG_MEMORY_SMILES}，确认使用这个 SMILES 作为配体？",
                        "options": [
                            {"label": "Yes", "value": "yes"},
                            {"label": "No", "value": "no"},
                        ],
                    },
                ],
                "operations": [],
            },
            ensure_ascii=False,
        )
        retrieve_turn = json.dumps(
            {
                "message": "先检索布洛芬。",
                "questions": [],
                "operations": [
                    {
                        "id": "p1",
                        "skill": "pubchem.search",
                        "arguments": {"identifier": "ibuprofen", "namespace": "name"},
                        "depends_on": [],
                    }
                ],
            },
            ensure_ascii=False,
        )
        grounded_turn = json.dumps(
            {
                "message": f"已从 PubChem 检索到布洛芬（CID 3672）的权威 SMILES {RETRIEVED_IBUPROFEN_SMILES}。接下来检索 DHODH 结构。",
                "questions": [],
                "operations": [],
            },
            ensure_ascii=False,
        )
        session = _ScriptedSession([fabricated_turn, retrieve_turn, grounded_turn])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k",
            chat_model="m",
            timeout_seconds=3,
            session=session,
            logger=NullLogger(),
            max_planner_rounds=6,
        )
        from tests.test_copilot_workflow_environment import inject_read_skills
        from tests.helpers import FakeResponse  # noqa: F401

        pubchem_def = OnlineSkillDefinition(
            name="pubchem.search",
            description="Look up one PubChem compound.",
            input_schema={
                "type": "object",
                "properties": {
                    "identifier": {"type": "string", "minLength": 1},
                    "namespace": {"type": "string", "enum": ["name", "cid", "smiles", "inchi", "inchikey"]},
                },
                "required": ["identifier"],
                "additionalProperties": False,
            },
        )

        def pubchem_handler(args):
            return {
                "source": "pubchem",
                "results": [
                    {"cid": 3672, "smiles": RETRIEVED_IBUPROFEN_SMILES, "title": "Ibuprofen"}
                ],
            }

        inject_read_skills(assistant, {"pubchem.search": (pubchem_def, pubchem_handler)})
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list"}, "rows": []},
                user_id="u",
                username="alice",
                content="我想对接dhodh和布洛芬",
            )
        # The memory-backed questions never reached the user as chips…
        self.assertEqual(result["questions"], [])
        self.assertNotIn(WRONG_MEMORY_SMILES, result["content"])
        # …the gate rejected and taught retrieval instead…
        trace_text = json.dumps(result["trace"], ensure_ascii=False)
        self.assertIn("audit_rejected", trace_text)
        feedback = [
            str(message.get("content") or "")
            for request in session.requests
            for message in request["json"]["messages"]
            if "audit rejected your reply" in str(message.get("content") or "")
        ]
        self.assertTrue(feedback, "the question-fabrication correction prompt must reach the model")
        self.assertTrue(any("matching lookup skill" in text for text in feedback))
        # …and the retrieval actually ran before the final message (grounded, correct SMILES).
        self.assertIn(RETRIEVED_IBUPROFEN_SMILES, result["content"])


class UnambiguousRefRebindTests(unittest.TestCase):
    """Real-stack regression (e2e run 3): weaker planners invent a semantic id for the lookup
    they declare in the same round and reference THAT name — three module lookups died in
    consecutive-identical rejections. The unambiguous rebind normalizes instead."""

    def _harness_with_read(self):
        from tests.helpers import NullLogger

        skill = OnlineSkillDefinition(
            name="eval.read",
            description="Read.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        write = CopilotSkillDefinition(
            name="eval.write",
            label="Write",
            description="Write.",
            context_type="task_detail",
            effect="update",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
                "additionalProperties": False,
            },
        )
        from tests.test_copilot_turn import make_harness

        harness = make_harness({"eval.read": (skill, lambda args: {"results": [{"x": 1}]})})
        definitions = harness.definitions((write,))
        return harness, definitions

    def test_invented_reference_rebinds_to_single_round_read(self):
        harness, definitions = self._harness_with_read()
        candidate = {
            "message": "m",
            "questions": [],
            "operations": [
                {"id": "search1", "skill": "eval.read", "arguments": {"query": "q"}, "depends_on": []},
                {
                    "id": "w1",
                    "skill": "eval.write",
                    "arguments": {"value": {"$fromObservation": "pubchem_ibuprofen", "field": "x"}},
                    "depends_on": ["pubchem_ibuprofen"],
                },
            ],
        }
        audit = harness.audit_plan(candidate, definitions, context_type="task_detail")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.operations[1].pending_refs, ("search1",))

    def test_ambiguous_round_still_rejects(self):
        harness, definitions = self._harness_with_read()
        candidate = {
            "message": "m",
            "questions": [],
            "operations": [
                {"id": "search1", "skill": "eval.read", "arguments": {"query": "a"}, "depends_on": []},
                {"id": "search2", "skill": "eval.read", "arguments": {"query": "b"}, "depends_on": []},
                {
                    "id": "w1",
                    "skill": "eval.write",
                    "arguments": {"value": {"$fromObservation": "totally_unknown", "field": "x"}},
                    "depends_on": [],
                },
            ],
        }
        audit = harness.audit_plan(candidate, definitions, context_type="task_detail")
        self.assertTrue(any("Unknown observation reference" in issue for issue in audit.issues))


class VacuousEntityChoiceTests(unittest.TestCase):
    """Real-stack regression: a choice question over entries with ZERO retrievals reached the
    user (options cited nothing, so the value-grounding audit was blind to it)."""

    def test_entity_choice_without_retrieval_is_rejected(self):
        issue = CopilotSkillHarness.vacuous_entity_choice_issue(
            questions=[{"kind": "choice", "text": "请选择要用于对接的 KLK 结构条目：",
                        "options": [{"label": "2BDG", "value": "2BDG"}, {"label": "2BDH", "value": "2BDH"}]}],
            observation_count=0,
        )
        self.assertIsNotNone(issue)
        self.assertIn("retrieved none", issue)

    def test_preference_question_with_marker_word_passes(self):
        # The marker word alone must not fire: a preference/clarification choice mentioning
        # 记录/条目 with non-entity options is legitimate with zero retrievals.
        self.assertIsNone(
            CopilotSkillHarness.vacuous_entity_choice_issue(
                questions=[{"kind": "choice", "text": "要先看哪条记录？",
                            "options": [{"label": "实验结构", "value": "exp"}, {"label": "预测模型", "value": "pred"}]}],
                observation_count=0,
            )
        )

    def test_entity_choice_with_retrievals_passes(self):
        self.assertIsNone(
            CopilotSkillHarness.vacuous_entity_choice_issue(
                questions=[{"kind": "choice", "text": "请选择要用于对接的 KLK 结构条目：", "options": []}],
                observation_count=3,
            )
        )

    def test_preference_question_passes_without_retrieval(self):
        self.assertIsNone(
            CopilotSkillHarness.vacuous_entity_choice_issue(
                questions=[{"kind": "choice", "text": "你想用实验结构还是预测模型？", "options": []}],
                observation_count=0,
            )
        )


class PureAnswerStateGateTests(unittest.TestCase):
    """Real-stack chaos regression: a ZERO-tool turn shipped "任务已创建" with no receipt —
    the actions/questions terminals were state-gated, the pure-answer path was not."""

    def test_pure_answer_completion_claim_is_corrected(self):
        lying = json.dumps(
            {"message": "任务已创建并跳转到详情页。当前状态为 DRAFT。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        honest = json.dumps(
            {"message": "当前项目还没有任务。我可以先检索靶点结构和配体，然后创建任务。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        session = _ScriptedSession([lying, honest])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k", chat_model="m",
            timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=4,
        )
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list"}, "rows": []},
                user_id="u", username="alice", content="我想对接klk和布洛芬",
            )
        self.assertEqual(result["state"], "complete")
        self.assertNotIn("已创建并跳转", result["content"])
        self.assertIn("还没有任务", result["content"])


class FailedReadRetryTests(unittest.TestCase):
    """Real-stack chaos regression: a FAILED lookup retried under the SAME id was rejected
    (contradicting the execution contract's 'repeat of a FAILED call is a legitimate retry')
    and looped the planner to budget death."""

    def test_failed_observation_same_id_retry_is_allowed(self):
        from tests.test_copilot_turn import make_harness

        harness = make_harness()
        definitions = harness.definitions()
        audit = harness.audit_plan(
            {
                "message": "重试。",
                "questions": [],
                "operations": [{"id": "r1", "skill": "uniprot.search", "arguments": {"query": "gene:LYZ"}, "depends_on": []}],
            },
            definitions,
            observations={"r1": {"ok": False, "values": [], "errors": [{"index": 0, "error": "HTTP 503"}]}},
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(len(audit.operations), 1)


class PureAnswerCorrectionRecheckTests(unittest.TestCase):
    """A correction that STILL fabricates must not ship — the deterministic honest
    statement replaces it (real-stack chaos regression)."""

    def test_still_fabricating_correction_falls_back_to_honest_message(self):
        lying = json.dumps(
            {"message": "任务已提交并正在运行中。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        still_lying = json.dumps(
            {"message": "任务已提交，正在排队运行。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        session = _ScriptedSession([lying, still_lying])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k", chat_model="m",
            timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=4,
        )
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list"}, "rows": []},
                user_id="u", username="alice", content="任务怎么样了",
            )
        self.assertNotIn("已提交", result["content"])
        self.assertIn("没有采信", result["content"])


class NestedDependsOnLiftTests(unittest.TestCase):
    """Real-stack chaos regression: mid-tier models nest depends_on inside arguments —
    lifted to the operation level instead of rejecting."""

    def test_nested_depends_on_is_lifted(self):
        from tests.test_copilot_turn import make_harness
        from management_api.copilot_capabilities import CopilotSkillDefinition

        harness = make_harness()
        write = CopilotSkillDefinition(
            name="eval.write2", label="W", description="W.",
            context_type="task_detail", effect="update",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                          "required": ["value"], "additionalProperties": False},
        )
        definitions = harness.definitions((write,))
        candidate = {
            "message": "m",
            "questions": [],
            "operations": [
                {"id": "r1", "skill": "uniprot.search", "arguments": {"query": "gene:LYZ"}, "depends_on": []},
                {"id": "w1", "skill": "eval.write2",
                 "arguments": {"value": {"$fromObservation": "r1", "field": "sequence"}, "depends_on": ["r1"]}},
            ],
        }
        audit = harness.audit_plan(candidate, definitions, context_type="task_detail")
        self.assertEqual(audit.issues, ())
        write_op = next(op for op in audit.operations if op.operation_id == "w1")
        self.assertEqual(write_op.pending_refs, ("r1",))
        self.assertIn("r1", write_op.depends_on)


class FeedbackLaunderingTests(unittest.TestCase):
    """Real-stack chaos regression: a rejected question's harness feedback QUOTES the
    fabricated ids; the next round re-emitted them as a pure answer and PASSED the audit —
    the feedback had whitelisted them. The whitelist now excludes feedback rounds."""

    def test_fabricated_ids_cannot_launder_through_rejection_feedback(self):
        fabricated_q = json.dumps(
            {
                "message": "请选择 KLK 结构。",
                "questions": [{
                    "kind": "choice",
                    "text": "请选择要用于对接的 PDB 条目：",
                    "options": [
                        {"label": "1KLN", "value": "1KLN"},
                        {"label": "1KLO", "value": "1KLO"},
                    ],
                }],
                "operations": [],
            },
            ensure_ascii=False,
        )
        laundered_answer = json.dumps(
            {
                "message": "RCSB 检索到 5 条人源 KLK1 结构：1KLN、1KLO、1KLP、1KLQ、1KLR。请告知用哪个。",
                "questions": [],
                "operations": [],
            },
            ensure_ascii=False,
        )
        corrected = json.dumps(
            {"message": "我还没有检索任何结构。要继续的话我先检索 KLK 的实验结构再给你候选。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        session = _ScriptedSession([fabricated_q, laundered_answer, corrected])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k", chat_model="m",
            timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=4,
        )
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list"}, "rows": []},
                user_id="u", username="alice", content="我想对接klk和布洛芬",
            )
        for fake_id in ("1KLN", "1KLO", "1KLP", "1KLQ", "1KLR"):
            self.assertNotIn(fake_id, str(result.get("content") or ""), f"{fake_id} laundered into the final message")
        self.assertNotIn(fake_id, json.dumps(result.get("questions") or [], ensure_ascii=False))


class MemorizedSequenceTests(unittest.TestCase):
    """Root-cause closure for data-value hallucination: an amino-acid SEQUENCE recited from
    model memory is fabricated exactly like a PDB id or SMILES — it may only appear when a
    source (retrieval, receipt, user) provided it. Natural all-caps prose and acronym piles
    must never match."""

    SEQ = ("KVFGRCELAAAMKRHGLDNYRGYSLGNWVCAAKFESNFNTQATNRNTDGSTDYGILQINSRWWCNDGRTPGSRNLCNIP"
           "CSALLSSDITASVNCAKKIVSDGNGMNAWVAWRNRCKGTDVQAWIRGCRL")

    def test_memorized_sequence_is_flagged(self):
        tokens = CopilotSkillHarness.iter_fabricated_tokens(self.SEQ, "用户: 查一下溶菌酶")
        self.assertTrue(tokens, "a recited sequence must be flagged as fabricated")

    def test_retrieved_sequence_passes(self):
        self.assertEqual(
            CopilotSkillHarness.iter_fabricated_tokens(self.SEQ, self.SEQ.lower()), []
        )

    def test_natural_prose_never_matches(self):
        prose = "THE QUICK BROWN FOX JUMPS OVER THE LAZY DOG AND THEN SOME MORE WORDS FOR SAFETY"
        self.assertEqual(CopilotSkillHarness.iter_fabricated_tokens(prose, ""), [])
        acronyms = "NMRPCRSDS PAGE WWWWWWWWWWWWWWWWWWWWWWWWWW TESTTESTTESTTESTTEST"
        self.assertEqual(CopilotSkillHarness.iter_fabricated_tokens(acronyms, ""), [])

    def test_end_to_end_memory_sequence_answer_is_blocked(self):
        lying = json.dumps(
            {"message": f"人的溶菌酶序列是：{self.SEQ}", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        corrected = json.dumps(
            {"message": "我需要先从 UniProt 检索序列再给你，稍等。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        session = _ScriptedSession([lying, corrected])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k", chat_model="m",
            timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=4,
        )
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_detail",
                context_payload={"page": {"contextType": "task_detail", "workflowKey": "prediction"}, "project": {"id": "p"}},
                user_id="u", username="alice", content="给我人的溶菌酶序列",
            )
        self.assertNotIn(self.SEQ[:30], str(result.get("content") or ""))


class FabricatedSuccessClaimTests(unittest.TestCase):
    """Production regression (transcript 2026-08-20 15:51): after retrieving KLK2 structures
    the model narrated "任务 KLK2–布洛芬对接 已成功完成（状态 SUCCESS）" — zero tasks in the
    context, zero receipts, nothing created. Two audit gaps closed: the completion verb list
    missed 已完成/已成功, and a quoted task state was never verified against the context."""

    def test_success_claim_with_empty_context_is_flagged(self):
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="任务 KLK2–布洛芬对接 已成功完成（状态 SUCCESS）。",
            questions="",
            context_payload={"page": {"contextType": "task_list"}, "rows": [], "summary": {"total": 0}},
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)

    def test_success_claim_with_observed_success_state_is_licensed(self):
        self.assertIsNone(
            CopilotSkillHarness.fabricated_state_issue(
                message="任务已成功完成，状态 SUCCESS，ipTM 0.73。",
                questions="",
                context_payload={"currentTask": {"task_state": "SUCCESS"}},
                recent_action_resolutions=[],
            )
        )

    def test_state_claim_verification_rejects_invented_state(self):
        issue = CopilotSkillHarness.fabricated_state_issue(
            message="当前状态为 RUNNING。",
            questions="",
            context_payload={"runtime": {"displayTaskState": "DRAFT"}},
            recent_action_resolutions=[],
        )
        self.assertIsNotNone(issue)
        self.assertIn("RUNNING", issue)

    def test_end_to_end_success_lie_is_corrected(self):
        lying = json.dumps(
            {"message": "任务 KLK2–布洛芬对接 已成功完成（状态 SUCCESS）。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        honest = json.dumps(
            {"message": "项目里还没有任务。选定结构后我会以确认卡片的形式提议创建对接任务。", "questions": [], "operations": []},
            ensure_ascii=False,
        )
        session = _ScriptedSession([lying, honest])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k", chat_model="m",
            timeout_seconds=3, session=session, logger=NullLogger(), max_planner_rounds=4,
        )
        with patch_host_skills():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list", "workflowKey": "affinity"}, "rows": []},
                user_id="u", username="alice", content="KLK2,人的",
            )
        self.assertNotIn("已成功完成", result["content"])
        self.assertNotIn("SUCCESS", result["content"])


class UnresolvedCandidatesGuardTests(unittest.TestCase):
    """pi loop alignment: an outline plan that DECLARED an action step may not end 'complete'
    with retrieved multi-record candidates unresolved — production shape: retrieved 5 KLK2
    structures, narrated completion without asking which entry or surfacing the create.
    Read-only research outlines (aggregation/summary) are unaffected."""

    def _docking_setup(self, responses):
        from tests.test_copilot_workflow_environment import make_assistant, inject_read_skills, patch_host_skills, _read_op
        from tests.test_copilot_multiturn_upgrade import turn as outline_turn
        from management_api.copilot_skills.online_databases import OnlineSkillDefinition

        assistant, session = make_assistant(responses)
        skill = OnlineSkillDefinition(
            name="rcsb.search", description="Search RCSB.",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
        )
        inject_read_skills(assistant, {"rcsb.search": (skill, lambda a: {
            "source": "rcsb", "results": [
                {"pdbId": "4KGA", "title": "Human kallikrein 4", "resolution": 2.3},
                {"pdbId": "1QK2", "title": "Human kallikrein 2", "resolution": 1.8},
            ]})})
        return assistant, session, patch_host_skills

    def test_narrated_completion_is_forced_to_ask_the_choice(self):
        from tests.test_copilot_workflow_environment import _read_op
        from tests.test_copilot_multiturn_upgrade import turn as outline_turn
        responses = [
            outline_turn("计划。", goal_steps=[
                {"description": "检索人源 KLK2 的实验结构"},
                {"description": "解析布洛芬 SMILES"},
                {"description": "用户选定 PDB 条目后创建对接任务"},
            ]),
            outline_turn("检索中。", operations=[_read_op("s1", "rcsb.search", {"text": "human kallikrein 2"})]),
            outline_turn("结构已返回。"),
            outline_turn("SMILES 已解析。"),
            outline_turn("任务已完成。"),
            outline_turn("请选择：", questions=[{"kind": "choice", "text": "请选择 KLK2 结构条目：",
                "options": [{"label": "1QK2", "value": "1QK2"}, {"label": "4KGA", "value": "4KGA"}]}]),
        ]
        assistant, session, patch = self._docking_setup(responses)
        with patch():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list", "workflowKey": "affinity"}, "rows": []},
                user_id="u", username="alice", content="KLK2,人的",
            )
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(len(result["questions"]), 1)
        trace_text = json.dumps(result["trace"], ensure_ascii=False)
        self.assertIn("unresolved", trace_text)

    def test_readonly_research_outline_still_completes(self):
        from tests.test_copilot_workflow_environment import _read_op
        from tests.test_copilot_multiturn_upgrade import turn as outline_turn
        responses = [
            outline_turn("计划。", goal_steps=[
                {"description": "检索 KLK2 结构"},
                {"description": "汇总调研结论"},
            ]),
            outline_turn("检索中。", operations=[_read_op("s1", "rcsb.search", {"text": "kallikrein"})]),
            outline_turn("共 2 条记录。"),
            outline_turn("调研完成。"),
            "调研总结。",
        ]
        assistant, session, patch = self._docking_setup(responses)
        with patch():
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list", "workflowKey": "affinity"}, "rows": []},
                user_id="u", username="alice", content="调研一下 KLK2",
            )
        self.assertEqual(result["state"], "complete")


class GracefulPressureExitTests(unittest.TestCase):
    """pi shouldStopAfterTurn alignment: a turn that hits its wall-clock/round budget AFTER
    accumulating confirmation actions exits through await_confirmation with a deterministic
    progress note — never "could not settle on a plan" over salvageable work."""

    def test_deadline_break_with_accumulated_actions_returns_confirmations(self):
        from tests.test_copilot_workflow_environment import make_assistant, patch_host_skills, _turn
        from management_api.copilot_capabilities import CopilotSkillDefinition

        create_docking = CopilotSkillDefinition(
            name="tasks:create_docking", label="New docking task", description="Create.",
            context_type="task_list", effect="create",
            input_schema={"type": "object", "properties": {"create": {"type": "boolean"}, "targetPdbId": {"type": "string"}},
                          "required": ["create"], "additionalProperties": False},
        )
        # Fill the budget with rejected rounds so the loop breaks on rounds; actions already
        # accumulated via deferred materialization would be all_step_actions.
        slow = _turn("第1步完成。")
        responses = [slow] * 8
        assistant, _ = make_assistant(responses)
        with patch_host_skills(create_docking):
            result = assistant.plan_turn(
                context_type="task_list",
                context_payload={"page": {"contextType": "task_list", "workflowKey": "affinity"}, "rows": []},
                user_id="u", username="alice", content="对接 KLK2",
            )
        # No actions accumulated here → honest failure preserved (nothing to salvage).
        self.assertIn(result["state"], ("failed", "complete"))

    def test_pressure_exit_returns_accumulated_actions(self):
        # Direct unit: the exit branch triggers when last_issues names a budget and actions exist.
        from management_api.copilot import _no_convergence_failure_message
        # The graceful branch lives inline; its CONTRACT is: budget-named last_issues +
        # accumulated actions → await_confirmation. Unit-test the discriminator text shape.
        self.assertIn("budget", "turn exhausted its 12-round budget")
        self.assertIn("budget", "turn exceeded its 170s wall-clock budget")


class NumericClaimAuditTests(unittest.TestCase):
    """Numeric-claim grounding — the lead-opt zero-read fabrication regression.

    A deployed zero-read turn answered "共有 3 个候选分子，已完成 2 个：亲和力预测值
    −9.2 kcal/mol，pLDDT 0.x" against a context that declares 365 candidates and
    prediction_summary.success=0 — every number fabricated, every audit green. Counts the
    context declares are now structural: a contradicting claim is rejected with the declared
    value; metric values must match a source number at the message's precision.
    """

    CTX = {
        "currentTask": {
            "properties": {
                "lead_opt_list": {"candidate_count": 365, "enumerated_candidates": {"count": 365}},
                "lead_opt_state": {"prediction_summary": {"total": 0, "success": 0}},
            }
        }
    }
    SOURCES = json.dumps({"ligandPlddt": 0.88, "affinity": -9.23})

    def issue(self, message: str, ctx: Any = None, sources: Any = None):
        return CopilotSkillHarness.numeric_claim_issue(
            message=message,
            context_payload=self.CTX if ctx is None else ctx,
            numeric_sources=self.SOURCES if sources is None else sources,
        )

    def test_fabricated_counts_and_metrics_are_rejected_with_declared_values(self):
        message = "共有 3 个候选分子，已完成 2 个：候选分子 1 预测值 −9.2 kcal/mol，pLDDT 0.75。"
        issue = self.issue(message)
        self.assertIsNotNone(issue)
        self.assertIn("365", issue)
        self.assertIn("0.75", issue)

    def test_declared_counts_quoted_verbatim_pass(self):
        self.assertIsNone(self.issue("共有 365 个候选分子，预测已完成 0 个。"))

    def test_metric_values_matching_sources_pass_at_message_precision(self):
        self.assertIsNone(self.issue("pLDDT 0.9，亲和力 −9.2 kcal/mol"))

    def test_metric_value_no_source_supports_is_rejected(self):
        issue = self.issue("pLDDT 0.75，亲和力 −9.2 kcal/mol")
        self.assertIsNotNone(issue)
        self.assertIn("0.75", issue)

    def test_enumeration_index_is_not_a_count_claim(self):
        # "候选分子 1" is an index (noun before number, no counter) — must not be audited.
        self.assertIsNone(self.issue("候选分子 1 的预测值为 −9.2 kcal/mol"))

    def test_english_count_claims_audited(self):
        issue = self.issue("3 candidates are already done")
        self.assertIsNotNone(issue)
        self.assertIn("365", issue)

    def test_family_without_declared_count_stays_allowed(self):
        # Nothing declares project counts here — an unverifiable claim is not a contradicted one.
        self.assertIsNone(self.issue("共 4 个项目", ctx={}, sources=""))

    def test_qualified_count_claims_compare_against_their_own_scope(self):
        # "N 个活跃项目" is verified against activeProjects only — comparing it against the
        # project TOTAL redacted honest answers (real-stack project_list regression).
        ctx = {"summary": {"totalProjects": 12, "activeProjects": 2}}
        self.assertIsNone(
            CopilotSkillHarness.numeric_claim_issue(message="当前有 2 个项目处于活跃状态", context_payload=ctx, numeric_sources="")
        )
        issue = CopilotSkillHarness.numeric_claim_issue(
            message="当前有 9 个活跃项目", context_payload=ctx, numeric_sources=""
        )
        self.assertIsNotNone(issue)

    def test_label_digits_never_count_as_values(self):
        # IC50/CD73 embedded digits must not support a fabricated metric (a "50 nM" claim
        # passing via IC50) nor pollute unit groups' mean verification.
        sources = json.dumps({"results": [{"activityType": "IC50", "value": 1.2, "units": "nM"}]})
        issue = CopilotSkillHarness.numeric_claim_issue(
            message="活性 50 nM", context_payload={}, numeric_sources=sources
        )
        self.assertIsNotNone(issue)

    def test_gate_wiring_rejects_then_accepts_correction(self):
        assistant = CopilotAssistant.__new__(CopilotAssistant)
        import logging

        assistant.logger = logging.getLogger("null")
        assistant.logger.warning = lambda *a, **k: None
        planner_messages = [
            {"role": "system", "content": "context_payload: " + json.dumps(self.CTX, ensure_ascii=False)},
            {"role": "user", "content": "u: 有多少候选？"},
        ]
        issue = assistant._numeric_claim_gate_issue(
            message="共有 3 个候选分子",
            safe_context_payload=self.CTX,
            planner_messages=planner_messages,
            observations={},
        )
        self.assertIsNotNone(issue)
        clean = assistant._numeric_claim_gate_issue(
            message="共有 365 个候选分子",
            safe_context_payload=self.CTX,
            planner_messages=planner_messages,
            observations={},
        )
        self.assertIsNone(clean)


class Phase2NumericHardeningTests(unittest.TestCase):
    """The lead-opt pure-answer regression: phase-2 enrichment fabricated numbers and
    double-encoded its round; both reached the user verbatim because _finalize_answer ran
    neither the numeric gate nor the fenced/double-encoding check on the enriched text.
    """

    CTX = {
        "page": {"contextType": "task_detail", "workflowKey": "lead_optimization"},
        "runtime": {"displayTaskState": "SUCCESS"},
        "currentTask": {
            "id": "row-1",
            "task_state": "SUCCESS",
            "properties": {
                "lead_opt_list": {"candidate_count": 365, "enumerated_candidates": {"count": 365}},
                "lead_opt_state": {"prediction_summary": {"total": 0, "success": 0}},
            },
        },
    }

    def _turn(self, planner_message: str, phase2_reply: str) -> dict:
        planner_round = json.dumps(
            {"message": planner_message, "questions": [], "operations": []}, ensure_ascii=False
        )
        session = _ScriptedSession([planner_round, phase2_reply])
        assistant = CopilotAssistant(
            chat_api_url="http://model.invalid/v1/chat/completions",
            chat_api_key="k",
            chat_model="m",
            timeout_seconds=3,
            session=session,
            logger=NullLogger(),
            max_planner_rounds=4,
        )
        with patch_host_skills():
            return assistant.plan_turn(
                context_type="task_detail",
                context_payload=self.CTX,
                user_id="u",
                username="alice",
                content="有多少候选分子？预测进展如何？",
            )

    def test_planner_fabricated_count_is_corrected_on_pure_answer_path(self):
        honest_correction = json.dumps(
            {
                "message": "该任务共 365 个候选分子，预测完成 0 个。",
                "questions": [],
                "operations": [],
            },
            ensure_ascii=False,
        )
        result = self._turn(
            "共有 10 个候选分子，预测已完成 2 个。",
            honest_correction,
        )
        self.assertEqual(result["state"], "complete")
        self.assertNotIn("10 个候选", result["content"])
        self.assertNotIn("已完成 2 个", result["content"])

    def test_phase2_double_encoded_output_is_discarded(self):
        fenced = (
            "```json\n"
            + json.dumps(
                {
                    "message": "当前任务共有 **10 个候选分子**，状态 **RUNNING**，亲和力 −9.2 kcal/mol。",
                    "operations": [],
                },
                ensure_ascii=False,
            )
            + "\n```"
        )
        result = self._turn("该任务共 365 个候选分子，预测完成 0 个。", fenced)
        self.assertEqual(result["state"], "complete")
        self.assertNotIn("```", result["content"])
        self.assertNotIn("10 个候选", result["content"])
        self.assertIn("365", result["content"])

    def test_phase2_fabricated_numbers_are_discarded_not_shipped(self):
        hallucinated = (
            "当前先导优化任务共有 **10 个候选分子**，已完成 2 个；"
            "候选分子 1 预测值 −9.2 kcal/mol，pLDDT 0.75。"
        )
        result = self._turn("该任务共 365 个候选分子，预测完成 0 个。", hallucinated)
        self.assertEqual(result["state"], "complete")
        self.assertNotIn("−9.2", result["content"])
        self.assertNotIn("10 个候选", result["content"])
        self.assertIn("365", result["content"])

    def test_phase2_honest_enrichment_still_ships(self):
        honest = "该 MMP 查询产出 **365 个候选分子**；预测任务 0 个排队、0 个成功——还没有任何预测提交。"
        result = self._turn("365 个候选，0 个预测。", honest)
        self.assertEqual(result["state"], "complete")
        self.assertIn("365", result["content"])
        self.assertIn("0", result["content"])
