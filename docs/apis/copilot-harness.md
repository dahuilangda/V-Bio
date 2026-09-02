# Copilot Harness — Architecture Notes

The Copilot harness follows the engineering philosophy of minimal agent harnesses
(pi-agent / Mario Zechner's `earendil-works/pi`): a small typed tool surface, the
registry as the single source of truth, event-based observability, and a harness
that executes/audits while the model owns direction. The system prompt is a
deliberate exception (see below). Scope is deliberately simpler than pi: V-Bio
owns all its capabilities — there is no third-party extension mechanism to build.

## Concept mapping (pi → V-Bio)

| pi concept | V-Bio implementation |
|---|---|
| Typed tool definition (name + description + schema + execute) | `CopilotSkillDefinition` / `OnlineSkillDefinition` (`management_api/copilot_skill_harness.py`). The description IS the model-facing documentation; the schema is the audit contract. |
| Tool registry, no snapshots | `OnlineDatabaseSkills.register()` is the single source of truth; `CopilotSkillHarness._read_definitions()` derives the catalog LIVE per turn — registering a skill makes it immediately executable AND auditable. |
| Skills as data (SKILL.md frontmatter) | Host page actions are declarative data: `TASK_LIST_ACTION_SCHEMAS`, `PROJECT_LIST_ACTION_SCHEMAS` (`copilot_skills/`), workflow skills from the registered capability schemas (`copilot_capabilities.py`). Adding a V-Bio capability = declaring a skill, no loop changes. |
| Progressive disclosure (read docs on demand) | Only the current page's action skills + the universal read catalog are exposed per turn (`build_cross_context_skill_definitions`); the frontend renders an action only on its own page (`payload.contextType`) and navigates on confirmation, so multi-page goals advance turn by turn. |
| Event-emitting agent loop, full observability | `PlannerTrace` (OTel-GenAI-style event names) streams every round via SSE (`copilot_trace.py`, `copilot_stream.py`). |
| Conformance tests | `tests/test_copilot_protocol.py` (harness contract), `management_api/copilot_eval.py` (scored gate), `server/e2e_copilot_matrix.py` + `e2e_copilot_multiturn.py` (real-model E2E; the matrix enforces "no turn may end `failed`"). |

## Every V-Bio capability is a skill

Including frontend unit operations: `tasks:create`, `task_detail:apply_parameter_patch`,
`task_detail:apply_docking_target_structure`, … are skills like any lookup — declared
with a JSON schema, audited before execution, and applied by the HOST only after the
user confirms (the read/write split below).

## Deliberate deviations from pi (justified by V-Bio's shape)

- **Read/write split.** pi YOLO-executes every tool. V-Bio mutations run in the user's
  browser session, so write skills become user-confirmation actions applied by the host
  page; only read skills execute server-side. Deferred materialization completes a
  same-round `read → write` dataflow: the harness executes reads, materializes
  `$fromObservation` references, and surfaces the actions in one round.
- **Bounded rounds.** pi has no max-steps knob (interactive CLI). A web request must
  terminate: `round_budget` scales with the plan outline and failures end honestly
  (`state="failed"`), never as a fake answer.
- **Structural audits beyond pi.** Grounding (answers must name retrieved records),
  fabrication (identifiers must come from a source), candidate-choice (a write may not
  silently apply one entry of a multi-record search — one-shot reconsideration, no
  deadlock). These exist because the planner model in deployment is mid-tier, not
  frontier; each guard is data-driven, never domain-hardcoded.
- **System prompt.** pi's sub-1000-token prompt assumes a frontier model. A/B evidence
  (real model, 3 reps × 3 scenarios): the slim prompt scored 4/9 vs 9/9 for the explicit
  one on the deployed model. The explicit prompt is kept and guarded by
  `test_system_prompt_load_bearer_sections_are_present`; re-evaluate with the A/B
  harness (`server/e2e_diag_copilot.py`) after a model upgrade.
- **Turn-scoped state.** pi persists JSONL sessions; V-Bio keeps server state per turn
  and carries cross-turn context via the frontend (`copilot_memory`,
  `recent_action_resolutions`), which already persists the conversation.

## Adding a capability

1. Declare the skill (registry for reads, declarative schema / capability builder for
   host actions): name, description (what it returns + boundaries + constraints per
   argument), `input_schema`, `effect`.
2. The audit, protocol rendering, confirmation flow, and trace pick it up
   automatically — no loop edits.
3. Add scenarios to the E2E matrix if it changes user-visible behavior.
