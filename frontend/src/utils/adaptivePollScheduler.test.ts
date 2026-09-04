import { describe, expect, it } from 'vitest';
import {
  applyEqualJitter,
  createAdaptivePollScheduler,
  type AdaptivePollSchedulerEnv
} from './adaptivePollScheduler';

/**
 * Deterministic harness: a manual single-slot timer queue the test advances explicitly,
 * plus injectable visibility / random / clock. No fake timers needed — the scheduler only
 * ever keeps one pending timeout.
 */
function createHarness(randomValue = 0) {
  let nowMs = 1_000_000;
  let visible = true;
  let handleSeq = 0;
  let pending: { handle: number; handler: () => void; atMs: number } | null = null;
  let lastDelayMs: number | null = null;
  const visibilityHandlers = new Set<() => void>();

  const env: AdaptivePollSchedulerEnv = {
    now: () => nowMs,
    setTimer: (handler, timeoutMs) => {
      const handle = ++handleSeq;
      pending = { handle, handler, atMs: nowMs + timeoutMs };
      lastDelayMs = timeoutMs;
      return handle;
    },
    clearTimer: (handle) => {
      if (pending?.handle === handle) pending = null;
    },
    isDocumentVisible: () => visible,
    onVisibilityChange: (handler) => {
      visibilityHandlers.add(handler);
      return () => visibilityHandlers.delete(handler);
    },
    random: () => randomValue
  };

  return {
    env,
    get lastDelayMs() {
      return lastDelayMs;
    },
    get hasPendingTimer() {
      return pending !== null;
    },
    /** Advance the clock and fire every timer that comes due. */
    async advance(ms: number) {
      nowMs += ms;
      if (pending && nowMs >= pending.atMs) {
        const current = pending;
        pending = null;
        current.handler();
      }
      // Let the tick chain settle (tick body + finally reschedule).
      await Promise.resolve();
      await Promise.resolve();
    },
    /** Advance the clock without firing timers — simulates a throttled background tab. */
    elapse(ms: number) {
      nowMs += ms;
    },
    fireVisibilityChange() {
      for (const handler of [...visibilityHandlers]) handler();
    },
    setVisible(next: boolean) {
      visible = next;
      this.fireVisibilityChange();
    }
  };
}

interface HarnessOptions {
  activeMs?: number;
  idleMs?: number;
  idleAfterUnchangedTicks?: number;
  hiddenIntervalMultiplier?: number;
  maxIntervalMs?: number;
  tickOnStart?: boolean;
  /** `null` keeps returning the default intervals; a value pauses the chain once. */
  pauseIntervalsOnce?: boolean;
  tickResults?: Array<boolean | 'error'>;
}

function createScheduler(harness: ReturnType<typeof createHarness>, options: HarnessOptions = {}) {
  const ticks: number[] = [];
  const scheduler = createAdaptivePollScheduler(
    {
      resolveIntervals: () => {
        if (options.pauseIntervalsOnce) {
          options.pauseIntervalsOnce = false;
          return null;
        }
        return {
          activeMs: options.activeMs ?? 5000,
          idleMs: options.idleMs ?? 12000
        };
      },
      idleAfterUnchangedTicks: options.idleAfterUnchangedTicks ?? 3,
      hiddenIntervalMultiplier: options.hiddenIntervalMultiplier ?? 2,
      maxIntervalMs: options.maxIntervalMs ?? 30000,
      tickOnStart: options.tickOnStart,
      tick: async () => {
        ticks.push(ticks.length);
        const result = options.tickResults?.shift();
        if (result === 'error') throw new Error('poll failed');
        return result ?? true;
      }
    },
    harness.env
  );
  return { scheduler, ticks };
}

describe('applyEqualJitter', () => {
  it('keeps half the delay and randomizes the rest (AWS equal jitter)', () => {
    expect(applyEqualJitter(5000, () => 0)).toBe(2500);
    expect(applyEqualJitter(5000, () => 1)).toBe(5000);
    expect(applyEqualJitter(5000, () => 0.5)).toBe(3750);
  });
});

