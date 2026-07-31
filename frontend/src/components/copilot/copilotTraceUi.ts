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
 */
export function collectCopilotMemory(
  messages: ProjectCopilotMessage[],
  sessionId: string,
  cap = 20
): Record<string, unknown>[] {
  // Walk messages chronologically (messages are in order), gathering this session's assistant
  // observations in retrieval order, then keep only the newest ``cap`` records.
  const chronological: Record<string, unknown>[] = [];
  for (const message of messages) {
    if (readSessionId(message) !== sessionId || message.role !== 'assistant') continue;
    const observations = message.metadata?.planner_observations;
    if (!Array.isArray(observations)) continue;
    for (const record of observations) {
      if (record && typeof record === 'object' && !Array.isArray(record)) {
        chronological.push(record as Record<string, unknown>);
      }
    }
  }
  return chronological.length > cap ? chronological.slice(chronological.length - cap) : chronological;
}

/** Render a trace step as one concise, general phrase. Generic — any skill maps to the same UI. */
export function formatTraceStep(step: CopilotTraceStep): string {
  const detail = (step.detail && typeof step.detail === 'object' ? step.detail : {}) as Record<string, unknown>;
  switch (step.event) {
    case 'model_request': {
      const usage = detail.usage;
      const tokens = usage && typeof usage === 'object'
        ? [(usage as Record<string, unknown>).input_tokens, (usage as Record<string, unknown>).output_tokens].filter(
            (v): v is number => typeof v === 'number'
          )
        : [];
      return tokens.length === 2 ? `Considered the request (${tokens[0]}→${tokens[1]} tokens)` : 'Considered the request';
    }
    case 'malformed_output':
      return 'Retried after a malformed response';
    case 'audit_rejected': {
      const issues = Array.isArray(detail.issues) ? detail.issues.filter((item) => typeof item === 'string') : [];
      return issues.length ? `Revised plan: ${String(issues[0])}` : 'Revised the plan after a check';
    }
    case 'skill_observations': {
      const observations = Array.isArray(detail.observations) ? detail.observations : [];
      const parts = observations
        .map((item) => {
          if (!item || typeof item !== 'object') return '';
          const observation = item as Record<string, unknown>;
          const skill = String(observation.skill || 'a database').replace(/^.*\./, '');
          const ok = observation.ok === true;
          const count = Number(observation.count);
          const success = Number(observation.successCount);
          const label = skill || 'lookup';
          if (!ok) return `${label}: no result`;
          const countLabel = Number.isInteger(count) && count > 0 ? `${Number.isInteger(success) ? success : count} result(s)` : 'ok';
          return `${label}: ${countLabel}`;
        })
        .filter(Boolean);
      return parts.length ? `Looked up — ${parts.join('; ')}` : 'Ran a lookup';
    }
    case 'fallback':
      return 'Answered conversationally';
    case 'terminal':
      return `Finalized${detail.state ? ` (${String(detail.state)})` : ''}`;
    case 'no_convergence':
      return 'Could not settle on a plan';
    default:
      return step.event;
  }
}
