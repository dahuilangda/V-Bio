/**
 * Bicyclic Cys anchor layout semantics — the single source of truth shared by
 * the runtime settings UI, the submit payload and the task preview.
 *
 * The peptide length is a range [min, max]; a layout mode defines where the
 * three Cys anchors sit for EVERY candidate length in that range:
 *
 * - auto     : engine layout — first / nearest-midpoint interior / last.
 * - ring     : user pins the two ring sizes (residues strictly between the
 *              anchors). Cys3 rides the C-terminus, the anchor block stays
 *              rigid and the N-terminal flank absorbs the length range.
 * - ratio    : user pins percentages of the candidate length; anchors scale
 *              with every candidate (shape preserved, ring sizes flex).
 * - absolute : literal 1-based positions; only meaningful when the range is
 *              pinned to a single length (min === max).
 *
 * All positions are 1-based. Keep the math in sync with
 * backend `peplm/loop/constraints.py::resolve_bicyclic_anchors` —
 * round-half-up (`floor(x + 0.5)`) is used on BOTH sides, never
 * `Math.round`/`round` (banker's rounding differs between JS and Python).
 */

export type CysLayoutMode = 'auto' | 'ring' | 'ratio' | 'absolute';

export interface RingLayoutParams {
  ring1: number;
  ring2: number;
}

export interface RatioLayoutParams {
  pct1: number;
  pct2: number;
  pct3: number;
}

export interface AbsoluteLayoutParams {
  cys1: number;
  cys2: number;
  cys3: number;
}

export interface CysLayoutParams {
  ring: RingLayoutParams;
  ratio: RatioLayoutParams;
  absolute: AbsoluteLayoutParams;
}

export const MIN_RING_SIZE = 1;
export const MIN_ANCHOR_GAP = 2;

/** round-half-up, deterministic across JS/Python. */
export function roundHalfUp(value: number): number {
  return Math.floor(value + 0.5);
}

export function clampInt(value: number, minValue: number, maxValue: number): number {
  return Math.max(minValue, Math.min(maxValue, Math.floor(value)));
}

/** Smallest length at which a layout can place all three anchors legally. */
export function minFeasibleLength(
  mode: CysLayoutMode,
  params: CysLayoutParams,
  fixTerminalCys: boolean
): number {
  if (mode === 'ring') {
    return params.ring.ring1 + params.ring.ring2 + 3;
  }
  if (mode === 'ratio') {
    for (let length = 5; length <= 120; length += 1) {
      if (resolveAnchorsAtLength(mode, params, length, fixTerminalCys)) return length;
    }
    return 121;
  }
  if (mode === 'absolute') {
    return params.absolute.cys3;
  }
  return 5;
}

/**
 * Resolve the three 1-based anchor positions for one candidate length.
 * Returns null when the layout cannot fit this length (the caller must not
 * sample such lengths).
 */
export function resolveAnchorsAtLength(
  mode: CysLayoutMode,
  params: CysLayoutParams,
  length: number,
  fixTerminalCys: boolean
): [number, number, number] | null {
  if (!Number.isFinite(length) || length < 5) return null;
  if (mode === 'ring') {
    const cys3 = length;
    const cys2 = cys3 - params.ring.ring2 - 1;
    const cys1 = cys2 - params.ring.ring1 - 1;
    if (cys1 < 1) return null;
    return [cys1, cys2, cys3];
  }
  if (mode === 'ratio') {
    const { pct1, pct2, pct3 } = params.ratio;
    // forward fix: each anchor must leave >= MIN_ANCHOR_GAP to the next
    const cys1 = clampInt(roundHalfUp((pct1 / 100) * length), 1, length);
    let cys2 = clampInt(roundHalfUp((pct2 / 100) * length), 1, length);
    let cys3 = fixTerminalCys
      ? length
      : clampInt(roundHalfUp((pct3 / 100) * length), 1, length);
    cys2 = Math.max(cys2, cys1 + MIN_ANCHOR_GAP);
    cys3 = Math.max(cys3, cys2 + MIN_ANCHOR_GAP);
    if (cys3 > length) return null;
    return [cys1, cys2, cys3];
  }
  if (mode === 'absolute') {
    const { cys1, cys2, cys3 } = params.absolute;
    if (cys3 > length || cys2 > length || cys1 > length) return null;
    return [cys1, cys2, cys3];
  }
  // auto: first / interior midpoint / last (engine mirror of the Python rule)
  const interior = Math.max(2, Math.floor((length - 1) / 2) + 1);
  return [1, Math.min(interior, length - 1), length];
}

/** Anchors for every integer length in [minLength, maxLength] (null-skipped). */
export function anchorsAcrossRange(
  mode: CysLayoutMode,
  params: CysLayoutParams,
  minLength: number,
  maxLength: number,
  fixTerminalCys: boolean
): Array<{ length: number; anchors: [number, number, number] | null }> {
  const rows: Array<{ length: number; anchors: [number, number, number] | null }> = [];
  for (let length = minLength; length <= maxLength; length += 1) {
    rows.push({ length, anchors: resolveAnchorsAtLength(mode, params, length, fixTerminalCys) });
  }
  return rows;
}

/**
 * Human-facing validation for the whole (mode, params, range) triple.
 * Returns an error string (blocking submit) or null when valid.
 */
