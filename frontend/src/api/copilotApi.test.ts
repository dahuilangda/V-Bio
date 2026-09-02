import { afterEach, describe, expect, it, vi } from 'vitest';
import { parseCopilotTrace, parseOneTraceStep, parseSseFrame, requestCopilotTurn, streamCopilotTurn, validateCopilotTurnPayload } from './copilotApi';

function validAction(overrides: Record<string, unknown> = {}) {
  return {
    id: 'host.update',
    operation_id: 'w1',
    plan_id: 'plan1',
    sequence: 0,
    label: 'Write',
    description: 'desc',
    arguments: { value: 'x' },
    payload: { planId: 'plan1', operationId: 'w1', effect: 'update', destructive: false },
    effect: 'update',
    needs_confirmation: true,
    execute_now: false,
    ...overrides
  };
}

function validPayload(overrides: Record<string, unknown> = {}) {
  return {
    content: 'done',
    state: 'complete',
    plan_id: 'plan1',
    questions: [],
    actions: [],
    trace: [],
    observations: [],
    ...overrides
  };
}

describe('parseCopilotTrace', () => {
  it('drops malformed steps and keeps detail', () => {
    const trace = parseCopilotTrace([
      { round: 0, event: 'terminal', detail: { state: 'complete' } },
      { round: 0, event: '' },
      'x',
      null
    ]);
    expect(trace).toHaveLength(1);
    expect(trace[0].detail).toEqual({ state: 'complete' });
  });

  it('returns [] for non-array input', () => {
    expect(parseCopilotTrace(undefined)).toEqual([]);
    expect(parseCopilotTrace({})).toEqual([]);
  });
});

describe('parseOneTraceStep', () => {
  it('returns null for malformed steps', () => {
    expect(parseOneTraceStep(null)).toBeNull();
    expect(parseOneTraceStep({ event: 'x' })).toBeNull(); // missing round
    expect(parseOneTraceStep({ round: 0, event: '' })).toBeNull();
  });
  it('parses a valid step with detail', () => {
    const step = parseOneTraceStep({ round: 2, event: 'terminal', detail: { state: 'complete' } });
    expect(step).toEqual({ round: 2, event: 'terminal', detail: { state: 'complete' } });
  });
});

describe('parseSseFrame', () => {
  it('extracts event and data, ignoring comment lines', () => {
    expect(parseSseFrame('event: trace\ndata: {"round":0}')).toEqual({ event: 'trace', data: '{"round":0}' });
    // multi-line data concatenation
    expect(parseSseFrame('event: result\ndata: {"a":\ndata: 1}')).toEqual({ event: 'result', data: '{"a":1}' });
  });
  it('returns null for frames without an event (e.g. keepalive comments)', () => {
    expect(parseSseFrame(': keepalive')).toBeNull();
  });
});

describe('validateCopilotTurnPayload', () => {
  it('returns the validated result for a well-formed payload', () => {
    const result = validateCopilotTurnPayload(validPayload({ actions: [validAction()], trace: [{ round: 0, event: 'terminal' }], observations: [{ source: 'x' }] }));
    expect(result.content).toBe('done');
    expect(result.state).toBe('complete');
    expect(result.actions).toHaveLength(1);
    expect(result.trace).toHaveLength(1);
    expect(result.observations).toEqual([{ source: 'x' }]);
  });

  it('throws on empty content, bad state, or missing plan id', () => {
    expect(() => validateCopilotTurnPayload(validPayload({ content: '' }))).toThrow();
    expect(() => validateCopilotTurnPayload(validPayload({ state: 'bogus' }))).toThrow();
    expect(() => validateCopilotTurnPayload(validPayload({ plan_id: '' }))).toThrow();
  });

  it('accepts the honest failed terminal state', () => {
    // A turn that exhausted the planning budget returns state=failed with a plain status message;
    // it is a real result (rendered with its trace), not a thrown error.
    const result = validateCopilotTurnPayload(
      validPayload({ state: 'failed', content: 'I could not complete this request.' })
    );
    expect(result.state).toBe('failed');
    expect(result.content).toContain('could not complete');
  });

  it('throws on malformed actions and drops malformed questions defensively', () => {
    // Malformed actions still fail the turn (integrity gate).
    expect(() => validateCopilotTurnPayload(validPayload({ actions: {} }))).toThrow();
    // Malformed questions are dropped defensively (like the trace), never failing the turn: a bad
    // question item becomes an empty list rather than rejecting the whole response.
    const result = validateCopilotTurnPayload(validPayload({ questions: [123] }));
    expect(result.questions).toEqual([]);
  });

  it('parses structured choice questions into typed objects', () => {
    const result = validateCopilotTurnPayload(
      validPayload({
        questions: [
          {
            text: 'Which task type?',
            kind: 'choice',
            options: [
              { label: 'Affinity', value: 'affinity' },
              { label: 'Prediction', value: 'prediction' },
            ],
          },
        ],
      })
    );
    expect(result.questions).toHaveLength(1);
    expect(result.questions[0].kind).toBe('choice');
    expect(result.questions[0].options).toHaveLength(2);
    expect(result.questions[0].options?.[0].value).toBe('affinity');
  });

  it('carries the allowOther flag through and drops non-boolean values', () => {
    // allowOther toggles the free-text "Other ___" answer on choice questions: an explicit false
    // disables it, absence keeps the default (enabled), and a non-boolean is dropped defensively.
    const result = validateCopilotTurnPayload(
      validPayload({
        questions: [
          { text: 'A?', kind: 'choice', allowOther: false, options: [{ label: 'A', value: 'a' }, { label: 'B', value: 'b' }] },
          { text: 'B?', kind: 'choice', options: [{ label: 'C', value: 'c' }, { label: 'D', value: 'd' }] },
          { text: 'C?', kind: 'choice', allowOther: 'yes', options: [{ label: 'E', value: 'e' }, { label: 'F', value: 'f' }] },
        ],
      })
    );
    expect(result.questions).toHaveLength(3);
    expect(result.questions[0].allowOther).toBe(false);
    expect(result.questions[1].allowOther).toBeUndefined();
    expect(result.questions[2].allowOther).toBeUndefined();
  });

  it('drops a choice question with fewer than two options', () => {
    const result = validateCopilotTurnPayload(
      validPayload({
        questions: [{ text: 'Only one?', kind: 'choice', options: [{ label: 'A', value: 'a' }] }],
      })
    );
    expect(result.questions).toEqual([]);
  });

  it('throws when an action fails its integrity checks', () => {
    // effect in payload disagrees with the declared effect
    expect(() => validateCopilotTurnPayload(validPayload({ actions: [validAction({ payload: { planId: 'plan1', operationId: 'w1', effect: 'delete', destructive: false }, effect: 'update' })] }))).toThrow();
    // duplicate operation id within one turn
    const dup = validAction();
    expect(() => validateCopilotTurnPayload(validPayload({ actions: [validAction(), dup] }))).toThrow();
  });
});

