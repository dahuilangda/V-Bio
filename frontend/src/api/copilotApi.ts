import { API_HEADERS, requestManagement } from './backendClient';
import type { CopilotPlanAction } from '../types/models';

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
        context_payload: input.contextPayload,
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
        context_payload: input.contextPayload,
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
