import { describe, expect, it } from 'vitest';
import type { ProjectCopilotMessage } from '../../types/models';
import { buildCopilotConversationContext } from './ProjectCopilotModal';

function message(partial: Partial<ProjectCopilotMessage> & { role: ProjectCopilotMessage['role'] }): ProjectCopilotMessage {
  return {
    id: 'm1',
    user_id: null,
    context_type: 'task_detail',
    project_id: 'p1',
    project_task_id: null,
    session_id: 'default',
    role: partial.role,
    content: partial.content ?? '',
    metadata: partial.metadata,
    created_at: partial.created_at ?? '2026-08-17T00:00:00Z',
    updated_at: partial.created_at ?? '2026-08-17T00:00:00Z'
  } as ProjectCopilotMessage;
}

describe('buildCopilotConversationContext action resolutions', () => {
  it('carries recent confirmation receipts (system role) into the planner context', () => {
    const messages = [
      message({ role: 'user', content: 'fill the target' }),
      message({ role: 'assistant', content: 'found structures' }),
      message({
        role: 'system',
        metadata: {
          action_resolutions: [
            {
              plan_id: 'plan-1',
              operation_id: 'apply-target',
              skill: 'task_detail:apply_docking_target_structure',
              label: 'Apply structure as affinity target',
              status: 'failed',
              error: 'Could not download the structure (HTTP 404).'
            }
          ]
        }
      })
    ];
    const context = buildCopilotConversationContext(messages) as {
      recent_messages: unknown[];
      recent_action_resolutions: Array<Record<string, unknown>>;
    };
    // the system receipt itself never enters the visible transcript
    expect(context.recent_messages).toHaveLength(2);
    const resolutions = context.recent_action_resolutions;
    expect(resolutions).toHaveLength(1);
    expect(resolutions[0].status).toBe('failed');
    expect(resolutions[0].skill).toBe('task_detail:apply_docking_target_structure');
    expect(resolutions[0].error).toContain('HTTP 404');
  });

  it('omits the resolutions key when no receipts exist', () => {
    const context = buildCopilotConversationContext([
      message({ role: 'user', content: 'hi' }),
      message({ role: 'assistant', content: 'hello' })
    ]);
    expect(context).not.toHaveProperty('recent_action_resolutions');
  });

  it('keeps only the most recent receipts, in chronological order, when receipts exceed the window', () => {
    const messages: ProjectCopilotMessage[] = [];
    for (let index = 0; index < 15; index += 1) {
      messages.push(
        message({
          role: 'system',
          metadata: {
            action_resolutions: [
              { plan_id: `p${index}`, operation_id: `op${index}`, skill: 's', label: '', status: 'applied' }
            ]
          }
        })
      );
    }
    const context = buildCopilotConversationContext(messages) as {
      recent_action_resolutions: Array<Record<string, unknown>>;
    };
    const resolutions = context.recent_action_resolutions;
    expect(resolutions.length).toBeLessThanOrEqual(12);
    const operationIds = resolutions.map((row) => row.operation_id);
    // chronological: the oldest kept receipt precedes the newest
    expect(operationIds.indexOf('op14')).toBe(operationIds.length - 1);
    expect(operationIds).not.toContain('op0');
  });

  it('ignores malformed receipt entries instead of failing the turn', () => {
    const context = buildCopilotConversationContext([
      message({
        role: 'system',
        metadata: {
          action_resolutions: [
            { plan_id: '', operation_id: 'op', status: 'applied' },
            { plan_id: 'p', operation_id: '', status: 'applied' },
            { plan_id: 'p', operation_id: 'op', status: 'unknown-status' },
            'not-an-object'
          ]
        }
      })
    ]);
    expect(context).not.toHaveProperty('recent_action_resolutions');
  });

  it('carries the action arguments so recovery/summary turns can cite what was applied', () => {
    const context = buildCopilotConversationContext([
      message({
        role: 'system',
        metadata: {
          action_resolutions: [
            {
              plan_id: 'plan-1',
              operation_id: 'apply-target',
              skill: 'task_detail:apply_docking_target_structure',
              label: 'Apply structure as docking target',
              status: 'applied',
              arguments: { structurePdbId: '1FBJ', fileName: '1FBJ.cif' }
            }
          ]
        }
      })
    ]) as {
      recent_action_resolutions: Array<Record<string, unknown>>;
    };
    const resolution = context.recent_action_resolutions[0];
    expect(resolution.arguments).toEqual({ structurePdbId: '1FBJ', fileName: '1FBJ.cif' });
  });
});
