/** Pure helpers for the Copilot composer's ↑/↓ input-history navigation.

Extracted from the modal so the navigation math is unit-testable without React/DOM, and lives in
one place. The history is the list of *sent* user inputs (most recent last); ``appendInputHistory``
and ``nextInputHistoryNav`` are pure — the localStorage wrappers are thin I/O around them.
*/

/** Maximum sent inputs retained per user. */
export const INPUT_HISTORY_LIMIT = 50;

/** Navigation cursor: ``index`` into the history currently shown, plus the in-progress draft
 * captured when navigation began (restored when the user arrows back below the newest entry). */
export interface InputHistoryNav {
  index: number;
  draft: string;
}

/** Pure: append a sent input, trimming, deduping a consecutive duplicate, and capping to ``limit``. */
export function appendInputHistory(
  history: readonly string[],
  text: string,
  limit: number = INPUT_HISTORY_LIMIT
): string[] {
  const trimmed = String(text || '').trim();
  if (!trimmed) return [...history];
  if (history.length > 0 && history[history.length - 1] === trimmed) return [...history];
  const next = [...history, trimmed];
  return next.length > limit ? next.slice(next.length - limit) : next;
}

/**
 * Pure: compute the next navigation cursor and the draft value to show for one ↑/↓ step.
 *
 * Returns ``null`` when nothing should change (empty history, already at the oldest on ↑, or ↓
 * pressed while not navigating). On ↓ past the newest entry, returns ``{ nav: null, value }``
 * where ``value`` is the in-progress draft captured when navigation began.
 */
export function nextInputHistoryNav(
  history: readonly string[],
  current: InputHistoryNav | null,
  draft: string,
  direction: 'up' | 'down'
): { nav: InputHistoryNav | null; value: string } | null {
  if (history.length === 0) return null;
  if (direction === 'up') {
    const nextIndex = current ? current.index - 1 : history.length - 1;
    if (nextIndex < 0) return null;
    // The first time the user presses ↑, snapshot the draft they had so ↓ past the newest restores it.
    const startingDraft = current ? current.draft : draft;
    return { nav: { index: nextIndex, draft: startingDraft }, value: history[nextIndex] };
  }
  // direction === 'down'
  if (!current) return null;
  const nextIndex = current.index + 1;
  if (nextIndex >= history.length) {
    return { nav: null, value: current.draft };
  }
  return { nav: { index: nextIndex, draft: current.draft }, value: history[nextIndex] };
}

/**
 * Pure: whether an arrow key should navigate history instead of moving the caret. ↑ navigates only
 * on the first line (no newline before the caret); ↓ only on the last line (no newline after it).
 * This preserves normal per-line caret movement in a multi-line draft.
 */
export function shouldNavigateHistory(
  direction: 'up' | 'down',
  value: string,
  caret: number
): boolean {
  const safeCaret = Math.max(0, Math.min(caret, value.length));
  if (direction === 'up') return !value.slice(0, safeCaret).includes('\n');
  return !value.slice(safeCaret).includes('\n');
}

function normalizeUserId(userId: string): string {
  return String(userId || 'anonymous').trim().toLowerCase() || 'anonymous';
}

function inputHistoryStorageKey(userId: string): string {
  return ['vbio:copilot-input-history:v1', normalizeUserId(userId), 'global'].join(':');
}

/** Read this user's sent-input history from localStorage (best-effort; [] on any failure). */
export function readStoredInputHistory(userId: string): string[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(inputHistoryStorageKey(userId));
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string').slice(-INPUT_HISTORY_LIMIT)
      : [];
  } catch {
    return [];
  }
}

/** Persist this user's sent-input history to localStorage (best-effort; never throws). */
export function writeStoredInputHistory(userId: string, history: readonly string[]): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(
      inputHistoryStorageKey(userId),
      JSON.stringify(history.slice(-INPUT_HISTORY_LIMIT))
    );
  } catch {
    // History is a best-effort UI convenience.
  }
}
