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

/** Render a trace step as one short, user-friendly phrase. Generic — any skill maps to the same UI.
 *  Deliberately hides developer-only detail (token counts, internal skill names, audit issue text,
 *  terminal state codes): the reasoning panel is for the user, not the developer. */
export function formatTraceStep(step: CopilotTraceStep): string {
  switch (step.event) {
    case 'model_request':
      return 'Analyzing request';
    case 'malformed_output':
      return 'Regenerating';
    case 'audit_rejected':
      return 'Adjusting plan';
    case 'fallback':
      return 'Composing answer';
    case 'terminal':
      return 'Done';
    case 'no_convergence':
      return 'Could not settle on a plan';
    case 'skill_observations': {
      const detail = (step.detail && typeof step.detail === 'object' ? step.detail : {}) as Record<string, unknown>;
      const observations = Array.isArray(detail.observations) ? detail.observations : [];
      let total = 0;
      for (const item of observations) {
        if (!item || typeof item !== 'object') continue;
        const observation = item as Record<string, unknown>;
        if (observation.ok !== true) continue;
        const success = Number(observation.successCount);
        const count = Number(observation.count);
        total += Number.isInteger(success) ? success : Number.isInteger(count) ? count : 0;
      }
      return total > 0 ? `Found ${total} result${total === 1 ? '' : 's'}` : 'No results found';
    }
    default:
      return step.event;
  }
}
