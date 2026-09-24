from __future__ import annotations

import json
import logging
import random
import threading
import unittest
from typing import Any, Dict, List

import requests

from management_api.copilot import CopilotAssistant
from management_api.copilot_skill_harness import (
    CONFIRMATION_EFFECTS,
    CopilotSkillDefinition,
    CopilotSkillHarness,
)
from management_api.copilot_skills.online_databases import (
    OnlineDatabaseSkills,
    OnlineSkillDefinition,
)


EMPTY_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


class RecordingSkills:
    def __init__(self) -> None:
        self.definitions = [
            OnlineSkillDefinition(
                name="registry.read",
                description="Read authoritative registry data.",
                input_schema=EMPTY_OBJECT_SCHEMA,
            )
        ]
        self.calls: List[tuple[str, Dict[str, Any]]] = []

    def execute(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        return {"source": "registry", "ok": True}


class StubResponse:
    def __init__(self, *, ok: bool, payload: Dict[str, Any] | None = None, status: int = 200, text: str = "") -> None:
        self.ok = ok
        self._payload = payload or {}
        self.status_code = status
        self.text = text

    def json(self) -> Dict[str, Any]:
        return self._payload


class StubSession:
    def __init__(self, responses: List[StubResponse]) -> None:
        self.responses = list(responses)
        self.posts: List[Dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> StubResponse:
        self.posts.append({"url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected model request")
        return self.responses.pop(0)


def write_definition(effect: str = "update") -> CopilotSkillDefinition:
    return CopilotSkillDefinition(
        name=f"host.{effect}",
        label="Confirm host operation",
        description="Apply the declared host operation.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string", "minLength": 1}},
            "required": ["value"],
            "additionalProperties": False,
        },
        effect=effect,
        destructive=effect == "delete",
    )


class CopilotHarnessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.write = write_definition()
        self.definitions = self.harness.definitions([self.write])

    def test_protocol_prompt_states_the_three_role_contract(self) -> None:
        # The planner/harness/skills division of labor is the contract the whole loop runs on;
        # it must be stated once, at the top of the protocol prompt, in English.
        prompt = self.harness.render_protocol_prompt(self.definitions)
        self.assertIn("CONTRACT — three roles, one loop", prompt)
        self.assertIn("PLANNER (you): own the DIRECTION", prompt)
        self.assertIn("HARNESS: own the INVARIANT", prompt)
        self.assertIn("SKILLS: atomic unit operations", prompt)
        self.assertIn("outcome class", prompt)
        self.assertIn("failed receipt is an open blocker", prompt)

    def test_builtin_skill_descriptions_carry_no_qa_examples(self) -> None:
        # Skill descriptions declare capabilities, types, and boundaries — not scripted
        # question/answer examples that overfit the planner to specific phrasings.
        from management_api.copilot_skills.online_databases import OnlineDatabaseSkills
        from tests.helpers import FakeResponse, NullLogger

        class _Session:
            def post(self, *args: Any, **kwargs: Any) -> Any:
                return FakeResponse("")

        skills = OnlineDatabaseSkills(session=_Session())  # type: ignore[arg-type]
        for definition in skills.definitions:
            self.assertNotIn("Answers '", definition.description, definition.name)
            self.assertNotIn(" e.g. ", definition.description, definition.name)

    def test_planner_schema_is_closed_and_discriminated_by_registered_skill(self) -> None:
        # The simple schema is the only envelope sent to the model: closed object, required
        # fields, and the skill enum discriminates exactly the registered catalog.
        schema = self.harness.planner_output_schema_simple(self.definitions)

        self.assertIs(schema["additionalProperties"], False)
        self.assertEqual(schema["required"], ["message", "questions", "operations"])
        self.assertNotIn("state", schema["properties"])
        self.assertEqual(set(schema["properties"]["operations"]["items"]["properties"]["skill"]["enum"]), set(self.definitions))

    def test_harness_derives_state_and_owns_confirmation_metadata(self) -> None:
        candidate = {
            "message": "The operation is ready for confirmation.",
            "questions": [],
            "operations": [
                {
                    "id": "operation-1",
                    "skill": self.write.name,
                    "arguments": {"value": "declared"},
                    "depends_on": [],
                }
            ],
        }

        audit = self.harness.audit_plan(candidate, self.definitions)

        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "await_confirmation")
        self.assertEqual(audit.operations[0].label, self.write.label)
        self.assertEqual(audit.operations[0].description, self.write.description)

    def test_schema_keywords_are_rejected_as_planner_data(self) -> None:
        candidate = {
            "message": "No operation was requested.",
            "questions": [],
            "operations": [],
            "additionalProperties": False,
        }

        audit = self.harness.audit_plan(candidate, self.definitions)

        self.assertIn("planner output field is not declared: additionalProperties", audit.issues)

    def test_operation_dependencies_are_explicit_and_unique(self) -> None:
        base_operation = {
            "id": "operation-1",
            "skill": self.write.name,
            "arguments": {"value": "declared"},
        }
        missing = self.harness.audit_plan(
            {"message": "Awaiting confirmation.", "questions": [], "operations": [base_operation]},
            self.definitions,
        )
        read = CopilotSkillDefinition(
            name="host.lookup",
            description="Look values up.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="read",
        )
        definitions = self.harness.definitions([self.write, read])
        duplicated = self.harness.audit_plan(
            {
                "message": "Awaiting confirmation.",
                "questions": [],
                "operations": [
                    {"id": "prior", "skill": read.name, "arguments": {}, "depends_on": []},
                    {**base_operation, "depends_on": ["prior", "prior"]},
                ],
            },
            definitions,
        )

        self.assertIn("operations[0].depends_on is required", missing.issues)
        # Duplicated dependency ids are REPAIRED deterministically (dedupe preserving order),
        # not rejected: [a, a] is semantically identical to [a], and rejection made real
        # models re-emit an identical plan until the turn died (A/B evidence, gemma4-31b).
        # The repaired plan passes the audit and carries the deduped dependency list.
        self.assertEqual(duplicated.issues, ())
        self.assertEqual(
            [tuple(op.depends_on) for op in duplicated.operations],
            [(), ("prior",)],
        )

    def test_only_read_effects_execute_in_the_harness(self) -> None:
        read_candidate = {
            "message": "Reading authoritative data.",
            "questions": [],
            "operations": [
                {"id": "read-1", "skill": "registry.read", "arguments": {}, "depends_on": []}
            ],
        }
        read_audit = self.harness.audit_plan(read_candidate, self.definitions)

        self.assertEqual(read_audit.state, "continue")
        observations = self.harness.execute_operations(read_audit.operations)
        self.assertTrue(observations["read-1"]["ok"])
        self.assertEqual(len(self.skills.calls), 1)

        write_candidate = {
            "message": "Awaiting confirmation.",
            "questions": [],
            "operations": [
                {
                    "id": "write-1",
                    "skill": self.write.name,
                    "arguments": {"value": "declared"},
                    "depends_on": [],
                }
            ],
        }
        write_audit = self.harness.audit_plan(write_candidate, self.definitions)
        with self.assertRaisesRegex(ValueError, "write operation cannot execute"):
            self.harness.execute_operations(write_audit.operations)
        self.assertEqual(len(self.skills.calls), 1)

    def test_every_non_read_effect_requires_confirmation(self) -> None:
        for effect in CONFIRMATION_EFFECTS:
            with self.subTest(effect=effect):
                self.assertFalse(write_definition(effect).read_only)

        unsupported = write_definition("write")
        with self.assertRaisesRegex(ValueError, "unsupported effect"):
            self.harness.definitions([unsupported])

    def test_confirmation_action_preserves_audited_arguments_and_control_metadata(self) -> None:
        audit = self.harness.audit_plan(
            {
                "message": "Awaiting confirmation.",
                "questions": [],
                "operations": [
                    {
                        "id": "write-1",
                        "skill": self.write.name,
                        "arguments": {"value": "declared"},
                        "depends_on": [],
                    }
                ],
            },
            self.definitions,
        )

        action = self.harness.build_confirmation_actions(
            audit.operations,
            plan_id="plan-1",
            context_type="context",
        )[0]

        self.assertEqual(action["arguments"], {"value": "declared"})
        self.assertEqual(action["effect"], "update")
        self.assertTrue(action["needs_confirmation"])
        self.assertFalse(action["execute_now"])
        self.assertEqual(action["payload"]["planId"], "plan-1")
        self.assertEqual(action["payload"]["operationId"], "write-1")

    def _compound_observation(self, cid: str = "134611040", title: str = "benzimidazole acid", smiles: str = "C1CO[C@@H]1CN2C3") -> dict:
        return {
            "find": {
                "ok": True,
                "values": [{"source": "pubchem", "results": [{"cid": cid, "title": title, "smiles": smiles}]}],
            }
        }

    def test_ungrounded_final_answer_is_rejected_when_one_record_retrieved(self) -> None:
        audit = self.harness.audit_plan(
            {"message": "这是一种 JAK2 抑制剂，用于骨髓增殖性肿瘤。", "questions": [], "operations": []},
            self.definitions,
            observations=self._compound_observation(),
        )
        self.assertTrue(any("does not reference" in issue for issue in audit.issues))

    def test_grounded_final_answer_is_accepted(self) -> None:
        audit = self.harness.audit_plan(
            {"message": "PF06882961 -> CID 134611040，SMILES C1CO[C@@H]1CN2C3。", "questions": [], "operations": []},
            self.definitions,
            observations=self._compound_observation(),
        )
        self.assertFalse(any("does not reference" in issue for issue in audit.issues))

    def test_grounding_enforced_for_small_record_sets(self) -> None:
        # 1–3 records: the answer must name at least one retrieved record. A "several candidates"
        # summary that names NONE of them is refused — with a stronger planner this bar is safe.
        observations = {
            "find": {
                "ok": True,
                "values": [{"results": [{"pdbId": "1UBQ"}, {"pdbId": "1IVO"}]}],
            }
        }
        audit = self.harness.audit_plan(
            {"message": "There were several candidates.", "questions": [], "operations": []},
            self.definitions,
            observations=observations,
        )
        self.assertTrue(any("does not reference" in issue for issue in audit.issues))
        named = self.harness.audit_plan(
            {"message": "Found 1UBQ and 1IVO.", "questions": [], "operations": []},
            self.definitions,
            observations=observations,
        )
        self.assertFalse(any("does not reference" in issue for issue in named.issues))

    def test_grounding_not_enforced_for_large_record_sets(self) -> None:
        # A large result set is legitimately summarized in aggregate; quoting one identifier is
        # not required. This exemption stays so aggregate answers are never false-rejected.
        observations = {
            "find": {
                "ok": True,
                "values": [{"results": [{"pdbId": f"1XY{i}"} for i in range(6)]}],
            }
        }
        audit = self.harness.audit_plan(
            {"message": "There were several candidates.", "questions": [], "operations": []},
            self.definitions,
            observations=observations,
        )
        self.assertFalse(any("does not reference" in issue for issue in audit.issues))

    def test_grounding_not_enforced_without_observations(self) -> None:
        audit = self.harness.audit_plan(
            {"message": "Here is a general answer.", "questions": [], "operations": []},
            self.definitions,
        )
        self.assertFalse(any("does not reference" in issue for issue in audit.issues))

    def test_grounding_anchors_come_from_the_shared_field_registry(self) -> None:
        # The guard derives anchors from RECORD_IDENTITY_FIELDS, so any identity field grounds —
        # not a hardcoded per-source subset. nctId is in the registry; a generic phrase does not.
        observations = {"trial": {"ok": True, "values": [{"results": [{"nctId": "NCT04284442"}]}]}}
        grounded = self.harness.audit_plan(
            {"message": "Trial NCT04284442 is enrolling.", "questions": [], "operations": []},
            self.definitions,
            observations=observations,
        )
        self.assertFalse(any("does not reference" in issue for issue in grounded.issues))
        ungrounded = self.harness.audit_plan(
            {"message": "There is an enrolling trial somewhere.", "questions": [], "operations": []},
            self.definitions,
            observations=observations,
        )
        self.assertTrue(any("does not reference" in issue for issue in ungrounded.issues))

    def test_summarize_observations_renders_registry_fields(self) -> None:
        # The summarizer renders whatever the shared registry declares, including fields the old
        # hardcoded tuple omitted (nctId) and numeric fields (avgPlddt).
        summary = CopilotAssistant._summarize_observations(
            {"trial": {"ok": True, "skill": "clinicaltrials.search", "values": [{"results": [{"nctId": "NCT04284442", "avgPlddt": 88.4}]}]}}
        )
        self.assertIn("nctId=NCT04284442", summary)
        self.assertIn("avgPlddt=88.4", summary)

    def test_failed_observation_is_source_unavailable_not_no_match(self) -> None:
        # A transport/source failure (HTTP 5xx, timeout) must be reported as "source unavailable"
        # so the model tells the user to retry — NOT conflated with an authoritative "no match".
        observations = {
            "inh": {
                "ok": False,
                "skill": "chembl.target_activity",
                "items": [{"index": 0, "arguments": {"query": "HPK1", "accession": "Q92918"}, "ok": False, "result": None, "error": "online source unreachable: Read timed out"}],
                "values": [],
                "errors": [{"index": 0, "error": "online source unreachable: Read timed out"}],
            }
        }
        summary = CopilotAssistant._summarize_observations(observations)
        self.assertIn("SOURCE UNAVAILABLE", summary)
        self.assertIn("NOT an authoritative no-match", summary)
        self.assertIn("online source unreachable", summary)
        self.assertIn("HPK1", summary)  # the target label is echoed for context

    def test_zero_result_success_is_not_marked_source_unavailable(self) -> None:
        # An authoritative empty result (ok=True, zero records) is a real "no match" and must NOT
        # be dressed up as a source failure — the two carry opposite advice to the user.
        observations = {
            "find": {
                "ok": True,
                "skill": "chembl.target_activity",
                "values": [{"query": "NOPE", "results": []}],
            }
        }
        summary = CopilotAssistant._summarize_observations(observations)
        # treated as a normal (authoritative) observation, never as a source failure
        self.assertNotIn("SOURCE UNAVAILABLE", summary)
        self.assertNotIn("could not be reached", summary)

    def test_chembl_target_activity_record_surfaces_smiles_and_potency(self) -> None:
        # Flat bioactivity records must surface smiles (long) + value (numeric) + activityType/units
        # (identity) so the model can quote the structure and potency verbatim in its answer.
        observations = {
            "inh": {
                "ok": True,
                "skill": "chembl.target_activity",
                "values": [{"query": "HPK1", "results": [
                    {"title": "inhibitor X", "chemblId": "CHEMBL123", "smiles": "CCO", "activityType": "IC50", "value": 12.0, "units": "nM"},
                ]}],
            }
        }
        summary = CopilotAssistant._summarize_observations(observations)
        self.assertIn("smiles=CCO", summary)
        self.assertIn("value=12.0", summary)
        self.assertIn("activityType=IC50", summary)
        self.assertIn("units=nM", summary)
        self.assertIn("chemblId=CHEMBL123", summary)

    def test_record_surfaces_all_scalar_fields_not_just_curated_ones(self) -> None:
        # The summarizer surfaces EVERY scalar field of a record, not only an allowlisted few — so
        # fields the old curated tuples omitted (PubMed journal/year/authors, trial status/phase)
        # now reach the model instead of being silently dropped (the same class of bug that hid
        # ChEMBL SMILES). List values join cleanly; meta/url fields stay hidden.
        observations = {
            "lit": {
                "ok": True,
                "skill": "pubmed.search",
                "values": [{
                    "query": "osimertinib",
                    "results": [
                        {"pmid": "111", "title": "Resistance mechanisms", "authors": "A B et al.", "journal": "Nature", "year": "2026", "sourceUrl": "https://x/111"},
                    ],
                }],
            },
            "trial": {
                "ok": True,
                "skill": "clinicaltrials.search",
                "values": [{
                    "query": "EGFR",
                    "results": [
                        {"nctId": "NCT1", "title": "Trial", "status": "RECRUITING", "phase": "PHASE2", "conditions": ["Cancer", "EGFR Mutation"]},
                    ],
                }],
            },
        }
        summary = CopilotAssistant._summarize_observations(observations)
        # previously-hidden scalar fields now surface
        self.assertIn("journal=Nature", summary)
        self.assertIn("year=2026", summary)
        self.assertIn("authors=A B et al.", summary)
        self.assertIn("status=RECRUITING", summary)
        self.assertIn("phase=PHASE2", summary)
        # list field is joined, not stringified as a Python list
        self.assertIn("conditions=Cancer, EGFR Mutation", summary)
        self.assertNotIn("['Cancer'", summary)
        # curated fields still surface
        self.assertIn("pmid=111", summary)
        self.assertIn("nctId=NCT1", summary)
        # meta and URL fields stay hidden
        self.assertNotIn("sourceUrl=", summary)
        self.assertNotIn("source=pubmed", summary)

    def test_observation_reference_extracts_scalar_from_top_record(self) -> None:
        observations = {
            "prot": {
                "ok": True,
                "values": [{"source": "uniprot", "results": [{"accession": "Q02127", "sequence": "MTPRK", "geneNames": "DHODH"}]}],
            },
            "lig": {
                "ok": True,
                "values": [{"source": "pubchem", "results": [{"cid": "134611040", "smiles": "C1CO"}]}],
            },
        }
        self.assertEqual(
            self.harness.materialize_observations({"$fromObservation": "prot", "field": "sequence", "index": 0}, observations),
            "MTPRK",
        )
        self.assertEqual(
            self.harness.materialize_observations({"$fromObservation": "lig", "field": "smiles"}, observations),
            "C1CO",
        )

    def test_observation_reference_all_form_returns_the_column(self) -> None:
        # The sanctioned collection form is {"all": true} over a named field; the legacy
        # fieldless list-forwarding branch was removed with the pre-refactor protocol.
        observations = {"x": {"ok": True, "values": [{"results": [{"a": 1}, {"a": 2}]}]}}
        self.assertEqual(
            self.harness.materialize_observations({"$fromObservation": "x", "field": "a", "all": True}, observations),
            [1, 2],
        )

    def test_write_operation_consumes_prior_observation_scalar(self) -> None:
        observations = {
            "prot": {"ok": True, "values": [{"results": [{"value": "declared-from-observation"}]}]},
        }
        audit = self.harness.audit_plan(
            {
                "message": "Creating the task from the retrieved sequence.",
                "questions": [],
                "operations": [
                    {
                        "id": "create-1",
                        "skill": self.write.name,
                        "arguments": {"value": {"$fromObservation": "prot", "field": "value", "index": 0}},
                        "depends_on": ["prot"],
                    }
                ],
            },
            self.definitions,
            observations=observations,
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.operations[0].arguments, {"value": "declared-from-observation"})

    def test_unfulfilled_promise_message_after_retrieval_is_rejected(self) -> None:
        # With a small retrieved record set, a message that promises future work ("I will look
        # up...") instead of answering names none of the retrieved records — refused by the
        # grounding audit. The planner must answer from the data it already has or emit the
        # operations it promised.
        observations = {
            "protein": {"ok": True, "values": [{"results": [{"accession": "Q02127", "name": "DHODH"}]}]},
            "compound": {"ok": True, "values": [{"results": [{"name": "ibuprofen", "smiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"}]}]},
        }
        audit = self.harness.audit_plan(
            {
                "message": "I will look up the structures and set up the task.",
                "questions": [],
                "operations": [],
            },
            self.definitions,
            observations=observations,
        )
        self.assertTrue(any("does not reference" in issue for issue in audit.issues))
        self.assertEqual(audit.state, "complete")

    def test_grounded_answer_after_retrieval_is_allowed(self) -> None:
        # When the model retrieved records and the final message actually cites them, the turn
        # completes normally — this is a legitimate lookup answer, not a premature stop.
        observations = {
            "compound": {"ok": True, "values": [{"results": [{"name": "ibuprofen", "smiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"}]}]},
        }
        audit = self.harness.audit_plan(
            {
                "message": "Ibuprofen has the SMILES CC(C)CC1=CC=C(C=C1)C(C)C(=O)O.",
                "questions": [],
                "operations": [],
            },
            self.definitions,
            observations=observations,
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "complete")

    def test_arbitrary_scalar_observation_allows_message_completion(self) -> None:
        # An observation carrying only an arbitrary scalar (no identity/SMILES/sequence) is not plan
        # input, so ending with a message after it is a legitimate lookup, not a premature stop.
        observations = {
            "state": {"ok": True, "values": [{"value": "resolved"}]},
        }
        audit = self.harness.audit_plan(
            {
                "message": "The request has been evaluated.",
                "questions": [],
                "operations": [],
            },
            self.definitions,
            observations=observations,
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "complete")

    def test_message_text_is_never_rejected_by_harness(self) -> None:
        # The harness does NOT audit message text content. Message quality (length, formatting,
        # colon-ending, context citation) is the MODEL's responsibility, guided by the system prompt.
        # The harness audits STRUCTURE only: operations, schema, dependencies, grounding (when a
        # record was retrieved). Any non-empty message passes the audit.
        for msg in ("当前任务分析如下：", "breakdown follows:", "分布如下： ", "hi", "我会做这些："):
            audit = self.harness.audit_plan(
                {"message": msg, "questions": [], "operations": []},
                self.definitions,
                context_type="task_detail",
            )
            self.assertEqual(audit.issues, (), f"{msg!r} was wrongly rejected: {audit.issues}")
            self.assertEqual(audit.state, "complete")

    def test_grounded_context_answer_accepted(self) -> None:
        # A message that cites real payload values passes cleanly.
        audit = self.harness.audit_plan(
            {"message": "已完成,boltz 后端 pLDDT 85.2。", "questions": [], "operations": []},
            self.definitions,
            context_type="task_detail",
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "complete")

    def test_anchor_guard_does_not_fire_without_anchors(self) -> None:
        # On a page with no rich context (empty workspace), there are no anchors, so the guard
        # cannot fire — any non-empty message is accepted. This prevents the guard from rejecting
        # legitimate short messages on context-poor pages.
        audit = self.harness.audit_plan(
            {"message": "你好", "questions": [], "operations": []},
            self.definitions,
            context_type="workspace",
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "complete")

    def test_substantive_context_answer_is_accepted(self) -> None:
        # A real analysis that cites actual values passes cleanly.
        audit = self.harness.audit_plan(
            {
                "message": "This task completed successfully with average pLDDT 85.2 and ipTM 0.71, indicating a high-confidence prediction.",
                "questions": [],
                "operations": [],
            },
            self.definitions,
            context_type="task_detail",
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "complete")
    @staticmethod
    def assistant(session: StubSession, *, rounds: int = 8) -> CopilotAssistant:
        return CopilotAssistant(
            chat_api_url="https://model.invalid/v1/chat/completions",
            chat_api_key="",
            chat_model="model",
            timeout_seconds=10,
            session=session,
            logger=logging.getLogger("copilot-protocol-test"),
            max_planner_rounds=rounds,
        )

    def test_model_contract_failure_is_not_retried_or_downgraded(self) -> None:
        session = StubSession([StubResponse(ok=False, status=400, text="unsupported contract")])
        assistant = self.assistant(session)

        with self.assertRaisesRegex(RuntimeError, "required structured-output contract"):
            assistant._call_model(
                [{"role": "user", "content": "plan"}],
                response_schema=EMPTY_OBJECT_SCHEMA,
            )

        self.assertGreaterEqual(len(session.posts), 1)
        body = session.posts[0]["json"]
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})

    def test_json_output_with_grammar_accepted(self) -> None:
        # With the simple grammar constraint, the model outputs valid JSON. The planner parses it
        # directly. The message can be any length (no maxLength constraint in the schema).
        content = json.dumps({"message": "你好！我是 V-Bio Copilot。", "questions": [], "operations": []})
        payload = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}
        # Phase-2 free-text answer (the pure-answer turn enriches the short JSON message).
        payload2 = {"choices": [{"message": {"content": "你好！我是 V-Bio Copilot。"}, "finish_reason": "stop"}]}
        session = StubSession([StubResponse(ok=True, payload=payload), StubResponse(ok=True, payload=payload2)])
        assistant = self.assistant(session, rounds=4)

        result = assistant.plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="hello",
        )
        self.assertEqual(result["state"], "complete")
        self.assertIn("V-Bio Copilot", result["content"])
        self.assertGreaterEqual(len(session.posts), 1)

    def test_malformed_json_gives_up_after_retry_budget(self) -> None:
        # Empty responses exhaust the retry budget — the planner cannot parse them and cannot
        # treat them as plain text (too short). The turn fails honestly.
        bad = {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}
        session = StubSession([StubResponse(ok=True, payload=bad)] * 4)
        assistant = self.assistant(session, rounds=8)

        result = assistant.plan_turn(
            context_type="workspace",
            context_payload={},
            user_id="user",
            username="user",
            content="hello",
        )
        # The planner should return a result (not raise) — the harness keeps the audit chain intact.
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["actions"], [])

        # it retried a few times then stopped (bounded), rather than burning the whole round budget
        self.assertLessEqual(len(session.posts), assistant.max_malformed_retries + 3)

    def test_abort_skips_the_model_call(self) -> None:
        # A pre-set abort must short-circuit before any model call (no canned responses provided).
        session = StubSession([])
        assistant = self.assistant(session)
        abort = threading.Event()
        abort.set()
        with self.assertRaisesRegex(RuntimeError, "aborted"):
            assistant.plan_turn(
                context_type="workspace",
                context_payload={},
                user_id="user",
                username="user",
                content="hi",
                abort=abort,
            )
        self.assertEqual(len(session.posts), 0)

    def test_repeated_audit_rejection_stops_without_exhausting_round_budget(self) -> None:
        invalid_turn = json.dumps(
            {
                "message": "No operation was requested.",
                "questions": [],
                "operations": [],
                "additionalProperties": False,
            }
        )
        response = {
            "choices": [{"message": {"content": invalid_turn}, "finish_reason": "stop"}],
        }
        # Structural rejection (additionalProperties is a schema violation): reject → retry → same
        # reject → convergence-aid coaching round → same reject again → break. 3 responses
        # (the coaching grant gives exactly one extra attempt before honest failure).
        session = StubSession(
            [StubResponse(ok=True, payload=response), StubResponse(ok=True, payload=response),
             StubResponse(ok=True, payload=response)]
        )
        assistant = self.assistant(session, rounds=8)

        result = assistant.plan_turn(
            context_type="unregistered_context",
            context_payload={},
            user_id="user",
            username="user",
            content="respond",
        )
        # The repeated structural rejection (even after the coaching aid) ends in an honest failure.
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(session.posts), 3)  # 2 rejections + coaching round, then stopped

    @staticmethod
    def _choice(turn: Dict[str, Any]) -> Dict[str, Any]:
        return {"choices": [{"message": {"content": json.dumps(turn)}, "finish_reason": "stop"}]}

    def test_outline_is_locked_and_driven_step_by_step(self) -> None:
        # Round 0 declares the outline; round 1 tries to re-emit goal_steps and is REJECTED (the
        # direction is immutable); round 2 concretizes step 1 with an operation; round 3 concludes
        # step 2 and completes. The harness drives every step from the locked outline.
        session = StubSession(
            [
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Planning.",
                            "questions": [],
                            "operations": [],
                            "goal_steps": [
                                {"description": "fetch data"},
                                {"description": "submit"},
                            ],
                        }
                    ),
                ),
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Redo.",
                            "questions": [],
                            "operations": [],
                            "goal_steps": [{"description": "a different direction"}],
                        }
                    ),
                ),
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Step 1 concretized.",
                            "questions": [],
                            "operations": [
                                {
                                    "id": "w1",
                                    "skill": "projects:update_view",
                                    "arguments": {"search": "active"},
                                    "depends_on": [],
                                }
                            ],
                        }
                    ),
                ),
                StubResponse(
                    ok=True,
                    payload=self._choice({"message": "All done.", "questions": [], "operations": []}),
                ),
            ]
        )
        assistant = self.assistant(session)

        result = assistant.plan_turn(
            context_type="project_list",
            context_payload={},
            user_id="user",
            username="user",
            content="set up and run a task",
        )

        self.assertEqual(result["state"], "await_confirmation")  # one confirmation action pending
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(len(session.posts), 4)
        # The re-emission round was rejected with the direction-lock message.
        all_feedback = "\n".join(
            str(message.get("content") or "")
            for post in session.posts
            for message in post["json"]["messages"]
            if str(message.get("role") or "") == "system"
        )
        self.assertIn("outline is fixed", all_feedback)
        events = [step["event"] for step in result["trace"]]
        self.assertEqual(events.count("outline"), 1)  # locked once, never re-accepted
        self.assertEqual(events.count("step_done"), 2)

    def test_question_mid_outline_returns_needs_input(self) -> None:
        # A question inside an outline ends the turn with needs_input and keeps the questions —
        # it must not be silently discarded nor advance the step.
        session = StubSession(
            [
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Planning.",
                            "questions": [],
                            "operations": [],
                            "goal_steps": [{"description": "fetch data"}, {"description": "submit"}],
                        }
                    ),
                ),
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Which target?",
                            "questions": [{"text": "Which target?", "kind": "freeform"}],
                            "operations": [],
                        }
                    ),
                ),
            ]
        )
        assistant = self.assistant(session)

        result = assistant.plan_turn(
            context_type="project_list",
            context_payload={},
            user_id="user",
            username="user",
            content="set up a task",
        )

        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["questions"], [{"text": "Which target?", "kind": "freeform"}])
        self.assertEqual(len(session.posts), 2)  # the question ended the turn, no step advance


    def test_question_mid_outline_keeps_accumulated_step_actions(self) -> None:
        # Step 1 surfaced a confirmation action; step 2 asks a question. The question ends the
        # turn, but the already-planned confirmation must survive it — the next turn re-plans
        # and cannot recover a dropped action.
        session = StubSession(
            [
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Planning.",
                            "questions": [],
                            "operations": [],
                            "goal_steps": [{"description": "filter view"}, {"description": "submit"}],
                        }
                    ),
                ),
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Step 1 done.",
                            "questions": [],
                            "operations": [
                                {
                                    "id": "w1",
                                    "skill": "projects:update_view",
                                    "arguments": {"search": "active"},
                                    "depends_on": [],
                                }
                            ],
                        }
                    ),
                ),
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Which name?",
                            "questions": [{"text": "Which name?", "kind": "freeform"}],
                            "operations": [],
                        }
                    ),
                ),
            ]
        )
        assistant = self.assistant(session)

        result = assistant.plan_turn(
            context_type="project_list",
            context_payload={},
            user_id="user",
            username="user",
            content="set up and run a task",
        )

        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["questions"], [{"text": "Which name?", "kind": "freeform"}])
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["id"], "projects:update_view")
        self.assertEqual(result["actions"][0]["arguments"], {"search": "active"})

    def test_question_mid_outline_reconsiders_held_writes(self) -> None:
        # A read+write round mid-outline holds the write; the next round asks a question. The
        # one-shot reconsideration must fire (the planner re-emits the write on its own), and
        # the later question must return WITH the accumulated write action — never stranded.
        session = StubSession(
            [
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Planning.",
                            "questions": [],
                            "operations": [],
                            "goal_steps": [{"description": "inspect"}, {"description": "submit"}],
                        }
                    ),
                ),
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Inspecting.",
                            "questions": [],
                            "operations": [
                                {
                                    "id": "t1",
                                    "skill": "translate.to_english",
                                    "arguments": {"text": "布洛芬", "english": "ibuprofen"},
                                    "depends_on": [],
                                },
                                {
                                    "id": "w1",
                                    "skill": "projects:update_view",
                                    "arguments": {"search": "active"},
                                    "depends_on": [],
                                },
                            ],
                        }
                    ),
                ),
                # The question arrives while the write is still held -> forced reconsideration.
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Which name?",
                            "questions": [{"text": "Which name?", "kind": "freeform"}],
                            "operations": [],
                        }
                    ),
                ),
                # Reconsideration: the write is re-emitted alone (questions cannot accompany
                # confirmation operations), so it accumulates for the step.
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Re-emitting the held operation.",
                            "questions": [],
                            "operations": [
                                {
                                    "id": "w2",
                                    "skill": "projects:update_view",
                                    "arguments": {"search": "active"},
                                    "depends_on": [],
                                }
                            ],
                        }
                    ),
                ),
                # The question is re-asked for the next step now that the debt is resolved.
                StubResponse(
                    ok=True,
                    payload=self._choice(
                        {
                            "message": "Which name?",
                            "questions": [{"text": "Which name?", "kind": "freeform"}],
                            "operations": [],
                        }
                    ),
                ),
            ]
        )
        assistant = self.assistant(session)

        result = assistant.plan_turn(
            context_type="project_list",
            context_payload={},
            user_id="user",
            username="user",
            content="inspect then set up",
        )

        all_feedback = "\n".join(
            str(message.get("content") or "")
            for post in session.posts
            for message in post["json"]["messages"]
            if str(message.get("role") or "") == "system"
        )
        self.assertIn("were HELD and never applied", all_feedback)
        # The reconsideration resolved the debt, and the re-asked question returns WITH the
        # accumulated confirmation action instead of stranding it.
        self.assertEqual(result["state"], "needs_input")
        self.assertEqual(result["questions"], [{"text": "Which name?", "kind": "freeform"}])
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["actions"][0]["id"], "projects:update_view")


