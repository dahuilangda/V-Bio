export interface Envelope<T = unknown> {
  id: number;
  ok: boolean;
  result?: T;
  error?: string;
}

export type Reply<T = unknown> = (response: Envelope<T>) => void;

export interface Transport<M = unknown> {
  send: (id: number, payload: M) => void;
  close: () => void;
}

export interface RpcOptions<M = unknown, T = unknown> {
  open: (reply: Reply<T>, crash: (error: Error) => void) => Transport<M>;
  idleMs: number;
  setTimer?: (callback: () => void, ms: number) => number;
  clearTimer?: (handle: number) => void;
}

export interface Rpc<M = unknown, T = unknown> {
  call: (payload: M) => Promise<T>;
}

interface Pending {
  resolve: (value: unknown) => void;
  reject: (error: Error) => void;
}

export function createRpc<M, T>(options: RpcOptions<M, T>): Rpc<M, T> {
  const setTimer = options.setTimer ?? ((callback: () => void, ms: number) => window.setTimeout(callback, ms));
  const clearTimer = options.clearTimer ?? ((handle: number) => window.clearTimeout(handle));
  const pending = new Map<number, Pending>();
  let transport: Transport<M> | null = null;
  let idle: number | null = null;
  let nextId = 1;

  const close = (error: Error) => {
    if (idle !== null) {
      clearTimer(idle);
      idle = null;
    }
    const dead = transport;
    transport = null;
    dead?.close();
    for (const waiter of pending.values()) waiter.reject(error);
    pending.clear();
  };

  const settle = (id: number, outcome: { value?: unknown; error?: Error }) => {
    const waiter = pending.get(id);
    if (!waiter) return;
    pending.delete(id);
    if (outcome.error) waiter.reject(outcome.error);
    else waiter.resolve(outcome.value);
    if (pending.size === 0 && transport) {
      idle = setTimer(() => close(new Error('Worker idle.')), options.idleMs);
    }
  };

  const reply: Reply<T> = (response) => {
    settle(response.id, response.ok ? { value: response.result } : { error: new Error(response.error || 'Worker call failed.') });
  };

  return {
    call: (payload: M) => {
      if (idle !== null) {
        clearTimer(idle);
        idle = null;
      }
      if (!transport) transport = options.open(reply, (error) => close(error));
      const id = nextId++;
      const settled = new Promise<T>((resolve, reject) => {
        pending.set(id, { resolve: (value) => resolve(value as T), reject });
      });
      transport.send(id, payload);
      return settled;
    }
  };
}
