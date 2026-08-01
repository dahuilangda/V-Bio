import { API_HEADERS, fetchWithTimeout, managementApiUrl, requestManagement } from './backendClient';
import type { CopilotPlanAction, CopilotTraceStep } from '../types/models';

const MAX_CONTEXT_STRING_CHARS = 1600;
const MAX_CONTEXT_LIST_ITEMS = 40;
const MAX_CONTEXT_DICT_KEYS = 80;
const REDACTED_FILE_TEXT_KEYS = new Set([
  'content',
  'structure_text',
  'structuretext',
  'cif_text',
  'pdb_text',
  'sdf_text',
  'mol_text',
  'file_content',
  'filecontent',
  'raw',
  'blob',
  'bytes',
  'data'
]);
const FILE_METADATA_KEYS = new Set([
  'filename',
  'file_name',
  'format',
  'type',
  'mimetype',
  'size',
  'chainid',
  'chainids',
  'template_chain_id',
  'templatechainid',
  'target_chain_ids',
  'targetchainids'
]);

function normalizeContextKey(key: unknown): string {
  return String(key || '').trim().toLowerCase().replace(/[^a-z0-9_]/g, '');
}

function looksLikeFilePayload(value: Record<string, unknown>): boolean {
  return Object.keys(value).some((key) => FILE_METADATA_KEYS.has(normalizeContextKey(key)));
}

function compactContextString(value: string): string {
  if (value.length <= MAX_CONTEXT_STRING_CHARS) return value;
  return `${value.slice(0, MAX_CONTEXT_STRING_CHARS)}... [truncated, original_chars=${value.length}]`;
}

function sanitizeCopilotContextValue(value: unknown, depth = 0, parent: Record<string, unknown> | null = null, key?: unknown): unknown {
  if (depth > 8) return '[truncated: max depth reached]';
  const normalizedKey = normalizeContextKey(key);
  if (typeof value === 'string') {
    if (
      REDACTED_FILE_TEXT_KEYS.has(normalizedKey) &&
      (!parent || looksLikeFilePayload(parent) || value.length > MAX_CONTEXT_STRING_CHARS)
    ) {
      return `[omitted file/text payload, chars=${value.length}]`;
    }
    return compactContextString(value);
  }
  if (value === null || typeof value === 'boolean' || typeof value === 'number') return value;
  if (Array.isArray(value)) {
    const rows = value.slice(0, MAX_CONTEXT_LIST_ITEMS).map((item) => sanitizeCopilotContextValue(item, depth + 1));
    if (value.length > MAX_CONTEXT_LIST_ITEMS) rows.push({ _truncated_items: value.length - MAX_CONTEXT_LIST_ITEMS });
    return rows;
  }
  if (typeof value === 'object') {
    const input = value as Record<string, unknown>;
    const output: Record<string, unknown> = {};
    const entries = Object.entries(input);
    for (const [index, [childKey, childValue]] of entries.entries()) {
      if (index >= MAX_CONTEXT_DICT_KEYS) {
        output._truncated_keys = entries.length - MAX_CONTEXT_DICT_KEYS;
        break;
      }
      output[childKey] = sanitizeCopilotContextValue(childValue, depth + 1, input, childKey);
    }
    return output;
  }
  return compactContextString(String(value || ''));
}

function sanitizeCopilotContextPayload(payload: Record<string, unknown>): Record<string, unknown> {
  const sanitized = sanitizeCopilotContextValue(payload);
  return sanitized && typeof sanitized === 'object' && !Array.isArray(sanitized) ? sanitized as Record<string, unknown> : {};
}

export async function getCopilotConfig(): Promise<{ enabled: boolean; completionEnabled: boolean }> {
  const res = await requestManagement('/vbio-api/copilot/config', { method: 'GET', headers: API_HEADERS }, 10000);
  const payload = (await res.json().catch(() => ({}))) as { enabled?: boolean; completionEnabled?: boolean };
  if (res.status === 404) return { enabled: true, completionEnabled: false };
  if (!res.ok) return { enabled: false, completionEnabled: false };
  return { enabled: payload.enabled === true, completionEnabled: payload.completionEnabled === true };
}