class CopilotSchemaSanitizerTests(unittest.TestCase):
    def test_strips_grammar_unsupported_keys_but_preserves_structure(self) -> None:
        from management_api.copilot import _sanitize_schema_for_grammar

        schema = {
            "type": "object",
            "properties": {
                "tags": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                "identifier": {"type": "string", "pattern": "^[0-9]+$"},
                "name": {"type": "string"},
            },
            "required": ["name"],
        }
        cleaned = _sanitize_schema_for_grammar(schema)
        self.assertNotIn("uniqueItems", cleaned["properties"]["tags"])
        self.assertNotIn("pattern", cleaned["properties"]["identifier"])
        self.assertEqual(cleaned["properties"]["name"], {"type": "string"})
        self.assertEqual(cleaned["required"], ["name"])

    def test_thinking_mode_json_extraction_ignores_trailing_braces(self) -> None:
        # Free-text thinking output with stray trailing braces: the first complete JSON object is
        # extracted (a greedy {.*} regex would span to the last brace and fail to parse).
        from management_api.copilot import CopilotAssistant

        candidate = CopilotAssistant._parse_planner_turn(
            'Here is my plan: {"message":"hi","questions":[],"operations":[]} (note: extra {braces} after)'
        )
        self.assertEqual(candidate["message"], "hi")
        self.assertEqual(candidate["operations"], [])


