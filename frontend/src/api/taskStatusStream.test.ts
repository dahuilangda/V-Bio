import { describe, expect, it } from 'vitest';
import { parseStatusStreamChunk } from './taskStatusStream';

describe('parseStatusStreamChunk', () => {
  it('parses a changed frame split across chunks', () => {
    const frame1 = 'event: changed\ndata: {"project_id":"p-1","entr';
    const frame2 = 'ies":[{"task_id":"t-1","state":"SUCCESS"}]}\n\n';
    const first = parseStatusStreamChunk('', frame1);
    expect(first.entries).toEqual([]);
    const second = parseStatusStreamChunk(first.buffer, frame2);
    expect(second.entries).toEqual([[{ task_id: 't-1', state: 'SUCCESS' }]]);
    expect(second.buffer).toBe('');
  });

  it('ignores keepalive comments and reconnect events', () => {
    const res = parseStatusStreamChunk('', ': keepalive\n\nevent: reconnect\ndata: {}\n\n');
    expect(res.entries).toEqual([]);
  });

  it('keeps incomplete trailing frame in the buffer', () => {
    const res = parseStatusStreamChunk('', 'event: changed\ndata: {"entries":[]}\n');
    expect(res.entries).toEqual([]);
    expect(res.buffer).toContain('event: changed');
  });

  it('drops entries missing task_id or state', () => {
    const res = parseStatusStreamChunk(
      '',
      'event: changed\ndata: {"entries":[{"task_id":"t-1"},{"task_id":"","state":"X"},{"task_id":"t-2","state":123}]}\n\n'
    );
    // state 非字符串但 String() 可转 → t-2 保留；缺 task_id / 空 id 丢弃
    expect(res.entries[0].map((entry) => entry.task_id)).toEqual(['t-2']);
  });
});