export function validateCysLayout(params: {
  mode: CysLayoutMode;
  layout: CysLayoutParams;
  lengthMin: number;
  lengthMax: number;
  fixTerminalCys: boolean;
}): string | null {
  const { mode, layout, lengthMin, lengthMax, fixTerminalCys } = params;
  if (lengthMin > lengthMax) {
    return `肽长度窗口无效：min ${lengthMin} > max ${lengthMax}。`;
  }
  if (mode === 'ring') {
    if (layout.ring.ring1 < MIN_RING_SIZE || layout.ring.ring2 < MIN_RING_SIZE) {
      return '环大小至少为 1 个残基。';
    }
    const core = minFeasibleLength('ring', layout, fixTerminalCys);
    if (core > lengthMax) {
      return `环 1 (${layout.ring.ring1}) + 环 2 (${layout.ring.ring2}) + 3 个 Cys 至少需要 ${core} aa，超过长度上限 ${lengthMax}。`;
    }
    return null;
  }
  if (mode === 'ratio') {
    const { pct1, pct2, pct3 } = layout.ratio;
    if (pct1 < 0 || pct2 < 0 || pct3 < 0 || pct1 > 100 || pct2 > 100 || pct3 > 100) {
      return '比例需在 0–100 之间。';
    }
    const feasible = minFeasibleLength('ratio', layout, fixTerminalCys);
    if (feasible > lengthMax) {
      return `当前比例即使拉到最大长度 ${lengthMax} 也放不下三个 Cys（间距至少 ${MIN_ANCHOR_GAP}）。请调开比例或缩短 Fix Terminal。`;
    }
    return null;
  }
  if (mode === 'absolute') {
    if (lengthMin !== lengthMax) {
      return '绝对位置要求固定的肽长度（min = max）。';
    }
    const { cys1, cys2, cys3 } = layout.absolute;
    if (cys3 > lengthMax) {
      return `Cys 3 位置 ${cys3} 超出肽长度 ${lengthMax}。`;
    }
    if (!(cys1 < cys2 && cys2 < cys3)) {
      return '三个 Cys 位置必须严格递增。';
    }
    if (cys2 - cys1 < MIN_ANCHOR_GAP || cys3 - cys2 < MIN_ANCHOR_GAP) {
      return `相邻 Cys 之间至少间隔 ${MIN_ANCHOR_GAP} 个残基。`;
    }
    return null;
  }
  return null;
}

/**
 * Non-blocking hint about how the layout interacts with the configured
 * range (raised floors, clamped sampling) — shown under the controls.
 */
export function cysLayoutRangeNotice(params: {
  mode: CysLayoutMode;
  layout: CysLayoutParams;
  lengthMin: number;
  lengthMax: number;
  fixTerminalCys: boolean;
}): string | null {
  const { mode, layout, lengthMin, fixTerminalCys } = params;
  if (mode === 'ring') {
    const core = minFeasibleLength('ring', layout, fixTerminalCys);
    if (lengthMin < core) {
      return `环占用 ${core} aa：长度低于 ${core} 的候选放不下该环，实际最小长度为 ${core}。`;
    }
    return null;
  }
  if (mode === 'ratio') {
    const feasible = minFeasibleLength('ratio', layout, fixTerminalCys);
    if (lengthMin < feasible) {
      return `长度低于 ${feasible} aa 的候选放不下三个 Cys，实际最小长度为 ${feasible}。`;
    }
    return null;
  }
  return null;
}

/** Convert a layout's anchors at the reference length into another mode. */
export function convertLayoutParams(
  from: CysLayoutMode,
  to: CysLayoutMode,
  params: CysLayoutParams,
  referenceLength: number,
  fixTerminalCys: boolean
): CysLayoutParams {
  const anchors = resolveAnchorsAtLength(from, params, referenceLength, fixTerminalCys);
  if (!anchors) return params;
  const [cys1, cys2, cys3] = anchors;
  const next: CysLayoutParams = { ...params };
  if (to === 'ring') {
    next.ring = {
      ring1: Math.max(MIN_RING_SIZE, cys2 - cys1 - 1),
      ring2: Math.max(MIN_RING_SIZE, cys3 - cys2 - 1)
    };
  } else if (to === 'ratio') {
    next.ratio = {
      pct1: clampInt((cys1 / referenceLength) * 100, 0, 100),
      pct2: clampInt((cys2 / referenceLength) * 100, 0, 100),
      pct3: clampInt((cys3 / referenceLength) * 100, 0, 100)
    };
  } else if (to === 'absolute') {
    next.absolute = { cys1, cys2, cys3 };
  }
  return next;
}

/**
 * Default Cys positions for the manual rail, matching the long-standing
 * 3 / 8 / 15 defaults of the orchestrator when feasible.
 */
export function defaultLayoutParams(referenceLength: number): CysLayoutParams {
  const cys3 = referenceLength;
  const cys2 = Math.min(8, Math.max(3, cys3 - 7));
  return {
    ring: { ring1: 4, ring2: 6 },
    ratio: { pct1: Math.round((3 / cys3) * 100), pct2: Math.round((cys2 / cys3) * 100), pct3: 100 },
    absolute: { cys1: 3, cys2, cys3 }
  };
}