class CopilotSchemaPatternTests(unittest.TestCase):
    """The grammar strips `pattern`; the harness must re-validate it so a skill schema's pattern
    constraint is actually enforced (closes the gap where pattern was enforced nowhere)."""

    def setUp(self) -> None:
        self.harness = CopilotSkillHarness(skills=RecordingSkills())

    def test_pattern_rejects_non_matching_string(self) -> None:
        schema = {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"}
        self.assertTrue(self.harness._validate_schema("lowercase", schema))  # has errors
        self.assertTrue(self.harness._validate_schema("9ABC", schema))

    def test_pattern_accepts_matching_string(self) -> None:
        schema = {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"}
        self.assertFalse(self.harness._validate_schema("ABC_123", schema))  # no errors
        self.assertFalse(self.harness._validate_schema("A", schema))

    def test_invalid_pattern_in_schema_does_not_crash(self) -> None:
        # A malformed pattern in the schema is treated as no constraint, never an exception.
        self.assertEqual(self.harness._validate_schema("anything", {"type": "string", "pattern": "["}), [])

    def test_non_numeric_minimum_does_not_crash_audit(self) -> None:
        # A malformed schema (non-numeric minimum/maximum) surfaces as an issue, not a TypeError.
        errors = self.harness._validate_schema(5, {"type": "number", "minimum": "big"})
        self.assertTrue(errors)


class CopilotAuditRobustnessTests(unittest.TestCase):
    """The audit must never raise on arbitrary planner output — it reports issues instead. A seeded
    fuzz over random candidates (no hand-picked fixtures) keeps this invariant pinned at scale."""

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self._write_name = write_definition().name
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.definitions = self.harness.definitions([write_definition()])
        self._primitives = [
            None, True, False, 0, 1, -1.5, "", "x", "registry.read", self._write_name,
            [], {}, {"id": "x"}, {"skill": "registry.read"}, [1, 2, 3],
        ]

    def test_audit_never_raises_on_arbitrary_input(self) -> None:
        rng = random.Random(20260731)
        for _ in range(500):
            candidate = self._random_candidate(rng)
            try:
                audit = self.harness.audit_plan(candidate, self.definitions)
            except Exception as exc:  # noqa: BLE001 — the invariant is "no exception, ever"
                self.fail(f"audit_plan raised on candidate {candidate!r}: {exc}")
            self.assertIsInstance(audit.state, str)

    def _random_candidate(self, rng: random.Random) -> Any:
        kind = rng.random()
        if kind < 0.3:
            return rng.choice(self._primitives)
        if kind < 0.6:
            return {
                "message": str(rng.choice(self._primitives) or ""),
                "questions": rng.choice(self._primitives),
                "operations": rng.choice(self._primitives),
                "extra": rng.choice(self._primitives),
            }
        return {
            "message": str(rng.choice(self._primitives) or ""),
            "questions": [str(rng.choice(self._primitives) or "") for _ in range(rng.randint(0, 4))],
            "operations": [self._random_operation(rng) for _ in range(rng.randint(0, 4))],
        }

    def _random_operation(self, rng: random.Random) -> Any:
        return {
            "id": str(rng.randint(0, 3)),
            "skill": rng.choice(["registry.read", self._write_name, "unknown.skill", ""]),
            "arguments": rng.choice(self._primitives),
            "depends_on": [str(rng.randint(0, 3)) for _ in range(rng.randint(0, 3))],
        }


class CrossContextPlanningTests(unittest.TestCase):
    """A single plan may span several host pages. The harness must not reject an operation whose
    context_type differs from the turn's context (that gate is removed); instead each action carries
    its target page so the frontend advances page by page. These tests pin the cross-context contract
    without referencing any concrete workflow or entity."""

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        # A read skill available everywhere, plus write skills on different host pages that target
        # different downstream pages — modeling the project_list -> task_list -> task_detail flow.
        self.read_def = CopilotSkillDefinition(
            name="lookup.resolve",
            description="Resolve an identifier from an authoritative source.",
            input_schema=EMPTY_OBJECT_SCHEMA,
            effect="read",
        )
        self.create_project = CopilotSkillDefinition(
            name="host.create_project",
            label="New project",
            description="Open the new-project flow.",
            input_schema=EMPTY_OBJECT_SCHEMA,
            effect="create",
            context_type="project_list",
            target_context="task_list",
        )
        self.create_task = CopilotSkillDefinition(
            name="host.create_task",
            label="New task",
            description="Create a new task in the current project.",
            input_schema=EMPTY_OBJECT_SCHEMA,
            effect="create",
            context_type="task_list",
            target_context="task_detail",
        )
        self.submit = CopilotSkillDefinition(
            name="host.submit_task",
            label="Start run",
            description="Submit the current task.",
            input_schema=EMPTY_OBJECT_SCHEMA,
            effect="execute",
            context_type="task_detail",
        )
        self.definitions = self.harness.definitions(
            [self.read_def, self.create_project, self.create_task, self.submit]
        )

    def test_cross_context_operation_is_not_rejected_by_source_page(self) -> None:
        # The turn started on project_list, but the plan proposes a task_list skill — this must NOT
        # be an audit failure. The plan crosses page boundaries by design.
        candidate = {
            "message": "Planning across pages.",
            "questions": [],
            "operations": [
                {"id": "op-create-project", "skill": "host.create_project", "arguments": {}, "depends_on": []},
                {"id": "op-create-task", "skill": "host.create_task", "arguments": {}, "depends_on": ["op-create-project"]},
            ],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "await_confirmation")
        self.assertEqual(len(audit.operations), 2)

    def test_confirmation_action_carries_target_context_type(self) -> None:
        candidate = {
            "message": "Planning.",
            "questions": [],
            "operations": [
                {"id": "op-create-project", "skill": "host.create_project", "arguments": {}, "depends_on": []},
            ],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        actions = self.harness.build_confirmation_actions(
            audit.operations, plan_id="plan-1", context_type="project_list", workflow_key=""
        )
        self.assertEqual(len(actions), 1)
        payload = actions[0]["payload"]
        # contextType is where the turn started; targetContextType is where confirming navigates to.
        self.assertEqual(payload["contextType"], "project_list")
        self.assertEqual(payload["targetContextType"], "task_list")

    def test_target_context_falls_back_to_skill_context_when_unset(self) -> None:
        # The submit skill has no explicit target_context; its target defaults to its own page.
        candidate = {
            "message": "Submitting.",
            "questions": [],
            "operations": [
                {"id": "op-submit", "skill": "host.submit_task", "arguments": {}, "depends_on": []},
            ],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="task_detail")
        actions = self.harness.build_confirmation_actions(
            audit.operations, plan_id="plan-2", context_type="task_detail", workflow_key=""
        )
        self.assertEqual(actions[0]["payload"]["targetContextType"], "task_detail")

    def test_from_observation_links_read_to_later_page_write(self) -> None:
        # A read operation produces an observation; a write operation on a DIFFERENT page consumes it
        # via $fromObservation. The harness resolves the reference across pages in the same plan.
        candidate_read = {
            "message": "Looking up.",
            "questions": [],
            "operations": [
                {"id": "op-lookup", "skill": "lookup.resolve", "arguments": {}, "depends_on": []},
            ],
        }
        audit_read = self.harness.audit_plan(candidate_read, self.definitions, context_type="project_list")
        observations = self.harness.execute_operations(audit_read.operations)
        self.assertTrue(observations["op-lookup"]["ok"])

        # Now a write on task_detail references the lookup's result field across the page boundary.
        submit_with_ref = CopilotSkillDefinition(
            name="host.apply_value",
            label="Apply value",
            description="Apply a value retrieved earlier.",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string", "minLength": 1}},
                "required": ["value"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        )
        definitions = self.harness.definitions([self.read_def, submit_with_ref])
        candidate_write = {
            "message": "Applying the retrieved value.",
            "questions": [],
            "operations": [
                {
                    "id": "op-apply",
                    "skill": "host.apply_value",
                    "arguments": {"value": {"$fromObservation": "op-lookup", "field": "source", "index": 0}},
                    "depends_on": ["op-lookup"],
                },
            ],
        }
        audit_write = self.harness.audit_plan(
            candidate_write, definitions, observations=observations, context_type="task_detail"
        )
        self.assertEqual(audit_write.issues, ())
        self.assertEqual(audit_write.operations[0].arguments["value"], "registry")

    def test_choice_question_yields_needs_input_state(self) -> None:
        # A structured choice question (with options) sets the turn to needs_input and carries no
        # operations — the planner asks the user to resolve an ambiguity before planning.
        candidate = {
            "message": "Which task type should I set up?",
            "questions": [
                {
                    "text": "Which task type?",
                    "kind": "choice",
                    "options": [
                        {"label": "Affinity", "value": "affinity"},
                        {"label": "Prediction", "value": "prediction"},
                    ],
                }
            ],
            "operations": [],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "needs_input")
        self.assertEqual(len(audit.operations), 0)

    def test_choice_question_requires_at_least_two_options(self) -> None:
        candidate = {
            "message": "Pick one.",
            "questions": [{"text": "Which?", "kind": "choice", "options": [{"label": "A", "value": "a"}]}],
            "operations": [],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        # A malformed question is a contract violation, not a silent drop: the planner must re-emit
        # a well-formed question, so the round is rejected instead of completing on an empty slate.
        self.assertEqual(audit.state, "complete")
        self.assertTrue(
            any("options must list at least two choices" in issue for issue in audit.issues),
            f"expected a malformed-question issue, got {audit.issues}",
        )

    def test_freeform_question_is_valid(self) -> None:
        candidate = {
            "message": "Tell me the target.",
            "questions": [{"text": "Which target protein?", "kind": "freeform"}],
            "operations": [],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "needs_input")

    def test_choice_question_allow_other_flag_round_trips(self) -> None:
        # allowOther controls the UI's free-text "Other ___" answer next to the option chips. It is
        # optional (absent = enabled) and must survive the audit untouched so the turn result and
        # the frontend see exactly what the planner declared.
        candidate = {
            "message": "Which task type should I set up?",
            "questions": [
                {
                    "text": "Which task type?",
                    "kind": "choice",
                    "allowOther": False,
                    "options": [
                        {"label": "Affinity", "value": "affinity"},
                        {"label": "Prediction", "value": "prediction"},
                    ],
                }
            ],
            "operations": [],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "needs_input")

    def test_choice_question_rejects_non_boolean_allow_other(self) -> None:
        candidate = {
            "message": "Pick one.",
            "questions": [
                {
                    "text": "Which?",
                    "kind": "choice",
                    "allowOther": "yes",
                    "options": [
                        {"label": "A", "value": "a"},
                        {"label": "B", "value": "b"},
                    ],
                }
            ],
            "operations": [],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.state, "complete")
        self.assertTrue(
            any("allowOther must be a boolean" in issue for issue in audit.issues),
            f"expected a malformed allowOther issue, got {audit.issues}",
        )

    def test_confirmation_action_carries_its_own_page_not_turn_origin(self) -> None:
        # A plan emitted from project_list may include a task_list skill. Each confirmation action
        # must carry the page that skill belongs to (its context_type), NOT the page the turn started
        # on — so the frontend renders it on the correct host page and the cross-page plan advances.
        candidate = {
            "message": "Full plan.",
            "questions": [],
            "operations": [
                {"id": "op-create-project", "skill": "host.create_project", "arguments": {}, "depends_on": []},
                {"id": "op-create-task", "skill": "host.create_task", "arguments": {}, "depends_on": ["op-create-project"]},
            ],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        actions = self.harness.build_confirmation_actions(
            audit.operations, plan_id="plan-page", context_type="project_list", workflow_key=""
        )
        by_id = {action["id"]: action for action in actions}
        # projects:create belongs to project_list; tasks:create belongs to task_list — even though
        # the turn originated on project_list.
        self.assertEqual(by_id["host.create_project"]["payload"]["contextType"], "project_list")
        self.assertEqual(by_id["host.create_task"]["payload"]["contextType"], "task_list")
        # Their navigation targets remain distinct.
        self.assertEqual(by_id["host.create_project"]["payload"]["targetContextType"], "task_list")
        self.assertEqual(by_id["host.create_task"]["payload"]["targetContextType"], "task_detail")

    def test_full_write_chain_emitted_in_one_round(self) -> None:
        # After read observations return, the planner must emit the ENTIRE confirmation chain in a
        # single round (create + apply + submit together), not just the first step. This pins the
        # contract that lets a multi-step task complete without the user being left mid-plan.
        # Two observations from prior read rounds (a structure URL and a SMILES).
        observations = {
            "read-structure": {"ok": True, "values": [{"cifUrl": "https://example.org/Q02127.cif"}]},
            "read-compound": {"ok": True, "values": [{"results": [{"smiles": "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"}]}]},
        }
        # A write skill that consumes a structure URL, one that consumes a SMILES, and a submit skill.
        apply_target = CopilotSkillDefinition(
            name="task_detail:apply_docking_target_structure",
            label="Set target",
            description="Set the affinity target structure.",
            input_schema={
                "type": "object",
                "properties": {"structureUrl": {"type": "string", "minLength": 1}},
                "required": ["structureUrl"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        )
        apply_ligand = CopilotSkillDefinition(
            name="task_detail:apply_docking_ligand_smiles",
            label="Set ligand",
            description="Set the affinity ligand SMILES.",
            input_schema={
                "type": "object",
                "properties": {"smiles": {"type": "string", "minLength": 1}},
                "required": ["smiles"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        )
        submit = CopilotSkillDefinition(
            name="task_detail:submit_current",
            label="Start run",
            description="Submit the task.",
            input_schema=EMPTY_OBJECT_SCHEMA,
            effect="execute",
            context_type="task_detail",
        )
        definitions = self.harness.definitions(
            [self.read_def, self.create_project, self.create_task, apply_target, apply_ligand, submit]
        )
        candidate = {
            "message": "Creating the full affinity plan.",
            "questions": [],
            "operations": [
                {"id": "create-project", "skill": "host.create_project", "arguments": {}, "depends_on": []},
                {"id": "create-task", "skill": "host.create_task", "arguments": {}, "depends_on": ["create-project"]},
                {"id": "apply-target", "skill": "task_detail:apply_docking_target_structure",
                 "arguments": {"structureUrl": {"$fromObservation": "read-structure", "field": "cifUrl", "index": 0}},
                 "depends_on": ["create-task"]},
                {"id": "apply-ligand", "skill": "task_detail:apply_docking_ligand_smiles",
                 "arguments": {"smiles": {"$fromObservation": "read-compound", "field": "smiles", "index": 0}},
                 "depends_on": ["create-task"]},
                {"id": "submit", "skill": "task_detail:submit_current", "arguments": {}, "depends_on": ["apply-target", "apply-ligand"]},
            ],
        }
        audit = self.harness.audit_plan(
            candidate, definitions, observations=observations, context_type="project_list"
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "await_confirmation")
        self.assertEqual(len(audit.operations), 5)
        # The $fromObservation references must resolve to the actual retrieved values.
        ops_by_id = {op.operation_id: op for op in audit.operations}
        self.assertEqual(ops_by_id["apply-target"].arguments["structureUrl"], "https://example.org/Q02127.cif")
        self.assertEqual(ops_by_id["apply-ligand"].arguments["smiles"], "CC(C)CC1=CC=C(C=C1)C(C)C(=O)O")


class HierarchicalPlanningTests(unittest.TestCase):
    """Tests for the goal_steps outline protocol: the planner emits an abstract outline, the harness
    accepts it (state=outline), and step-by-step concretization drives the plan turn loop."""

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.write = write_definition()
        self.definitions = self.harness.definitions([self.write])

    def test_goal_steps_with_no_operations_yields_outline_state(self) -> None:
        candidate = {
            "message": "I will plan this in steps.",
            "questions": [],
            "operations": [],
            "goal_steps": [
                {"description": "fetch data"},
                {"description": "configure task"},
                {"description": "submit"},
            ],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "outline")
        self.assertEqual(len(audit.goal_steps), 3)

    def test_goal_steps_with_operations_are_rejected(self) -> None:
        # The outline registers and the accompanying operations are held — the harness drives each
        # step; nothing is silently executed or discarded.
        candidate = {
            "message": "Plan.",
            "questions": [],
            "operations": [
                {"id": "op1", "skill": "host.update", "arguments": {"value": "x"}, "depends_on": []},
            ],
            "goal_steps": [{"description": "do something"}],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        # The outline registers; the accompanying operations are HELD (never executed by the
        # loop in outline state) and re-solicited step by step.
        self.assertEqual(audit.state, "outline")
        self.assertEqual(audit.issues, ())
        self.assertEqual(len(audit.operations), 1)

    def test_goal_steps_with_questions_are_normalized(self) -> None:
        # The QUESTION wins and the outline is dropped (it cannot persist across turns; the
        # next turn re-outlines with the answer). Normalized, not rejected — a hard rejection
        # here looped weaker planners to budget death in real-stack runs.
        candidate = {
            "message": "Plan.",
            "questions": [{"text": "Which?", "kind": "freeform"}],
            "operations": [],
            "goal_steps": [{"description": "do something"}],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.state, "needs_input")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.goal_steps, ())

    def test_goal_step_iterate_declaration_is_rejected(self) -> None:
        # The ``iterate`` declaration was removed because the harness never implemented the fan-out
        # it implied — accepting it let the planner expect behavior that silently never happened.
        # It is now rejected so the planner emits one operation per element explicitly instead.
        candidate = {
            "message": "Plan with iteration.",
            "questions": [],
            "operations": [],
            "goal_steps": [
                {"description": "fetch list"},
                {"description": "process each item", "iterate": {"source": "fetch", "field": "results"}},
            ],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertTrue(
            any("iterate is not supported" in issue for issue in audit.issues),
            f"expected an iterate-rejection issue, got {audit.issues}",
        )

    def test_outline_accepted_even_with_existing_observations(self) -> None:
        # An outline is a legitimate intermediate state even when observations already exist.
        observations = {"r": {"ok": True, "values": [{"results": [{"name": "x", "smiles": "C"}]}]}}
        candidate = {
            "message": "Planning next steps.",
            "questions": [],
            "operations": [],
            "goal_steps": [{"description": "step1"}, {"description": "step2"}],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, observations=observations, context_type="project_list")
        self.assertEqual(audit.state, "outline")
        self.assertEqual(audit.issues, ())

    def test_outline_reemission_with_locked_outline_is_rejected(self) -> None:
        # Direction immutability: once the harness locks the outline, re-emitting goal_steps (even
        # a different plan) is rejected — the planner must concretize the locked steps, not drift.
        candidate = {
            "message": "Redo the plan.",
            "questions": [],
            "operations": [],
            "goal_steps": [{"description": "a different direction"}],
        }
        locked = ({"description": "the locked direction"},)
        audit = self.harness.audit_plan(
            candidate, self.definitions, context_type="project_list", active_outline=locked
        )
        self.assertTrue(
            any("outline is fixed" in issue for issue in audit.issues),
            f"expected an outline-lock issue, got {audit.issues}",
        )
        self.assertEqual(audit.state, "complete")  # no operations and no questions

    def test_operations_round_with_locked_outline_is_accepted(self) -> None:
        # With a locked outline, a normal operations round for the current step is accepted — only
        # goal_steps re-emission is rejected.
        locked = ({"description": "the locked direction"},)
        candidate = {
            "message": "Concretizing.",
            "questions": [],
            "operations": [
                {"id": "w1", "skill": "host.update", "arguments": {"value": "x"}, "depends_on": []},
            ],
        }
        audit = self.harness.audit_plan(
            candidate, self.definitions, context_type="project_list", active_outline=locked
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "await_confirmation")
        self.assertEqual(len(audit.operations), 1)

    def test_planner_output_schema_includes_goal_steps(self) -> None:
        schema = self.harness.planner_output_schema_simple(self.definitions)
        self.assertIn("goal_steps", schema["properties"])


class FabricatedIdentifierAuditTests(unittest.TestCase):
    """The grounding check needs retrieved records, so a zero-tool turn could cite a made-up
    accession / structure id / SMILES and pass. The fabricated-identifier audit closes that hole:
    every recognizable database identifier in a final message must occur in the conversation,
    the context, or the turn's observations — otherwise the message is rejected and corrected.
    """

    def test_fabricated_accession_on_zero_tool_turn_is_caught(self) -> None:
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "The human DHODH sequence (UniProt Q9BQV7, 301 residues) has been applied.",
            "context without that accession",
        )
        self.assertIsNotNone(issue)
        self.assertIn("Q9BQV7", issue)

    def test_identifier_echoed_from_user_message_passes(self) -> None:
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "Resolved Q02127 as requested.", "the user asked about Q02127 earlier"
        )
        self.assertIsNone(issue)

    def test_retrieved_cid_in_prose_form_passes(self) -> None:
        # The message says "CID 134611040" while observations carry the bare numeric field — the
        # audit compares the CID by its numeric body, so a legitimately retrieved CID never trips.
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "PF06882961 -> CID 134611040, SMILES C1CO.", '"cid": "134611040"'
        )
        self.assertIsNone(issue)

    def test_fabricated_cid_is_caught(self) -> None:
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "applied CID 999999999", '"cid": "134611040"'
        )
        self.assertIsNotNone(issue)
        self.assertIn("999999999", issue)

    def test_fabricated_smiles_is_caught(self) -> None:
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "I looked it up and applied SMILES CC1=CC=C2C(=O)C3=C(C=CC3C(=O)C2=C1)OCCO to the ligand.",
            "user said: use brequinar",
        )
        self.assertIsNotNone(issue)
        self.assertIn("CC1=CC=C2C(=O)", issue)

    def test_retrieved_smiles_passes(self) -> None:
        smiles = "CC1=CC=C2C(=O)C3=C(C=CC3C(=O)C2=C1)OCCO"
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            f"applied SMILES {smiles}", f'{{"smiles": "{smiles}"}}'
        )
        self.assertIsNone(issue)

    def test_prose_words_are_not_smiles(self) -> None:
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "ConformationalAnalysis reconsidered thoroughly", "ctx"
        )
        self.assertIsNone(issue)

    def test_digit_tokens_are_not_pdb_ids(self) -> None:
        # A year or a count must never be mistaken for a structure id.
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "In 2026, 403 tests ran", "ctx"
        )
        self.assertIsNone(issue)

    def test_fabricated_pdb_id_is_caught(self) -> None:
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "applied structure 9V34 to the target", "nothing retrieved"
        )
        self.assertIsNotNone(issue)
        self.assertIn("9V34", issue)

    def test_retrieved_pdb_id_passes(self) -> None:
        issue = CopilotSkillHarness.fabricated_identifier_issue(
            "applied 9V34 as target", "pdbId 9V34 retrieved"
        )
        self.assertIsNone(issue)


class InlineToolCallLeakAuditTests(unittest.TestCase):
    """A model may leak its native tool-call markup into the message field instead of using the
    operations array. The harness must reject that round as a protocol violation — otherwise the
    raw markup is shown to the user and no lookup ever runs.
    """

    def setUp(self) -> None:
        self.harness = CopilotSkillHarness(
            skills=OnlineDatabaseSkills(session=requests.Session())
        )
        self.definitions = self.harness.definitions()

    def _audit_message(self, message: str):
        return self.harness.audit_plan(
            {"message": message, "questions": [], "operations": []}, self.definitions
        )

    def test_inline_tool_call_markup_is_rejected(self) -> None:
        for message in (
            'lookup <tool_call> {"skill": "rcsb.search"} </tool_call>',
            "calling <|tool_calls|> now",
            "[TOOL_CALLS] rcsb.search",
            "<function_call>run</function_call>",
        ):
            audit = self._audit_message(message)
            self.assertTrue(
                any("tool-call syntax" in issue for issue in audit.issues),
                f"markup not rejected in: {message}",
            )

    def test_clean_prose_is_never_rejected(self) -> None:
        audit = self._audit_message("Found 3 human DHODH structures in RCSB.")
        self.assertEqual(audit.issues, ())
        audit2 = self._audit_message("The protein binds DNA via a tool call domain.")
        self.assertEqual(audit2.issues, ())


class InputLanguageBoundaryAuditTests(unittest.TestCase):
    """A read skill backed by an English-indexed source must not receive non-English query text:
    the source returns no match, and a silently translated query argument is an unrecorded
    conversion. The one legal path is the atomic conversion — translate.to_english first, then
    query with the recorded English form via $fromObservation.
    """

    def setUp(self) -> None:
        from management_api.copilot_skills.compute_skills import register_compute_skills
        from management_api.copilot_skills.translation import register_translation_skills

        skills = OnlineDatabaseSkills(session=requests.Session())
        register_compute_skills(skills)
        register_translation_skills(skills)
        self.harness = CopilotSkillHarness(skills=skills)
        self.definitions = self.harness.definitions()

    def _audit(self, skill, arguments, observations=None):
        return self.harness.audit_plan(
            {
                "message": "m",
                "questions": [],
                "operations": [{"id": "op1", "skill": skill, "arguments": arguments, "depends_on": []}],
            },
            self.definitions,
            observations=observations,
        )

    def test_non_english_query_rejected_with_translate_guidance(self):
        for skill, key in (
            ("pubchem.search", "identifier"),
            ("uniprot.search", "query"),
            ("rcsb.search", "text"),
        ):
            audit = self._audit(skill, {key: "布洛芬"})
            self.assertTrue(
                any("translate.to_english" in issue for issue in audit.issues), skill
            )

    def test_english_query_and_translate_skill_pass(self):
        self.assertEqual(self._audit("pubchem.search", {"identifier": "ibuprofen"}).issues, ())
        self.assertEqual(
            self._audit("translate.to_english", {"text": "布洛芬", "english": "ibuprofen"}).issues, ()
        )

    def test_schema_token_micro_sign_not_flagged(self):
        audit = self._audit("compute.convert_units", {"value": 5.0, "from": "nM", "to": "µM"})
        self.assertEqual(audit.issues, ())

    def test_translated_observation_chain_passes(self):
        first = self._audit("translate.to_english", {"text": "布洛芬", "english": "ibuprofen"})
        self.assertEqual(first.issues, ())
        observations = self.harness.execute_operations(first.operations)
        follow = {
            "message": "m",
            "questions": [],
            "operations": [
                {
                    "id": "q",
                    "skill": "pubchem.search",
                    "arguments": {"identifier": {"$fromObservation": "op1", "field": "english"}},
                    "depends_on": ["op1"],
                }
            ],
        }
        second = self.harness.audit_plan(follow, self.definitions, observations=observations)
        self.assertEqual(second.issues, ())


class HeldQuestionsProtocolTests(unittest.TestCase):
    """Questions alongside READ operations are coherent: the harness runs the reads first and
    holds the questions (state=continue); the planner re-emits them alone next round. Questions
    alongside CONFIRMATION operations remain rejected.
    """

    def setUp(self) -> None:
        skills = OnlineDatabaseSkills(session=requests.Session())
        self.harness = CopilotSkillHarness(skills=skills)
        write = CopilotSkillDefinition(
            name="host.update", label="Update", description="Update host state.",
            input_schema={"type": "object", "properties": {"value": {"type": "string"}},
                          "required": ["value"], "additionalProperties": False},
            effect="update",
        )
        self.definitions = self.harness.definitions([write])

    def test_questions_with_read_operations_are_held(self):
        candidate = {
            "message": "m",
            "questions": [{"text": "which entry?", "kind": "choice",
                           "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]}],
            "operations": [{"id": "r", "skill": "rcsb.search", "arguments": {"text": "DHODH"}, "depends_on": []}],
        }
        audit = self.harness.audit_plan(candidate, self.definitions)
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "continue")  # reads run; questions held

    def test_questions_with_confirmation_operations_are_held(self) -> None:
        # A confirmation-only round carrying questions: the ACTIONS surface (they end the
        # turn) and the questions are HELD — recorded on the audit (visible, never a silent
        # drop; the post-confirmation continuation is their next window) but not shown with
        # the actions. Real-stack runs showed a hard rejection loops weaker planners to
        # budget death; the visible hold keeps the round's real outcome.
        candidate = {
            "message": "Submit now? And confirm the seed.",
            "questions": [{"text": "Seed ok?", "kind": "confirm"}],
            "operations": [
                {"id": "w1", "skill": "host.update", "arguments": {"value": "x"}, "depends_on": []},
            ],
        }
        audit = self.harness.audit_plan(candidate, self.definitions, context_type="project_list")
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "await_confirmation")
        self.assertEqual(len(audit.operations), 1)
        self.assertEqual(audit.candidate.get("questions"), [])
        self.assertEqual(len(audit.held_questions), 1)

    def test_questions_alone_still_need_input(self):
        candidate = {
            "message": "m",
            "questions": [{"text": "which?", "kind": "choice",
                           "options": [{"label": "A", "value": "a"}, {"label": "B", "value": "b"}]}],
            "operations": [],
        }
        audit = self.harness.audit_plan(candidate, self.definitions)
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "needs_input")


class OutlineIdempotenceTests(unittest.TestCase):
    """Re-emitting the LOCKED outline alongside a step's operations is idempotent (ignored, the
    operations proceed); re-emitting a DIFFERENT outline is drift and rejected.
    """

    def setUp(self) -> None:
        skills = OnlineDatabaseSkills(session=requests.Session())
        self.harness = CopilotSkillHarness(skills=skills)
        self.definitions = self.harness.definitions()
        self.locked = [{"description": "fetch data"}, {"description": "submit"}]

    def _candidate(self, goal_steps):
        return {
            "message": "m",
            "questions": [],
            "operations": [
                {"id": "a", "skill": "rcsb.search", "arguments": {"text": "DHODH"}, "depends_on": []}
            ],
            "goal_steps": goal_steps,
        }

    def test_identical_reemission_is_ignored(self):
        audit = self.harness.audit_plan(self._candidate(list(self.locked)), self.definitions, active_outline=self.locked)
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "continue")
        self.assertEqual(len(audit.operations), 1)

    def test_drifted_reemission_is_rejected(self):
        audit = self.harness.audit_plan(
            self._candidate([{"description": "a different direction"}]), self.definitions, active_outline=self.locked
        )
        self.assertTrue(any("outline is fixed" in issue for issue in audit.issues))


