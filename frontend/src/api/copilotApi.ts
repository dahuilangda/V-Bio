import { API_HEADERS, requestManagement } from './backendClient';
import type { CopilotPlanAction } from '../types/models';

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

export async function getCopilotConfig(): Promise<{ enabled: boolean }> {
  const res = await requestManagement('/vbio-api/copilot/config', { method: 'GET', headers: API_HEADERS }, 10000);
  const payload = (await res.json().catch(() => ({}))) as { enabled?: boolean };
  if (res.status === 404) return { enabled: true };
  if (!res.ok) return { enabled: false };
  return { enabled: payload.enabled === true };
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
