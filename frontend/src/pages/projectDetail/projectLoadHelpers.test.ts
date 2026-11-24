import { describe, it, expect } from 'vitest';
import { workspaceLoadSearchKey } from './projectLoadHelpers';

describe('workspaceLoadSearchKey', () => {
  it('drops params consumed in place by their own handlers', () => {
    const search = '?tab=basics&task_row_id=r1&task_list_page=2&clone_task_row_id=c1&copilot_task_name=x&copilot_sequence=Y';
    expect(workspaceLoadSearchKey(search)).toBe('');
  });

  it('is unchanged when a handler strips its own param', () => {
    expect(workspaceLoadSearchKey('?clone_task_row_id=c1&tab=basics')).toBe(
      workspaceLoadSearchKey('?tab=basics')
    );
  });

  it('keeps params that change what the server returns', () => {
    expect(workspaceLoadSearchKey('?new_task=1&source_task_row_id=s1&tab=basics')).toBe(
      '?new_task=1&source_task_row_id=s1'
    );
    expect(workspaceLoadSearchKey('?tab=basics')).not.toBe(workspaceLoadSearchKey('?new_task=1&tab=basics'));
  });
});