if __name__ == "__main__":
    unittest.main()


class Wave2FabricationRegressionTests(unittest.TestCase):
    """Regression pins for the second deep-review findings: CID matching must accept every
    serialization the allowed text can carry (including compact-JSON cross-turn memory) while
    rejecting shorter fabricated prefixes, and the redaction path must only redact tokens the
    detector itself flags (never plain long words).
    """

    def test_cid_compact_json_memory_form_accepted(self):
        # copilot_memory is serialized with separators=(",", ":") → '"cid":"2244"'
        allowed = '{"copilotMemory":[{"cid":"2244","title":"aspirin"}]}'
        issue = CopilotSkillHarness.fabricated_identifier_issue("PubChem CID 2244", allowed)
        self.assertIsNone(issue)

    def test_cid_ledger_and_prose_forms_accepted(self):
        for allowed in ('cid=2244 retrieved', 'cid 2244 in context', '"cid": "2244"'):
            self.assertIsNone(
                CopilotSkillHarness.fabricated_identifier_issue("CID 2244 applied", allowed)
            )

    def test_shorter_fabricated_cid_prefix_rejected(self):
        # "CID 224" must NOT pass because the allowed text contains "cid 2244" — the digits-only
        # boundary check rejects the prefix (a plain substring match would accept it).
        issue = CopilotSkillHarness.fabricated_identifier_issue("CID 224 applied", "cid 2244 retrieved")
        self.assertIsNotNone(issue)

    def test_iter_tokens_matches_issue_verdict(self):
        # The redaction path consumes iter_fabricated_tokens; its verdict must equal the issue's.
        msg = "applied 9V34 and CID 99999 now"
        allowed = "pdbId 9V34 retrieved"
        tokens = CopilotSkillHarness.iter_fabricated_tokens(msg, allowed)
        self.assertEqual(tokens, ["CID 99999"])
        self.assertIsNotNone(CopilotSkillHarness.fabricated_identifier_issue(msg, allowed))

    def test_redaction_via_iterator_leaves_prose_words(self):
        # Prose like "oligonucleotidesynthesis" is NOT a fabricated SMILES — the unified matcher
        # (dual-marker filter) must keep it, and any redaction built on the matcher keeps it too.
        msg = "We used oligonucleotidesynthesis protocols and polymerasebased methods. See 9ABC."
        allowed = "nothing retrieved"
        tokens = CopilotSkillHarness.iter_fabricated_tokens(msg, allowed)
        self.assertIn("9ABC", tokens)
        self.assertNotIn("oligonucleotidesynthesis", " ".join(tokens))
        for token in tokens:
            self.assertNotIn(token, ("oligonucleotidesynthesis", "polymerasebased"))


