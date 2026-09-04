/**
 * Adaptive poll scheduler — the timing engine shared by every workspace poller.
 *
 * Design notes ( distilled from ecosystem practice: TanStack Query's refetchInterval +
 * focusManager, SWR's refreshInterval/refreshWhenHidden/focusThrottleInterval, and AWS'
 * equal-jitter backoff guidance ):
 *
 * 1. FAST while the subject is visibly progressing, SLOW after a run of unchanged ticks,
 *    STOPPED when the caller says so (terminal state = caller never schedules again).
 *    Both query libraries expose exactly this via the function-form interval.
 * 2. The tab's visibility gates cadence: hidden tabs poll at `hiddenIntervalMultiplier`
 *    (paused entirely is the libraries' default; long-running jobs here opt into a slow
 *    multiplier instead so returning users keep continuity), and becoming visible again
 *    triggers ONE immediate catch-up tick — the SWR/TanStack refetch-on-focus behavior —
 *    skipped when the last tick is younger than the active interval (focus throttle).
 * 3. Delays get equal jitter (`delay/2 + random(0, delay/2)`, AWS) so many clients never
 *    synchronize into a thundering herd, and the delay never drops below half the base.
 * 4. Ticks are single-flight: a slow response never overlaps the next request; the next
 *    delay is measured from completion.
 * 5. Consecutive tick ERRORS double the delay up to `maxIntervalMs` (back off a failing
 *    endpoint) and reset on the first successful tick.
 */

export interface AdaptivePollIntervals {
  /** Cadence while the subject is changing (fresh progress). */
  activeMs: number;
  /** Cadence after `idleAfterUnchangedTicks` consecutive ticks without change. */
  idleMs: number;
}

export interface AdaptivePollSchedulerOptions {
  /**
   * Evaluated before every delay computation so callers can derive intervals from the
   * latest state (e.g. QUEUED vs RUNNING) without recreating the scheduler. Returning
   * `null` pauses the chain until the next `start()` — the scheduler equivalent of
   * TanStack Query's interval function returning `false`.
   */
  resolveIntervals: () => AdaptivePollIntervals | null;
  /** Consecutive unchanged ticks before the cadence drops from activeMs to idleMs. */
  idleAfterUnchangedTicks: number;
  /** Delay multiplier while the document is hidden. */
  hiddenIntervalMultiplier: number;
  /** Upper bound for any computed delay (before jitter). */
  maxIntervalMs: number;
  /** Fire one tick immediately on `start()` (mount-time freshness), like a query's initial fetch. */
  tickOnStart?: boolean;
  /** Return value contract: `true` when the tick observed a change (resets fast cadence + error backoff). */
  tick: () => Promise<boolean>;
}

export interface AdaptivePollScheduler {
  /** Idempotent: starts the tick chain and the visibility listener. */
  start: () => void;
  /** Cancels the pending timer and the visibility listener; safe to call twice. */
  stop: () => void;
}

/** Injectable clock + visibility so unit tests are deterministic without monkey-patching globals. */
export interface AdaptivePollSchedulerEnv {
  now: () => number;
  setTimer: (handler: () => void, timeoutMs: number) => number;
  clearTimer: (handle: number) => void;
  isDocumentVisible: () => boolean;
  onVisibilityChange: (handler: () => void) => () => void;
  /** Random source for jitter; injectable so tests can assert exact delays. */
  random: () => number;
}

function createDefaultEnv(): AdaptivePollSchedulerEnv {
  return {
    now: () => Date.now(),
    setTimer: (handler, timeoutMs) => window.setTimeout(handler, timeoutMs),
    clearTimer: (handle) => window.clearTimeout(handle),
    isDocumentVisible: () =>
      typeof document === 'undefined' || document.visibilityState === 'visible',
    onVisibilityChange: (handler) => {
      if (typeof document === 'undefined') return () => undefined;
      document.addEventListener('visibilitychange', handler);
      return () => document.removeEventListener('visibilitychange', handler);
    },
    random: Math.random
  };
}