const COPILOT_TURN_STATES = new Set(['continue', 'await_confirmation', 'needs_input', 'complete']);
const COPILOT_CONFIRMATION_EFFECTS = new Set(['create', 'update', 'delete', 'execute', 'navigate']);

/**
 * Validate the planner trace defensively: it is observability, so a malformed trace must never
 * fail a turn — drop anything that is not a {round, event} object and return the clean tail.
 */
export function parseCopilotTrace(value: unknown): CopilotTraceStep[] {
  if (!Array.isArray(value)) return [];
  const trace: CopilotTraceStep[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const step = item as Record<string, unknown>;
    const event = String(step.event || '').trim();
    const round = Number(step.round);
    if (!event || !Number.isInteger(round)) continue;
    const detail = step.detail;
    trace.push({ round, event, ...(detail && typeof detail === 'object' ? { detail: detail as Record<string, unknown> } : {}) });
  }
  return trace;
}

export interface CopilotTurnResult {
  content: string;
  actions: CopilotPlanAction[];
  state: 'continue' | 'await_confirmation' | 'needs_input' | 'complete';
  questions: string[];
  planId: string;
  /** Planner reasoning/audit trajectory. Observability only; never affects execution. */
  trace: CopilotTraceStep[];
  /** Compact records retrieved this turn, carried forward as copilot_memory for follow-up turns. */
  observations: Record<string, unknown>[];
}

/** Validate a raw Copilot turn payload into a CopilotTurnResult, or throw with a concrete reason.
 * Shared by the buffered (/turn) and streaming (/stream) transports so both apply identical checks. */
export function validateCopilotTurnPayload(payload: Record<string, unknown>): CopilotTurnResult {
  const content = String(payload.content || '').trim();
  const state = String(payload.state || '').trim();
  const planId = String(payload.plan_id || '').trim();
  if (!content) throw new Error('Copilot turn returned an empty response.');
  if (!COPILOT_TURN_STATES.has(state)) throw new Error('Copilot turn returned an invalid state.');
  if (!planId) throw new Error('Copilot turn returned no plan identity.');
  const rawQuestions = payload.questions;
  if (!Array.isArray(rawQuestions) || rawQuestions.some((question) => typeof question !== 'string')) {
    throw new Error('Copilot turn returned invalid questions.');
  }
  const rawActions = payload.actions;
  if (!Array.isArray(rawActions)) {
    throw new Error('Copilot turn returned invalid confirmation operations.');
  }
  const operationKeys = new Set<string>();
  const actions = rawActions.map((item) => {
    if (!item || typeof item !== 'object') {
      throw new Error('Copilot turn returned an invalid confirmation operation.');
    }
    const action = item as CopilotPlanAction;
    const operationId = String(action.operation_id || '').trim();
    const operationKey = `${planId}:${operationId}`;
    const effect = String(action.effect || '').trim();
    const actionArguments = action.arguments;
    const actionPayload = action.payload;
    if (
      String(action.plan_id || '').trim() !== planId ||
      !operationId ||
      !String(action.id || '').trim() ||
      !String(action.label || '').trim() ||
      !String(action.description || '').trim() ||
      typeof action.sequence !== 'number' || !Number.isInteger(action.sequence) ||
      operationKeys.has(operationKey) ||
      !COPILOT_CONFIRMATION_EFFECTS.has(effect) ||
      !actionArguments || typeof actionArguments !== 'object' || Array.isArray(actionArguments) ||
      !actionPayload || typeof actionPayload !== 'object' || Array.isArray(actionPayload) ||
      String(actionPayload.planId || '').trim() !== planId ||
      String(actionPayload.operationId || '').trim() !== operationId ||
      String(actionPayload.effect || '').trim() !== effect ||
      typeof actionPayload.destructive !== 'boolean' ||
      action.needs_confirmation !== true ||
      action.execute_now !== false
    ) {
      throw new Error('Copilot turn returned an invalid confirmation operation.');
    }
    operationKeys.add(operationKey);
    return action;
  });
  const rawObservations = payload.observations;
  const observations: Record<string, unknown>[] = Array.isArray(rawObservations)
    ? rawObservations.filter(
        (item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item)
      )
    : [];
  return {
    content,
    actions,
    state: state as CopilotTurnResult['state'],
    questions: rawQuestions as string[],
    planId,
    trace: parseCopilotTrace(payload.trace),
    observations
  };
}