describe('createAdaptivePollScheduler', () => {
  it('schedules the first tick at the jittered active interval', () => {
    const harness = createHarness(0); // random=0 → delay = active/2
    const { scheduler } = createScheduler(harness, { activeMs: 5000 });
    scheduler.start();
    expect(harness.lastDelayMs).toBe(2500);
    scheduler.stop();
  });

  it('ticks immediately on start when tickOnStart is set', async () => {
    const harness = createHarness(0);
    const { scheduler, ticks } = createScheduler(harness, { tickOnStart: true });
    scheduler.start();
    await harness.advance(0);
    expect(ticks).toHaveLength(1);
    expect(harness.hasPendingTimer).toBe(true);
    scheduler.stop();
  });

  it('drops to the idle cadence after consecutive unchanged ticks and recovers on change', async () => {
    const harness = createHarness(0);
    const { scheduler } = createScheduler(harness, {
      activeMs: 5000,
      idleMs: 12000,
      idleAfterUnchangedTicks: 3,
      tickResults: [true, false, false, false, true]
    });
    scheduler.start();

    // Changed tick → stays active.
    await harness.advance(2500);
    expect(harness.lastDelayMs).toBe(2500);
    // Two unchanged ticks → still active (threshold 3).
    await harness.advance(2500);
    await harness.advance(2500);
    expect(harness.lastDelayMs).toBe(2500);
    // Third unchanged tick → next delay uses idleMs.
    await harness.advance(2500);
    expect(harness.lastDelayMs).toBe(6000);
    // A changed tick resets to active.
    await harness.advance(6000);
    expect(harness.lastDelayMs).toBe(2500);
    scheduler.stop();
  });

  it('never overlaps ticks and measures the next delay from completion', async () => {
    const harness = createHarness(0);
    const ticks: number[] = [];
    // Ref indirection: the promise executor assigns it later, and TS narrowing cannot
    // track assignments made inside callbacks.
    const releaseRef: { current: (() => void) | null } = { current: null };
    const gated = createAdaptivePollScheduler(
      {
        resolveIntervals: () => ({ activeMs: 5000, idleMs: 12000 }),
        idleAfterUnchangedTicks: 3,
        hiddenIntervalMultiplier: 2,
        maxIntervalMs: 30000,
        tick: () => {
          ticks.push(ticks.length);
          return new Promise<boolean>((resolve) => {
            releaseRef.current = () => resolve(true);
          });
        }
      },
      harness.env
    );
    gated.start();
    await harness.advance(2500);
    expect(ticks).toHaveLength(1); // first tick started
    // The tick is in flight (not yet released): the clock may not start another one.
    await harness.advance(2500);
    expect(ticks).toHaveLength(1);
    releaseRef.current?.();
    await harness.advance(0);
    expect(ticks).toHaveLength(1); // released, no extra tick
    // Next delay is scheduled only after completion.
    expect(harness.hasPendingTimer).toBe(true);
    gated.stop();
  });

  it('applies the hidden multiplier while the document is hidden', async () => {
    const harness = createHarness(0);
    const { scheduler } = createScheduler(harness, {
      activeMs: 5000,
      hiddenIntervalMultiplier: 2
    });
    scheduler.start();
    harness.setVisible(false);
    await harness.advance(2500);
    expect(harness.lastDelayMs).toBe(5000); // 5000 active ×2 hidden, equal-jittered to half
    harness.setVisible(true);
    scheduler.stop();
  });

  it('fires exactly one immediate catch-up tick on visibility regain when stale', async () => {
    const harness = createHarness(0);
    const { scheduler, ticks } = createScheduler(harness, { activeMs: 5000, tickOnStart: true });
    scheduler.start();
    await harness.advance(0);
    expect(ticks).toHaveLength(1);

    // Simulate a throttled background tab: the clock passes the pending deadline without
    // the timer firing, then the user returns to the tab.
    harness.setVisible(false);
    harness.elapse(10000);
    harness.setVisible(true);
    await harness.advance(0);
    expect(ticks).toHaveLength(2); // catch-up fired
    // A second visibility event in the same visible period must not re-fire.
    harness.fireVisibilityChange();
    await harness.advance(0);
    expect(ticks).toHaveLength(2);
    scheduler.stop();
  });

  it('throttles the catch-up tick when the last tick is still fresh', async () => {
    const harness = createHarness(0);
    const { scheduler, ticks } = createScheduler(harness, { activeMs: 5000, tickOnStart: true });
    scheduler.start();
    await harness.advance(0);
    harness.setVisible(false);
    harness.elapse(1000); // still fresh (1s < 5s active)
    harness.setVisible(true);
    await harness.advance(0);
    expect(ticks).toHaveLength(1);
    scheduler.stop();
  });

  it('backs off exponentially on consecutive errors, capped, and resets on success', async () => {
    const harness = createHarness(0);
    const { scheduler } = createScheduler(harness, {
      activeMs: 5000,
      maxIntervalMs: 30000,
      tickResults: ['error', 'error', 'error', 'error', true]
    });
    scheduler.start();

    await harness.advance(2500); // error 1 → 5000×2 = 10000 → jittered 5000
    expect(harness.lastDelayMs).toBe(5000);
    await harness.advance(5000); // error 2 → 5000×4 = 20000 → jittered 10000
    expect(harness.lastDelayMs).toBe(10000);
    await harness.advance(10000); // error 3 → 5000×8 = 40000 → capped 30000 → jittered 15000
    expect(harness.lastDelayMs).toBe(15000);
    await harness.advance(15000); // error 4 → still capped → jittered 15000
    expect(harness.lastDelayMs).toBe(15000);
    await harness.advance(15000); // success → back to active
    expect(harness.lastDelayMs).toBe(2500);
    scheduler.stop();
  });

  it('pauses the chain when resolveIntervals returns null and stays stopped', async () => {
    const harness = createHarness(0);
    const { scheduler, ticks } = createScheduler(harness, {
      tickOnStart: true,
      pauseIntervalsOnce: true
    });
    scheduler.start();
    await harness.advance(0);
    expect(ticks).toHaveLength(1);
    expect(harness.hasPendingTimer).toBe(false);
    // Visibility changes must not resurrect a paused chain.
    harness.setVisible(false);
    harness.setVisible(true);
    await harness.advance(0);
    expect(ticks).toHaveLength(1);
    scheduler.stop();
  });

  it('stop() cancels the pending tick and double-stop is safe', async () => {
    const harness = createHarness(0);
    const { scheduler, ticks } = createScheduler(harness, { activeMs: 5000 });
    scheduler.start();
    scheduler.stop();
    scheduler.stop();
    await harness.advance(10000);
    expect(ticks).toHaveLength(0);
  });
});