class DeferredReferenceAuditTests(unittest.TestCase):
    """Same-round $fromObservation references from a confirmation operation to a READ.

    The planner declares the dataflow (fetch → apply the fetched value) in one round; the audit
    defers the reference (pending_refs) instead of rejecting it. References to anything else —
    unknown ids, earlier-turn ids, same-round WRITE ids, or references FROM a read — stay
    rejected: reads execute immediately and cannot consume a value that does not exist yet.
    """

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.write = write_definition()
        self.definitions = self.harness.definitions([self.write])

    def _candidate(self, *operations: Dict[str, Any]) -> Dict[str, Any]:
        return {"message": "m", "questions": [], "operations": list(operations)}

    def test_same_round_read_reference_is_deferred(self) -> None:
        audit = self.harness.audit_plan(
            self._candidate(
                {"id": "r1", "skill": "registry.read", "arguments": {}, "depends_on": []},
                {
                    "id": "w1", "skill": "host.update",
                    "arguments": {"value": {"$fromObservation": "r1", "field": "smiles", "index": 0}},
                    "depends_on": ["r1"],
                },
            ),
            self.definitions,
            context_type="task_detail",
        )
        self.assertEqual(audit.issues, ())
        write_op = next(op for op in audit.operations if op.operation_id == "w1")
        self.assertEqual(write_op.pending_refs, ("r1",))
        # Raw arguments are kept until the reads execute and materialization runs.
        self.assertIn("$fromObservation", json.dumps(write_op.arguments))

    def test_unknown_reference_is_rejected(self) -> None:
        # With a single read in the round, an unknown reference is UNAMBIGUOUS and rebinds
        # (the real-stack regression: planners invent a semantic id for the lookup they just
        # declared). Rejection survives only when the round cannot identify the target.
        audit = self.harness.audit_plan(
            self._candidate(
                {"id": "r1", "skill": "registry.read", "arguments": {}, "depends_on": []},
                {
                    "id": "w1", "skill": "host.update",
                    "arguments": {"value": {"$fromObservation": "ghost", "field": "smiles", "index": 0}},
                    "depends_on": [],
                },
            ),
            self.definitions,
            context_type="task_detail",
        )
        self.assertEqual(audit.issues, ())
        write_op = next(op for op in audit.operations if op.operation_id == "w1")
        self.assertEqual(write_op.pending_refs, ("r1",))

        ambiguous = self.harness.audit_plan(
            self._candidate(
                {"id": "r1", "skill": "registry.read", "arguments": {}, "depends_on": []},
                {"id": "r2", "skill": "registry.read", "arguments": {}, "depends_on": []},
                {
                    "id": "w1", "skill": "host.update",
                    "arguments": {"value": {"$fromObservation": "ghost", "field": "smiles", "index": 0}},
                    "depends_on": [],
                },
            ),
            self.definitions,
            context_type="task_detail",
        )
        self.assertTrue(any("Unknown observation reference" in issue for issue in ambiguous.issues), ambiguous.issues)

    def test_reference_to_same_round_write_is_rejected(self) -> None:
        # Only READ operations produce observations; a write referencing another write's id has
        # no value to consume and must stay rejected.
        audit = self.harness.audit_plan(
            self._candidate(
                {"id": "w0", "skill": "host.update", "arguments": {"value": "x"}, "depends_on": []},
                {
                    "id": "w1", "skill": "host.update",
                    "arguments": {"value": {"$fromObservation": "w0", "field": "smiles", "index": 0}},
                    "depends_on": ["w0"],
                },
            ),
            self.definitions,
            context_type="task_detail",
        )
        self.assertTrue(any("Unknown observation reference" in issue for issue in audit.issues), audit.issues)

    def test_read_referencing_same_round_read_is_deferred(self) -> None:
        # Reads execute in dependency order with per-wave materialization — the natural
        # "lookup -> compute over the result" chain is ONE round (real-stack regression:
        # rejecting it sent weaker planners into repeat-to-death). The consumer keeps its
        # raw arguments and deferred refs until the dependency's wave completes.
        audit = self.harness.audit_plan(
            self._candidate(
                {"id": "r1", "skill": "registry.read", "arguments": {}, "depends_on": []},
                {
                    "id": "r2", "skill": "registry.read",
                    "arguments": {"value": {"$fromObservation": "r1", "field": "smiles", "index": 0}},
                    "depends_on": ["r1"],
                },
            ),
            self.definitions,
            context_type="task_detail",
        )
        self.assertEqual(audit.issues, ())
        consumer = next(op for op in audit.operations if op.operation_id == "r2")
        self.assertEqual(consumer.pending_refs, ("r1",))

    def test_forward_depends_on_within_round_is_allowed(self) -> None:
        # depends_on may reference a LATER operation of the same round (the declared DAG is
        # order-free); only ids from EARLIER turns or unknown ids are rejected.
        audit = self.harness.audit_plan(
            self._candidate(
                {"id": "w1", "skill": "host.update", "arguments": {"value": "x"}, "depends_on": ["r2"]},
                {"id": "r2", "skill": "registry.read", "arguments": {}, "depends_on": []},
            ),
            self.definitions,
            context_type="task_detail",
        )
        self.assertFalse(any("unknown prior operation" in issue for issue in audit.issues), audit.issues)