export async function requestCopilotTurn(input: {
  contextType: string;
  contextPayload: Record<string, unknown>;
  userId: string;
  username: string;
  content: string;
}): Promise<CopilotTurnResult> {
  const res = await requestManagement(
    '/vbio-api/copilot/turn',
    {
      method: 'POST',
      headers: {
        ...API_HEADERS,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        context_type: input.contextType,
        context_payload: sanitizeCopilotContextPayload(input.contextPayload),
        user_id: input.userId,
        username: input.username,
        content: input.content
      })
    },
    180000
  );
  const payload = (await res.json()) as Record<string, unknown> & { error?: string };
  if (!res.ok) {
    throw new Error(payload.error || `Copilot turn failed with HTTP ${res.status}.`);
  }
  return validateCopilotTurnPayload(payload);
}

export function parseOneTraceStep(value: unknown): CopilotTraceStep | null {
  if (!value || typeof value !== 'object') return null;
  const step = value as Record<string, unknown>;
  const event = String(step.event || '').trim();
  const round = Number(step.round);
  if (!event || !Number.isInteger(round)) return null;
  const detail = step.detail;
  return { round, event, ...(detail && typeof detail === 'object' ? { detail: detail as Record<string, unknown> } : {}) };
}

export function parseSseFrame(frame: string): { event: string; data: string } | null {
  let event = '';
  let data = '';
  for (const line of frame.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice('event: '.length);
    else if (line.startsWith('data: ')) data += line.slice('data: '.length);
  }
  return event ? { event, data } : null;
}

function parseJsonSafe(value: string): unknown {
  try {
    return value ? JSON.parse(value) : null;
  } catch {
    return null;
  }
}

/**
 * Stream a Copilot turn over SSE: ``onTrace`` fires for each planner trace step as it happens
 * (live reasoning/lookups), then the promise resolves with the validated terminal turn result.
 * Uses fetch + ReadableStream (not EventSource) because the request needs the X-API-Token header.
 */