/** AWS "equal jitter": keep half the computed delay, randomize the rest. */
export function applyEqualJitter(delayMs: number, random: () => number): number {
  const half = delayMs / 2;
  return Math.round(half + random() * half);
}

export function createAdaptivePollScheduler(
  options: AdaptivePollSchedulerOptions,
  envInput?: Partial<AdaptivePollSchedulerEnv>
): AdaptivePollScheduler {
  const env = { ...createDefaultEnv(), ...envInput };
  const random = env.random;
  let timerHandle: number | null = null;
  let unsubscribeVisibility: (() => void) | null = null;
  let stopped = true;
  let chainScheduled = false;
  let tickInFlight = false;
  let consecutiveUnchanged = 0;
  let consecutiveErrors = 0;
  let lastTickCompletedAt = 0;
  // One catch-up tick per visibility regain; reset when the tab hides again.
  let catchUpFiredForCurrentVisiblePeriod = false;

  const computeDelayMs = (): number | null => {
    const intervals = options.resolveIntervals();
    if (!intervals) return null;
    const isQuiet = consecutiveUnchanged >= options.idleAfterUnchangedTicks;
    let baseMs = isQuiet ? intervals.idleMs : intervals.activeMs;
    // Error backoff doubles the delay per consecutive failure; any change resets it.
    baseMs *= Math.pow(2, consecutiveErrors);
    if (!env.isDocumentVisible()) {
      baseMs *= options.hiddenIntervalMultiplier;
    }
    baseMs = Math.min(baseMs, options.maxIntervalMs);
    return Math.max(0, applyEqualJitter(baseMs, random));
  };

  const scheduleNext = () => {
    if (stopped) return;
    const delayMs = computeDelayMs();
    if (delayMs === null) {
      // Caller paused the chain (e.g. nothing left to poll); start() resumes it.
      chainScheduled = false;
      return;
    }
    chainScheduled = true;
    timerHandle = env.setTimer(() => {
      void runTick();
    }, delayMs);
  };

  const runTick = async (): Promise<void> => {
    if (stopped || tickInFlight) return;
    tickInFlight = true;
    try {
      const changed = await options.tick();
      consecutiveErrors = 0;
      if (changed) {
        consecutiveUnchanged = 0;
      } else {
        consecutiveUnchanged += 1;
      }
    } catch {
      // A failing endpoint is backed off, not hammered; state keeps the last known values.
      consecutiveUnchanged += 1;
      consecutiveErrors += 1;
    } finally {
      tickInFlight = false;
      lastTickCompletedAt = env.now();
      scheduleNext();
    }
  };

  const onVisibilityChange = () => {
    if (stopped || !chainScheduled) return;
    if (!env.isDocumentVisible()) {
      catchUpFiredForCurrentVisiblePeriod = false;
      return;
    }
    if (catchUpFiredForCurrentVisiblePeriod || tickInFlight) return;
    // Refetch-on-focus, throttled: skip when the last tick is still fresh.
    const intervals = options.resolveIntervals();
    if (!intervals) return;
    if (env.now() - lastTickCompletedAt < intervals.activeMs) return;
    catchUpFiredForCurrentVisiblePeriod = true;
    if (timerHandle !== null) {
      env.clearTimer(timerHandle);
      timerHandle = null;
    }
    void runTick();
  };

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      unsubscribeVisibility = env.onVisibilityChange(onVisibilityChange);
      if (options.tickOnStart) {
        void runTick();
      } else {
        scheduleNext();
      }
    },
    stop() {
      if (stopped) return;
      stopped = true;
      chainScheduled = false;
      if (timerHandle !== null) {
        env.clearTimer(timerHandle);
        timerHandle = null;
      }
      if (unsubscribeVisibility) {
        unsubscribeVisibility();
        unsubscribeVisibility = null;
      }
    }
  };
}
