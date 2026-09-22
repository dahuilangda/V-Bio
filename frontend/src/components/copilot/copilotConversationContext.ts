/**
 * Compact conversation projection shared by the planner turn and the inline
 * completer (kept here to avoid a component import cycle).
 */
import type { ProjectCopilotMessage } from '../../types/models';
import { readActionResolutions, type CopilotActionResolution } from './copilotTraceUi';

const COPILOT_RECENT_CONTEXT_MESSAGES = 6;
const COPILOT_SUMMARY_SOURCE_MESSAGES = 12;
const COPILOT_CONTEXT_MESSAGE_CHARS = 700;
const COPILOT_CONTEXT_SUMMARY_CHARS = 1800;

// Confirmation receipts for recent_action_resolutions, bounded to keep context small.
const COPILOT_RECENT_ACTION_RESOLUTIONS = 12;

function compactCopilotText(value: unknown, limit: number): string {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...`;
}

function collectRecentActionResolutions(messages: ProjectCopilotMessage[]): CopilotActionResolution[] {
  const collected: CopilotActionResolution[] = [];
  for (let index = messages.length - 1; index >= 0 && collected.length < COPILOT_RECENT_ACTION_RESOLUTIONS; index -= 1) {
    const message = messages[index];
    if (message.role !== 'system') continue;
    const resolutions = readActionResolutions(message);
    for (let inner = resolutions.length - 1; inner >= 0 && collected.length < COPILOT_RECENT_ACTION_RESOLUTIONS; inner -= 1) {
      collected.unshift(resolutions[inner]);
    }
  }
  return collected;
}

export function buildCopilotConversationContext(messages: ProjectCopilotMessage[]): Record<string, unknown> {
  const visibleMessages = messages.filter((message) => message.role === 'user' || message.role === 'assistant');
  // Receipts (system role) aren't in the visible transcript, but the planner needs their
  // outcome: a failed apply means recover/re-propose, an applied one means advance.
  const actionResolutions = collectRecentActionResolutions(messages);
  if (visibleMessages.length === 0 && actionResolutions.length === 0) {
    return { compression: 'empty', recent_messages: [] };
  }
  const recent = visibleMessages.slice(-COPILOT_RECENT_CONTEXT_MESSAGES).map((message) => ({
    role: message.role,
    at: message.created_at,
    content: compactCopilotText(message.content, COPILOT_CONTEXT_MESSAGE_CHARS)
  }));
  const older = visibleMessages.slice(0, Math.max(0, visibleMessages.length - COPILOT_RECENT_CONTEXT_MESSAGES));
  const summarySource = older.slice(-COPILOT_SUMMARY_SOURCE_MESSAGES);
  const olderSummary = summarySource
    .map((message, index) => `${index + 1}. ${message.role}: ${compactCopilotText(message.content, 180)}`)
    .join('\n');
  return {
    compression: older.length > 0 ? 'summary_plus_recent' : 'recent_only',
    total_messages: visibleMessages.length,
    summarized_messages: older.length,
    summary_source_messages: summarySource.length,
    summary: older.length > 0 ? compactCopilotText(olderSummary, COPILOT_CONTEXT_SUMMARY_CHARS) : '',
    recent_messages: recent,
    ...(actionResolutions.length > 0 ? { recent_action_resolutions: actionResolutions } : {})
  };
}
