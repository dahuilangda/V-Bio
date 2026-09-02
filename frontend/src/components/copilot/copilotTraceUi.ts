/** Pure helpers for rendering/planning Copilot trace + memory UI.

Extracted from the modal component so the logic is unit-testable without React/DOM and lives in one
place. Domain-agnostic: these functions never reference specific compounds, proteins, or skills —
they only shape the planner's generic trace/memory data.
*/

import type { CopilotTraceStep, ProjectCopilotMessage } from '../../types/models';

/** Read a message's session id, falling back to 'default' (mirrors the backend session scope). */
export function readSessionId(message: ProjectCopilotMessage): string {
  const value = message.metadata?.session_id;
  const normalized = typeof value === 'string' ? value.trim() : '';
  return normalized || 'default';
}

/** Validate a planner trace defensively — observability must never break rendering. */
export function readPlannerTrace(value: unknown): CopilotTraceStep[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is CopilotTraceStep => {
    if (!item || typeof item !== 'object') return false;
    const step = item as Record<string, unknown>;
    return typeof step.event === 'string' && step.event.trim() !== '' && Number.isInteger(Number(step.round));
  });
}

/**
 * Collect the compact records retrieved in the session's recent assistant turns, to carry forward
 * as copilot_memory so a follow-up about a previously retrieved entity need not re-search. Capped to
 * match the backend's per-turn record cap; most-recent turns win, kept chronologically.
 *
 * Long fields (sequence, SMILES) are DROPPED from memory entirely — never prefix-truncated
 * (the backend's _compact_memory_records contract): a 60-char SMILES prefix is structurally
 * INVALID data, one weak paste away from a write action's parameters, which the schema
 * (string, ≤2048) cannot catch. The full values are still shown in the 'Retrieved data'
 * card via the display path; the planner only needs the identity to recognize a
 * previously retrieved entity, and can re-retrieve exact values when needed.
 */
export function collectCopilotMemory(
  messages: ProjectCopilotMessage[],
  sessionId: string,
  cap = 20
): Record<string, unknown>[] {
  const LONG_FIELDS = new Set(['sequence', 'smiles']);
  // Walk messages chronologically (messages are in order), gathering this session's assistant
  // observations in retrieval order, then keep only the newest ``cap`` records.
  const chronological: Record<string, unknown>[] = [];
  for (const message of messages) {
    if (readSessionId(message) !== sessionId || message.role !== 'assistant') continue;
    const observations = message.metadata?.planner_observations;
    if (!Array.isArray(observations)) continue;
    for (const record of observations) {
      if (record && typeof record === 'object' && !Array.isArray(record)) {
        // Memory carries IDENTITY, never full data: long identity fields are dropped, and
        // EVERY string caps at 80 — external record text (titles, names) is untrusted
        // data, and memory re-injects it into the next turn's context, so it must not
        // carry hostile instruction-shaped payloads across turns either.
        const compact: Record<string, unknown> = {};
        for (const [key, val] of Object.entries(record as Record<string, unknown>)) {
          if (LONG_FIELDS.has(key) && typeof val === 'string' && val.length > 60) {
            continue;
          } else if (typeof val === 'string' && val.length > 80) {
            compact[key] = val.slice(0, 80);
          } else {
            compact[key] = val;
          }
        }
        chronological.push(compact);
      }
    }
  }
  return chronological.length > cap ? chronological.slice(chronological.length - cap) : chronological;
}

/** Map a skill name to a human-readable source label. The skill names use dotted namespaces
 *  (uniprot.search, pubchem.search, rcsb.resolve, etc.); we surface the source name so the user
 *  sees WHERE data came from, not just "Found 1 result". */
function skillLabel(skill: unknown): string {
  const name = String(skill || '').trim();
  if (!name) return '';
  // Take the part before the dot, capitalize: "uniprot.search" → "UniProt"
  const source = name.split('.')[0];
  if (!source) return name;
  return source.charAt(0).toUpperCase() + source.slice(1);
}

/** Extract a short query/description from an observation's first item for context. */
function observationQuery(observation: Record<string, unknown>): string {
  const items = Array.isArray(observation.items) ? observation.items : [];
  const first = items[0];
  if (!first || typeof first !== 'object') return '';
  const args = (first as Record<string, unknown>).arguments;
  if (!args || typeof args !== 'object') return '';
  const argMap = args as Record<string, unknown>;
  const query = String(argMap.query || argMap.identifier || argMap.accession || argMap.name || argMap.text || '').trim();
  return query ? `"${query}"` : '';
}

/** Render a trace step as one short, descriptive phrase. Extracts context from the detail payload
 *  so the user sees WHAT happened and WHERE, not just generic labels. */
export function formatTraceStep(step: CopilotTraceStep): string {
  const detail = (step.detail && typeof step.detail === 'object' ? step.detail : {}) as Record<string, unknown>;
  switch (step.event) {
    case 'model_request': {
      const round = step.round;
      return round === 0 ? 'Understanding your request' : 'Refining the plan';
    }
    case 'malformed_output':
      return 'Reformatting response';
    case 'audit_rejected':
      return 'Adjusting approach';
    case 'terminal': {
      const state = String(detail.state || '');
      if (state === 'await_confirmation') return 'Ready for your confirmation';
      if (state === 'needs_input') return 'Waiting for your input';
      if (state === 'outline') return 'Plan outlined';
      if (state === 'failed') return 'Could not complete the plan';
      return 'Complete';
    }
    case 'no_convergence':
      // Mirrors the backend's honest failure copy: the request is fine, the planner failed —
      // the final message names the actual blocker. Never tell the user to rephrase.
      return 'Could not settle on a plan — the reply explains what broke';
    case 'outline': {
      const steps = Array.isArray(detail.steps) ? detail.steps.length : 0;
      return steps > 0 ? `Outlined ${steps}-step plan` : 'Planning';
    }
    case 'step_done': {
      const desc = String(detail.description || '').trim();
      if (desc) return `✓ ${desc}`;
      const materialized = Number(detail.materialized) || 0;
      if (materialized > 0) return `✓ Prepared ${materialized} action${materialized === 1 ? '' : 's'} from retrieved data`;
      return '✓ Step complete';
    }
    case 'writes_materialized': {
      const n = Number(detail.materialized) || 0;
      return `✓ Prepared ${n} action${n === 1 ? '' : 's'} from retrieved data`;
    }
    case 'skill_observations': {
      const observations = Array.isArray(detail.observations) ? detail.observations : [];
      // Build a descriptive label: "Searched UniProt for "DHODH" — found 1 result"
      const parts: string[] = [];
      let total = 0;
      for (const item of observations) {
        if (!item || typeof item !== 'object') continue;
        const observation = item as Record<string, unknown>;
        const source = skillLabel(observation.skill);
        const query = observationQuery(observation);
        if (observation.ok !== true) {
          parts.push(`${source} unavailable${query ? ` (${query})` : ''}`);
          continue;
        }
        const success = Number(observation.successCount);
        const count = Number(observation.count);
        const n = Number.isInteger(success) ? success : Number.isInteger(count) ? count : 0;
        total += n;
        const label = n > 0 ? `${source}: ${n} result${n === 1 ? '' : 's'}` : `${source}: no match`;
        parts.push(query ? `${label} for ${query}` : label);
      }
      if (parts.length === 0) return 'No results found';
      return parts.join('; ');
    }
    default:
      return step.event;
  }
}