/** A fake fetch Response whose body streams the given SSE frames. */
function sseResponse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    }
  });
  return { ok: true, status: 200, body } as unknown as Response;
}

const streamInput = { contextType: 'workspace', contextPayload: {}, userId: 'u', username: 'u', content: 'hi' };

describe('streamCopilotTurn', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('streams trace steps to onTrace then resolves with the validated result', async () => {
    const traceFrame = 'event: trace\ndata: {"round":0,"event":"model_request"}\n\n';
    const resultFrame = `event: result\ndata: ${JSON.stringify(validPayload({ actions: [validAction()], trace: [{ round: 0, event: 'terminal' }] }))}\n\n`;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([traceFrame, resultFrame])));
    const onTrace = vi.fn();
    const result = await streamCopilotTurn(streamInput, onTrace);
    expect(onTrace).toHaveBeenCalledTimes(1);
    expect(onTrace.mock.calls[0][0]).toMatchObject({ event: 'model_request' });
    expect(result.content).toBe('done');
    expect(result.actions).toHaveLength(1);
    expect(result.trace).toHaveLength(1);
  });

  it('throws on an error frame', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse(['event: error\ndata: {"error":"planner down"}\n\n'])));
    await expect(streamCopilotTurn(streamInput, () => {})).rejects.toThrow('planner down');
  });

  it('throws when the stream ends without a result frame', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse(['event: trace\ndata: {"round":0,"event":"x"}\n\n'])));
    await expect(streamCopilotTurn(streamInput, () => {})).rejects.toThrow('without a result');
  });

  it('throws on a non-ok HTTP response, surfacing the error message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 500, json: async () => ({ error: 'server error' }) })
    );
    await expect(streamCopilotTurn(streamInput, () => {})).rejects.toThrow('server error');
  });

  it('handles frames split across stream chunks', async () => {
    // Two trace steps arrive in one chunk, then the result in a second — split mid-frame.
    const chunk1 = 'event: trace\ndata: {"round":0,"event":"a"}\n\nevent: trace\ndata: {"round":0,"even';
    const chunk2 = 't":"b"}\n\n';
    const resultFrame = `event: result\ndata: ${JSON.stringify(validPayload())}\n\n`;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(chunk1));
        controller.enqueue(encoder.encode(chunk2));
        controller.enqueue(encoder.encode(resultFrame));
        controller.close();
      }
    });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, body } as unknown as Response));
    const onTrace = vi.fn();
    const result = await streamCopilotTurn(streamInput, onTrace);
    expect(onTrace).toHaveBeenCalledTimes(2);
    expect(result.content).toBe('done');
  });
});

describe('requestCopilotTurn (buffered transport)', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns the validated result for a well-formed response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => validPayload({ actions: [validAction()] }) })
    );
    const result = await requestCopilotTurn(streamInput);
    expect(result.content).toBe('done');
    expect(result.actions).toHaveLength(1);
  });

  it('throws on a non-ok response, surfacing the payload error', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 400, json: async () => ({ error: 'bad request' }) }));
    await expect(requestCopilotTurn(streamInput)).rejects.toThrow('bad request');
  });

  it('throws on an empty content payload', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => validPayload({ content: '   ' }) }));
    await expect(requestCopilotTurn(streamInput)).rejects.toThrow('empty response');
  });
});
