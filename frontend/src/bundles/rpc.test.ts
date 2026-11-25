import { describe, expect, it, vi } from 'vitest';
import { createRpc, type Transport } from './rpc';

interface Frame {
  id: number;
  op: string;
  args: unknown[];
}

function harness(idleMs = 1000) {
  const timers = new Map<number, () => void>();
  const transports: FakeTransport[] = [];
  let handle = 0;
  const setTimer = (callback: () => void) => {
    handle += 1;
    timers.set(handle, callback);
    return handle;
  };
  const clearTimer = (id: number) => {
    timers.delete(id);
  };
  const open = vi.fn((reply: (response: { id: number; ok: boolean; result?: unknown; error?: string }) => void, crash: (error: Error) => void) => {
    const transport = new FakeTransport(reply, crash);
    transports.push(transport);
    return transport;
  });
  const rpc = createRpc({ open, idleMs, setTimer, clearTimer });
  return {
    rpc,
    open,
    transports,
    current: () => transports[transports.length - 1],
    fireIdle: () => {
      const callbacks = [...timers.values()];
      timers.clear();
      for (const callback of callbacks) callback();
    },
    armed: () => timers.size
  };
}

class FakeTransport implements Transport {
  sent: Frame[] = [];
  closed = false;
  constructor(
    private reply: (response: { id: number; ok: boolean; result?: unknown; error?: string }) => void,
    private crash: (error: Error) => void
  ) {}

  send = (id: number, payload: unknown) => {
    this.sent.push({ id, op: String((payload as { op?: unknown }).op), args: [] });
  };
  close = () => {
    this.closed = true;
  };
  answer = (result: unknown) => {
    const frame = this.sent.shift();
    if (frame) this.reply({ id: frame.id, ok: true, result });
  };
  fail = (error: string) => {
    const frame = this.sent.shift();
    if (frame) this.reply({ id: frame.id, ok: false, error });
  };
  die = () => this.crash(new Error('worker crashed'));
}

describe('createRpc', () => {
  it('starts one transport and reuses it', async () => {
    const h = harness();
    const first = h.rpc.call({ op: 'echo', value: 1 });
    const second = h.rpc.call({ op: 'echo', value: 2 });
    h.current().answer(1);
    h.current().answer(2);
    expect(await Promise.all([first, second])).toEqual([1, 2]);
    expect(h.open).toHaveBeenCalledTimes(1);
  });

  it('closes the transport after the idle period and starts a fresh one on the next call', async () => {
    const h = harness();
    const call = h.rpc.call({ op: 'double', value: 2 });
    h.current().answer(4);
    expect(await call).toBe(4);
    expect(h.armed()).toBe(1);
    h.fireIdle();
    expect(h.current().closed).toBe(true);
    const again = h.rpc.call({ op: 'double', value: 3 });
    expect(h.open).toHaveBeenCalledTimes(2);
    h.current().answer(6);
    expect(await again).toBe(6);
  });

  it('propagates worker errors to the caller and keeps the transport usable', async () => {
    const h = harness();
    const call = h.rpc.call({ op: 'bad' });
    h.current().fail('invalid input');
    await expect(call).rejects.toThrow('invalid input');
    const next = h.rpc.call({ op: 'ok' });
    h.current().answer(1);
    expect(await next).toBe(1);
    expect(h.open).toHaveBeenCalledTimes(1);
  });

  it('rejects in-flight calls when the worker dies and restarts on the next call', async () => {
    const h = harness();
    const call = h.rpc.call({ op: 'slow' });
    h.current().die();
    await expect(call).rejects.toThrow('worker crashed');
    expect(h.current().closed).toBe(true);
    const next = h.rpc.call({ op: 'again' });
    expect(h.open).toHaveBeenCalledTimes(2);
    h.current().answer(9);
    expect(await next).toBe(9);
  });

  it('does not arm the idle timer while calls are in flight', async () => {
    const h = harness();
    const call = h.rpc.call({ op: 'wait' });
    expect(h.armed()).toBe(0);
    h.current().answer(null);
    await call;
    expect(h.armed()).toBe(1);
  });

  it('cancels a pending idle shutdown when a new call arrives', async () => {
    const h = harness();
    const first = h.rpc.call({ op: 'a' });
    h.current().answer(null);
    await first;
    expect(h.armed()).toBe(1);
    const second = h.rpc.call({ op: 'b' });
    expect(h.armed()).toBe(0);
    h.current().answer(null);
    await second;
  });
});