class ConfirmationCardSummaryTests(unittest.TestCase):
    """LAYER SEPARATION: the confirmation card shows a USER-facing one-line summary.

    The action's description is the first sentence of the skill description (capped) — the
    full model-facing documentation (boundaries, sourcing rules, workflow notes) stays in the
    protocol catalog where the planner reads it. Rendering the whole doc on the card was the
    "wall of text next to the Apply button" bug.
    """

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.long_desc = (
            "Set the ligand of the current docking task from a SMILES string. In dock mode "
            "the ligand is defined by SMILES alone — no ligand structure file is uploaded; "
            "the docking run places the ligand and computes the binding pose and affinity "
            "together. Pass the smiles from a prior pubchem.search observation, or the SMILES "
            "the user provided directly. Docking tasks only."
        )
        self.ligand = CopilotSkillDefinition(
            name="task_detail:apply_docking_ligand_smiles",
            label="Set docking ligand (SMILES)",
            description=self.long_desc,
            input_schema={
                "type": "object",
                "properties": {"smiles": {"type": "string", "minLength": 1, "maxLength": 2048}},
                "required": ["smiles"],
                "additionalProperties": False,
            },
            effect="update",
            context_type="task_detail",
        )
        self.definitions = self.harness.definitions([self.ligand])

    def test_card_description_is_first_sentence_not_full_doc(self) -> None:
        audit = self.harness.audit_plan(
            {
                "message": "Awaiting confirmation.",
                "questions": [],
                "operations": [
                    {"id": "w1", "skill": "task_detail:apply_docking_ligand_smiles",
                     "arguments": {"smiles": "CCO"}, "depends_on": []},
                ],
            },
            self.definitions,
            context_type="task_detail",
        )
        self.assertEqual(audit.issues, ())
        actions = self.harness.build_confirmation_actions(
            audit.operations, plan_id="p1", context_type="task_detail", workflow_key="affinity"
        )
        self.assertEqual(
            actions[0]["description"],
            "Set the ligand of the current docking task from a SMILES string.",
        )
        self.assertLess(len(actions[0]["description"]), 120)

    def test_summary_falls_back_to_label_for_terse_descriptions(self) -> None:
        self.assertEqual(CopilotSkillHarness.user_facing_summary("Short.", "Do the thing"), "Do the thing")
        self.assertEqual(CopilotSkillHarness.user_facing_summary("", "Do the thing"), "Do the thing")
        # Chinese first sentence terminates at 。
        self.assertEqual(
            CopilotSkillHarness.user_facing_summary("把配体设为给定 SMILES。更多说明。", "L"),
            "把配体设为给定 SMILES。",
        )


class QuestionNormalizationTests(unittest.TestCase):
    """Duplicate option values are NORMALIZED, not rejected (pi: normalize the unambiguous).

    A repeated value is the same choice stated twice — keep the first. Mid-tier planners
    emitted duplicates deterministically and a hard rejection looped them to budget death;
    the deduped question reaches the user as intended. Fewer than two DISTINCT options
    remains a genuine contract violation.
    """

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.definitions = self.harness.definitions([write_definition()])

    def _question_turn(self, question):
        return {
            "message": "Pick one.",
            "questions": [question],
            "operations": [],
        }

    def test_duplicate_option_values_are_deduped(self) -> None:
        audit = self.harness.audit_plan(
            self._question_turn({
                "text": "Which structure?",
                "kind": "choice",
                "options": [
                    {"label": "First", "value": "8YGY"},
                    {"label": "Repeat", "value": "8YGY"},
                    {"label": "Other", "value": "1SPJ"},
                ],
            }),
            self.definitions,
            context_type="project_list",
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "needs_input")
        self.assertEqual(len(audit.candidate["questions"][0]["options"]), 2)

    def test_all_duplicate_values_still_rejected(self) -> None:
        # Only ONE distinct option remains — genuinely insufficient for a choice.
        audit = self.harness.audit_plan(
            self._question_turn({
                "text": "Which?",
                "kind": "choice",
                "options": [
                    {"label": "A", "value": "x"},
                    {"label": "B", "value": "x"},
                ],
            }),
            self.definitions,
            context_type="project_list",
        )
        self.assertTrue(any("at least two choices" in issue for issue in audit.issues))


class ReferenceShorthandTests(unittest.TestCase):
    """The compact string reference form is NORMALIZED to the canonical object form.

    Planners naturally emit "$fromObservation:<id>.<field>" — unambiguous intent, so the
    harness rewrites it at the audit entry instead of letting a literal string fail schema
    validation (which mid-tier models repeat deterministically until budget death).
    """

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.definitions = self.harness.definitions([write_definition()])

    def test_shorthand_string_becomes_object_reference(self) -> None:
        self.assertEqual(
            CopilotSkillHarness._normalize_reference_shorthand("$fromObservation:op1.pdbId"),
            {"$fromObservation": "op1", "field": "pdbId"},
        )
        self.assertEqual(
            CopilotSkillHarness._normalize_reference_shorthand("$fromObservation:op1.results[2]"),
            {"$fromObservation": "op1", "field": "results", "index": 2},
        )
        # Non-reference strings pass through untouched.
        self.assertEqual(
            CopilotSkillHarness._normalize_reference_shorthand("CCOc1ccccc1"),
            "CCOc1ccccc1",
        )

    def test_shorthand_in_arguments_materializes_and_passes_audit(self) -> None:
        observations = {
            "rcsb_klk": {"ok": True, "values": [{"results": [{"pdbId": "8YGY"}]}]},
        }
        audit = self.harness.audit_plan(
            {
                "message": "Creating task.",
                "questions": [],
                "operations": [
                    {"id": "w1", "skill": "host.update",
                     "arguments": {"value": "$fromObservation:rcsb_klk.pdbId"},
                     "depends_on": ["rcsb_klk"]},
                ],
            },
            self.definitions,
            observations=observations,
            context_type="task_list",
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.operations[0].arguments["value"], "8YGY")


class DependencyCycleAuditTests(unittest.TestCase):
    """A cyclic depends_on graph is rejected at AUDIT time with an actionable issue — it used
    to pass reference checks and blow up execute_operations mid-turn (misrouted as HTTP 400).
    Diamonds (shared ancestors) are legal and must NOT be flagged."""

    def setUp(self) -> None:
        self.skills = RecordingSkills()
        self.harness = CopilotSkillHarness(skills=self.skills)
        self.definitions = self.harness.definitions([write_definition()])

    def test_cycle_is_rejected_with_actionable_issue(self) -> None:
        audit = self.harness.audit_plan(
            {
                "message": "m",
                "questions": [],
                "operations": [
                    {"id": "a", "skill": "host.update", "arguments": {"value": "x"}, "depends_on": ["b"]},
                    {"id": "b", "skill": "host.update", "arguments": {"value": "y"}, "depends_on": ["a"]},
                ],
            },
            self.definitions,
            context_type="project_list",
        )
        self.assertTrue(any("dependency cycle" in issue for issue in audit.issues), audit.issues)

    def test_diamond_dependency_is_not_a_cycle(self) -> None:
        audit = self.harness.audit_plan(
            {
                "message": "m",
                "questions": [],
                "operations": [
                    {"id": "root", "skill": "host.update", "arguments": {"value": "r"}, "depends_on": []},
                    {"id": "left", "skill": "host.update", "arguments": {"value": "l"}, "depends_on": ["root"]},
                    {"id": "right", "skill": "host.update", "arguments": {"value": "ri"}, "depends_on": ["root"]},
                    {"id": "top", "skill": "host.update", "arguments": {"value": "t"}, "depends_on": ["left", "right"]},
                ],
            },
            self.definitions,
            context_type="project_list",
        )
        self.assertFalse(any("cycle" in issue for issue in audit.issues), audit.issues)


class OutlineDriftNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = CopilotSkillHarness(skills=RecordingSkills())
        self.definitions = self.harness.definitions([write_definition()])

    """Production regression (transcript 18:43): the model re-emitted the LOCKED four-step
    outline with cosmetic edits (whitespace, a trailing 。, punctuation tweaks) and the
    byte-equality drift check looped it to budget death — killing a fully-valid docking chain
    at the apply step. Drift is now judged on alphanumeric skeletons: cosmetic re-emissions
    are idempotent echoes; genuine re-plans still reject."""

    def test_cosmetic_reemission_is_idempotent(self):
        harness = self.harness
        definitions = self.definitions
        locked = ({"description": "设置靶标结构 8YGY"}, {"description": "设置配体 SMILES"},
                  {"description": "设置口袋搜索框"}, {"description": "提交运行"})
        echoed = ({"description": "设置靶标结构 8YGY。"}, {"description": "设置 配体 SMILES"},
                  {"description": "设置口袋搜索框!"}, {"description": "提交运行。"})
        candidate = {
            "message": "执行步骤。",
            "questions": [],
            "operations": [],
            "goal_steps": list(echoed),
        }
        audit = harness.audit_plan(
            candidate, definitions, context_type="task_detail",
            active_outline=locked,
        )
        self.assertEqual(audit.issues, ())
        self.assertEqual(audit.state, "complete")

    def test_genuine_replan_still_rejects(self):
        harness = self.harness
        definitions = self.definitions
        locked = ({"description": "设置靶标结构"}, {"description": "提交运行"})
        replanned = ({"description": "改用别的结构来源"}, {"description": "提交运行"})
        candidate = {
            "message": "改计划。",
            "questions": [],
            "operations": [],
            "goal_steps": list(replanned),
        }
        audit = harness.audit_plan(
            candidate, definitions, context_type="task_detail",
            active_outline=locked,
        )
        self.assertTrue(any("differ from the locked outline" in issue for issue in audit.issues))


class ProtocolPromptBudgetTests(unittest.TestCase):
    """Deterministic context-budget gate for the rendered tool catalog.

    render_protocol_prompt carries the full tool catalog on EVERY planner round. Unbounded
    skill descriptions would silently eclipse the context-payload budget (the runtime logs
    an error but never kills a turn for this soft budget). This test is the hard gate: it
    renders the PRODUCTION catalog (registered read skills + every context's action skills
    across every workflow) and fails CI when the worst case exceeds MAX_PROTOCOL_PROMPT_CHARS
    — adding a skill is a conscious budget decision, not a silent drift.
    """

    def test_production_catalog_fits_protocol_budget(self):
        import requests as _requests

        from management_api.copilot import CAPABILITY_CATALOG_SKILL, _read_registered_capability_catalog
        from management_api.copilot_capabilities import build_cross_context_skill_definitions
        from management_api.copilot_skill_harness import (
            MAX_PROTOCOL_PROMPT_CHARS,
            CopilotSkillHarness,
        )
        from management_api.copilot_skills.compute_skills import register_compute_skills
        from management_api.copilot_skills.online_databases import (
            OnlineDatabaseSkills,
            OnlineSkillDefinition,
        )
        from management_api.copilot_skills.translation import register_translation_skills
        from management_api.copilot_skills.workflows import WORKFLOW_KEYS

        # PRODUCTION-identical registration: the assistant additionally registers the
        # capability catalog read skill plus compute and translation skills on top of the
        # online-database defaults (CopilotAssistant.__init__). Measuring a bare
        # OnlineDatabaseSkills harness measured the wrong catalog and let the production
        # protocol drift past the budget while this test stayed green.
        skills = OnlineDatabaseSkills(session=_requests.Session(), timeout_seconds=10)
        skills.register(
            OnlineSkillDefinition(
                name=CAPABILITY_CATALOG_SKILL,
                description=(
                    "Read the canonical platform workflow, operation, input, parameter, and option catalog "
                    "from the registered schemas before answering what the platform supports."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            _read_registered_capability_catalog,
        )
        register_compute_skills(skills)
        register_translation_skills(skills)
        harness = CopilotSkillHarness(skills=skills)
        worst = 0
        worst_label = ""
        for context_type in ("project_list", "task_list", "task_detail"):
            for workflow_key in ("",) + WORKFLOW_KEYS:
                definitions = harness.definitions(
                    build_cross_context_skill_definitions(
                        current_context=context_type,
                        context_payload={},
                        workflow_key=workflow_key,
                    )
                )
                size = len(harness.render_protocol_prompt(definitions))
                if size > worst:
                    worst = size
                    worst_label = f"{context_type}/{workflow_key or 'default'}"
        self.assertLessEqual(
            worst,
            MAX_PROTOCOL_PROMPT_CHARS,
            f"production tool catalog renders {worst} chars at {worst_label} "
            f"(budget {MAX_PROTOCOL_PROMPT_CHARS}): the protocol now eclipses the context "
            "payload budget. Shrink the new skill descriptions or introduce two-tier "
            "contract disclosure (A/B-validated via e2e_diag_copilot.py first).",
        )

    def test_summary_mode_catalog_with_contract_skill_fits_budget(self):
        """Summary tier + the skills.contract on-demand entry must also stay in budget.

        The A/B keeps full as the default, but summary mode is a supported constructor
        option; its catalog (compact read tools PLUS the extra skills.contract skill)
        is pinned here so a future model upgrade that flips the default already has a
        working budget gate.
        """
        import requests as _requests

        from management_api.copilot import CAPABILITY_CATALOG_SKILL, SKILLS_CONTRACT_SKILL, _read_registered_capability_catalog
        from management_api.copilot_capabilities import build_cross_context_skill_definitions
        from management_api.copilot_skill_harness import (
            MAX_PROTOCOL_PROMPT_CHARS,
            CopilotSkillHarness,
        )
        from management_api.copilot_skills.compute_skills import register_compute_skills
        from management_api.copilot_skills.online_databases import (
            OnlineDatabaseSkills,
            OnlineSkillDefinition,
        )
        from management_api.copilot_skills.translation import register_translation_skills
        from management_api.copilot_skills.workflows import WORKFLOW_KEYS

        skills = OnlineDatabaseSkills(session=_requests.Session(), timeout_seconds=10)
        skills.register(
            OnlineSkillDefinition(
                name=CAPABILITY_CATALOG_SKILL,
                description=(
                    "Read the canonical platform workflow, operation, input, parameter, and option catalog "
                    "from the registered schemas before answering what the platform supports."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
            _read_registered_capability_catalog,
        )
        skills.register(
            OnlineSkillDefinition(
                name=SKILLS_CONTRACT_SKILL,
                description=(
                    "Load the FULL description and argument contract of the named read skills "
                    "(their query-language rules, units, and semantics). Read it before "
                    "calling a skill whose summary line does not settle how to shape the query."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "skills": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Skill names; omit or empty for all read skills.",
                        }
                    },
                    "additionalProperties": False,
                },
            ),
            lambda _arguments: {},
        )
        register_compute_skills(skills)
        register_translation_skills(skills)
        harness = CopilotSkillHarness(skills=skills)

        worst = 0
        worst_label = ""
        for context_type in ("project_list", "task_list", "task_detail"):
            for workflow_key in ("",) + WORKFLOW_KEYS:
                definitions = harness.definitions(
                    build_cross_context_skill_definitions(
                        current_context=context_type,
                        context_payload={},
                        workflow_key=workflow_key,
                    )
                )
                size = len(harness.render_protocol_prompt(definitions, read_disclosure="summary"))
                if size > worst:
                    worst = size
                    worst_label = f"{context_type}/{workflow_key or 'default'}"
        self.assertLessEqual(worst, MAX_PROTOCOL_PROMPT_CHARS)

    def test_skill_contracts_handler_returns_full_tier_on_demand(self):
        """_read_skill_contracts: named skills get FULL descriptions; empty = all; unknown = empty entry."""
        from management_api.copilot import _read_skill_contracts
        from management_api.copilot_skill_harness import CopilotSkillDefinition

        read = CopilotSkillDefinition(
            name="db.demo",
            description="Search the demo database by protein name. Query text must be English.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "English name."}},
                "required": ["query"],
                "additionalProperties": False,
            },
            effect="read",
        )
        write = CopilotSkillDefinition(
            name="host.update",
            description="Apply a host update.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect="update",
        )
        definitions = {read.name: read, write.name: write}

        named = _read_skill_contracts({"skills": ["db.demo"]}, definitions)
        self.assertEqual(list(named["contracts"]), ["db.demo"])
        # FULL tier: property descriptions present (they were dropped from the summary tier).
        self.assertIn("English name.", named["contracts"]["db.demo"]["input_schema"]["properties"]["query"]["description"])

        everything = _read_skill_contracts({}, definitions)
        # Empty request = the full passed catalog. In production the assistant's handler
        # passes the REGISTRY definitions (read skills only); host write skills never
        # enter that registry, so they never appear through the on-demand channel.
        self.assertEqual(list(everything["contracts"]), ["db.demo", "host.update"])

        unknown = _read_skill_contracts({"skills": ["nope"]}, definitions)
        self.assertEqual(unknown["contracts"], {})


class TwoTierDisclosureTests(unittest.TestCase):
    """Summary-mode protocol rendering (progressive disclosure's compact tier).

    The A/B decision (2026-09, gemma4-31b, load-bearing scenarios): summary mode degraded
    sequence-by-name from retrieve-and-patch to asking — FULL stays the default. These tests
    pin the summary renderer's mechanical contract so the mode remains correct and available
    (35% protocol saving) for a future model upgrade's A/B rerun.
    """

    def setUp(self) -> None:
        from management_api.copilot_skill_harness import CopilotSkillDefinition, CopilotSkillHarness

        self.harness = CopilotSkillHarness(skills=RecordingSkills())
        self.read = CopilotSkillDefinition(
            name="db.demo",
            description=(
                "Search the demo database by protein name. Query text must be the official "
                "name in English; common names are translated first. Long results are capped."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Official English protein name."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            effect="read",
        )
        self.definitions = self.harness.definitions([self.read])

    def test_summary_tier_is_compact_first_sentence_plus_shape(self):
        full = self.harness.render_protocol_prompt(self.definitions, read_disclosure="full")
        summary = self.harness.render_protocol_prompt(self.definitions, read_disclosure="summary")
        self.assertLess(len(summary), len(full))
        # First sentence only — the language/units contract text lives in the full tier.
        self.assertIn("Search the demo database by protein name.", summary)
        self.assertNotIn("common names are translated", summary)
        # Schema keeps names/types/enums/required, drops per-property descriptions.
        self.assertIn("\"query\"", summary)
        self.assertIn("\"integer\"", summary)
        self.assertNotIn("Official English protein name", summary)

    def test_full_tier_unchanged_by_default(self):
        prompt = self.harness.render_protocol_prompt(self.definitions)
        self.assertIn("common names are translated", prompt)
        self.assertIn("Official English protein name", prompt)
