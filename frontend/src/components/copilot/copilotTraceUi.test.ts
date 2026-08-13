import { describe, it, expect } from 'vitest';
import type { ProjectCopilotMessage } from '../../types/models';
import { collectCopilotMemory, formatTraceStep, readPlannerTrace, readSessionId } from './copilotTraceUi';

function msg(role: ProjectCopilotMessage['role'], sessionId: string, observations?: unknown[]): ProjectCopilotMessage {
  return {
    id: 'm',
    context_type: 'project_list',
    project_id: null,
    project_task_id: null,
    user_id: null,
    role,
    content: '',
    metadata: { session_id: sessionId, ...(observations ? { planner_observations: observations } : {}) },
    created_at: '',
    updated_at: ''
  };
}

describe('readSessionId', () => {
  it('returns the metadata session id, falling back to default', () => {
    expect(readSessionId(msg('assistant', 's1'))).toBe('s1');
    expect(readSessionId(msg('assistant', '  '))).toBe('default');
  });
});

describe('readPlannerTrace', () => {
  it('keeps valid steps and drops malformed entries', () => {
    const trace = readPlannerTrace([
      { round: 0, event: 'model_request' },
      { round: 1, event: 'terminal', detail: { state: 'complete' } },
      { round: 2, event: '' }, // no event
      { round: 1.5, event: 'x' }, // non-integer round
      null,
      'nope'
    ]);
    expect(trace).toHaveLength(2);
    expect(trace[0]).toMatchObject({ round: 0, event: 'model_request' });
    expect(trace[1].detail).toEqual({ state: 'complete' });
  });
});

describe('formatTraceStep', () => {
  it('renders each lifecycle event as a descriptive user-facing phrase', () => {
    expect(formatTraceStep({ round: 0, event: 'model_request' })).toBe('Understanding your request');
    // later rounds show refinement
    expect(formatTraceStep({ round: 1, event: 'model_request' })).toBe('Refining the plan');
    // token counts are developer-only detail and must NOT surface to the user
    expect(formatTraceStep({ round: 0, event: 'model_request', detail: { usage: { input_tokens: 9272, output_tokens: 114 } } })).toBe('Understanding your request');
    expect(formatTraceStep({ round: 0, event: 'malformed_output' })).toBe('Reformatting response');
    expect(formatTraceStep({ round: 0, event: 'no_convergence' })).toContain('Could not settle');
  });

  it('hides technical audit issues but shows a user-friendly terminal label', () => {
    expect(formatTraceStep({ round: 0, event: 'audit_rejected', detail: { issues: ['bad skill', 'second'] } })).toBe('Adjusting approach');
    expect(formatTraceStep({ round: 0, event: 'audit_rejected', detail: {} })).toBe('Adjusting approach');
    expect(formatTraceStep({ round: 0, event: 'terminal', detail: { state: 'await_confirmation' } })).toBe('Ready for your confirmation');
    expect(formatTraceStep({ round: 0, event: 'terminal', detail: { state: 'needs_input' } })).toBe('Waiting for your input');
    expect(formatTraceStep({ round: 0, event: 'terminal', detail: { state: 'outline' } })).toBe('Plan outlined');
    expect(formatTraceStep({ round: 0, event: 'terminal', detail: { state: 'failed' } })).toBe('Could not complete the plan');
    expect(formatTraceStep({ round: 0, event: 'terminal', detail: { state: 'complete' } })).toBe('Complete');
  });

  it('shows the data source and result count for skill observations', () => {
    const text = formatTraceStep({
      round: 0,
      event: 'skill_observations',
      detail: { observations: [{ skill: 'pubchem.search', ok: true, count: 2, successCount: 2 }, { skill: 'rcsb.resolve', ok: false, count: 1, successCount: 0 }] }
    });
    // The source name (PubChem) is surfaced, not the full skill namespace (pubchem.search)
    expect(text).toContain('Pubchem');
    expect(text).toContain('2 result');
    expect(text).toContain('Rcsb unavailable');
    // Full skill namespace with dot should NOT appear
    expect(text).not.toContain('.search');
    expect(text).not.toContain('.resolve');
  });
});

describe('collectCopilotMemory', () => {
  it('collects assistant observations from the session, chronological, capped, skipping other sessions/roles', () => {
    const messages: ProjectCopilotMessage[] = [
      msg('assistant', 's1', [{ cid: '1' }, { cid: '2' }]), // older
      msg('user', 's1', [{ cid: 'ignored-user' }]),
      msg('assistant', 'other', [{ cid: 'other-session' }]),
      msg('assistant', 's1', [{ cid: '3' }]) // newer
    ];
    const memory = collectCopilotMemory(messages, 's1');
    // chronological order, only s1 assistant records, most-recent turn's record last
    expect(memory).toEqual([{ cid: '1' }, { cid: '2' }, { cid: '3' }]);
  });

  it('respects the cap, preferring the most recent turns', () => {
    const messages: ProjectCopilotMessage[] = [msg('assistant', 's1', [{ a: 1 }, { a: 2 }]), msg('assistant', 's1', [{ a: 3 }])];
    expect(collectCopilotMemory(messages, 's1', 2)).toEqual([{ a: 2 }, { a: 3 }]);
  });

  it('returns empty when no assistant observations exist', () => {
    expect(collectCopilotMemory([msg('user', 's1')], 's1')).toEqual([]);
  });
});