export async function streamCopilotTurn(
  input: {
    contextType: string;
    contextPayload: Record<string, unknown>;
    userId: string;
    username: string;
    content: string;
  },
  onTrace: (step: CopilotTraceStep) => void
): Promise<CopilotTurnResult> {
  const res = await fetchWithTimeout(
    managementApiUrl('/vbio-api/copilot/stream'),
    {
      method: 'POST',
      headers: {
        ...API_HEADERS,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        context_type: input.contextType,
        context_payload: sanitizeCopilotContextPayload(input.contextPayload),
        user_id: input.userId,
        username: input.username,
        content: input.content
      })
    },
    180000
  );
  if (!res.ok) {
    const payload = (await res.json().catch(() => ({}))) as { error?: string };
    throw new Error(payload.error || `Copilot stream failed with HTTP ${res.status}.`);
  }
  if (!res.body) throw new Error('Copilot stream returned no response body.');
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let result: CopilotTurnResult | null = null;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let separator = buffer.indexOf('\n\n');
    while (separator >= 0) {
      const frame = buffer.slice(0, separator);
      buffer = buffer.slice(separator + 2);
      separator = buffer.indexOf('\n\n');
      const parsed = parseSseFrame(frame);
      if (!parsed) continue;
      if (parsed.event === 'trace') {
        const step = parseOneTraceStep(parseJsonSafe(parsed.data));
        if (step) onTrace(step);
      } else if (parsed.event === 'result') {
        result = validateCopilotTurnPayload((parseJsonSafe(parsed.data) as Record<string, unknown>) || {});
      } else if (parsed.event === 'error') {
        const errorPayload = (parseJsonSafe(parsed.data) as { error?: string }) || {};
        throw new Error(errorPayload.error || 'Copilot stream reported an error.');
      }
    }
  }
  if (!result) throw new Error('Copilot stream ended without a result.');
  return result;
}
export async function requestCopilotAssistant(input: {
  contextType: string;
  contextPayload: Record<string, unknown>;
  userId: string;
  username: string;
  content: string;
}): Promise<string> {
  const res = await requestManagement(
    '/vbio-api/copilot/assistant',
    {
      method: 'POST',
      headers: {
        ...API_HEADERS,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        context_type: input.contextType,
        context_payload: sanitizeCopilotContextPayload(input.contextPayload),
        user_id: input.userId,
        username: input.username,
        content: input.content
      })
    },
    90000
  );
  const payload = (await res.json().catch(() => ({}))) as { content?: string; error?: string };
  if (!res.ok) {
    throw new Error(payload.error || `Copilot assistant failed with HTTP ${res.status}.`);
  }
  const content = String(payload.content || '').trim();
  if (!content) throw new Error('Copilot assistant returned an empty response.');
  return content;
}

export async function requestCopilotPlanActions(input: {
  contextType: string;
  contextPayload: Record<string, unknown>;
  userId: string;
  username: string;
  content: string;
}): Promise<CopilotPlanAction[]> {
  const res = await requestManagement(
    '/vbio-api/copilot/plan_actions',
    {
      method: 'POST',
      headers: {
        ...API_HEADERS,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({
        context_type: input.contextType,
        context_payload: sanitizeCopilotContextPayload(input.contextPayload),
        user_id: input.userId,
        username: input.username,
        content: input.content
      })
    },
    90000
  );
  const payload = (await res.json().catch(() => ({}))) as { actions?: CopilotPlanAction[]; error?: string };
  if (!res.ok) {
    throw new Error(payload.error || `Copilot action planning failed with HTTP ${res.status}.`);
  }
  return Array.isArray(payload.actions) ? payload.actions : [];
}

/**
 * Fetch a short inline-completion suggestion for the in-progress draft. Best-effort: returns "" on
 * any failure (timeout, abort, non-ok, parse error) so the composer never blocks. Uses its own
 * short timeout and links an external AbortSignal so a newer keystroke can cancel an in-flight call.
 */
export async function requestCopilotCompletion(
  input: {
    contextType: string;
    contextPayload: Record<string, unknown>;
    userId: string;
    username: string;
    content: string;
  },
  signal?: AbortSignal
): Promise<string> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  const onExternalAbort = () => controller.abort();
  if (signal) {
    if (signal.aborted) {
      controller.abort();
    } else {
      signal.addEventListener('abort', onExternalAbort, { once: true });
    }
  }
  try {
    const res = await fetch(managementApiUrl('/vbio-api/copilot/complete'), {
      method: 'POST',
      headers: { ...API_HEADERS, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        context_type: input.contextType,
        context_payload: sanitizeCopilotContextPayload(input.contextPayload),
        user_id: input.userId,
        username: input.username,
        content: input.content
      }),
      signal: controller.signal
    });
    if (!res.ok) return '';
    const payload = (await res.json().catch(() => ({}))) as { suggestion?: string };
    return typeof payload.suggestion === 'string' ? payload.suggestion : '';
  } catch {
    return '';
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onExternalAbort);
  }
}
