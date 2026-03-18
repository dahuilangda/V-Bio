import type { TaskState } from '../../types/models';
import {
  inferTaskStateFromStatusPayload as inferRuntimeTaskStateFromStatusPayload,
  mapBackendTaskState,
  readTaskRuntimeStatusText
} from '../../utils/taskRuntime';

export type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';

export function mapTaskState(raw: string): TaskState {
  return mapBackendTaskState(raw);
}

export function readStatusText(task: { info?: Record<string, unknown>; state: string }) {
  return readTaskRuntimeStatusText(task);
}

export function inferTaskStateFromStatusPayload(
  task: { info?: Record<string, unknown>; state: string },
  currentStateInput?: unknown
): TaskState {
  return inferRuntimeTaskStateFromStatusPayload(task, currentStateInput);
}

export function findProgressPercent(data: unknown): number | null {
  if (typeof data !== 'object' || data === null) return null;
  const obj = data as Record<string, unknown>;

  const directCandidates = ['progress', 'percent', 'percentage', 'pct', 'ratio'];
  for (const key of directCandidates) {
    const value = obj[key];
    if (typeof value === 'number' && Number.isFinite(value)) {
      const normalized = value <= 1 ? value * 100 : value;
      if (normalized >= 0 && normalized <= 100) {
        return normalized;
      }
    }
  }

  const nestedCandidates = ['tracker', 'meta', 'details', 'info'];
  for (const key of nestedCandidates) {
    const nested = obj[key];
    const nestedPercent = findProgressPercent(nested);
    if (nestedPercent !== null) return nestedPercent;
  }

  const current = obj.current;
  const total = obj.total;
  if (typeof current === 'number' && typeof total === 'number' && total > 0) {
    return Math.min(100, Math.max(0, (current / total) * 100));
  }

  return null;
}

export function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const segment of path.split('.')) {
    if (!current || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

export function readFirstFiniteMetric(data: Record<string, unknown>, paths: string[]): number | null {
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

export function readFirstNonEmptyStringMetric(data: Record<string, unknown> | null, paths: string[]): string {
  if (!data) return '';
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return '';
}

export function readStringListMetric(data: Record<string, unknown> | null, paths: string[]): string[] {
  if (!data) return [];
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (!Array.isArray(value)) continue;
    const rows = value
      .filter((item): item is string => typeof item === 'string')
      .map((item) => item.trim())
      .filter(Boolean);
    if (rows.length > 0) return rows;
  }
  return [];
}

export function readLigandSmilesFromMap(data: Record<string, unknown> | null, preferredLigandChainId: string | null): string {
  if (!data) return '';
  const mapCandidates: unknown[] = [
    readObjectPath(data, 'ligand_smiles_map'),
    readObjectPath(data, 'ligand.smiles_map'),
    readObjectPath(data, 'ligand.smilesMap')
  ];
  const preferredChain = String(preferredLigandChainId || '').trim().toUpperCase();

  for (const mapValue of mapCandidates) {
    if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) continue;
    const entries = Object.entries(mapValue as Record<string, unknown>)
      .map(([key, value]) => {
        if (typeof value !== 'string') return null;
        const normalizedValue = value.trim();
        if (!normalizedValue) return null;
        return {
          key: String(key || '').trim(),
          value: normalizedValue
        };
      })
      .filter((item): item is { key: string; value: string } => item !== null);
    if (entries.length === 0) continue;

    if (preferredChain) {
      for (const entry of entries) {
        const key = entry.key.toUpperCase();
        const keyChain = key.includes(':') ? key.slice(0, key.indexOf(':')).trim() : key;
        if (keyChain === preferredChain) {
          return entry.value;
        }
      }
    }

    if (entries.length === 1) {
      return entries[0].value;
    }
  }

  return '';
}

export function splitChainTokens(value: string): string[] {
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

function normalizeProbability(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

function normalizeChainToken(value: string): string {
  return String(value || '').trim().toUpperCase();
}

function chainTokenEquals(a: string, b: string): boolean {
  const left = normalizeChainToken(a);
  const right = normalizeChainToken(b);
  if (!left || !right) return false;
  if (left === right) return true;
  const compactLeft = left.replace(/[^A-Z0-9]/g, '');
  const compactRight = right.replace(/[^A-Z0-9]/g, '');
  if (compactLeft && compactRight && compactLeft === compactRight) return true;
  const leftTokens = left.split(/[^A-Z0-9]+/).filter(Boolean);
  if (leftTokens.includes(right) || (compactRight && leftTokens.includes(compactRight))) {
    return true;
  }
  if (compactLeft && compactRight) {
    if (compactLeft.startsWith(compactRight) || compactLeft.endsWith(compactRight)) {
      return true;
    }
    if (compactRight.startsWith(compactLeft) || compactRight.endsWith(compactLeft)) {
      return true;
    }
  }
  return false;
}

function isNumericToken(value: string): boolean {
  return /^\d+$/.test(String(value || '').trim());
}

function readPairValueFromNestedMap(
  mapValue: unknown,
  chainA: string,
  chainB: string
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const rowA =
    byChain[chainA] && typeof byChain[chainA] === 'object' && !Array.isArray(byChain[chainA])
      ? (byChain[chainA] as Record<string, unknown>)
      : (() => {
          for (const [key, value] of Object.entries(byChain)) {
            if (!chainTokenEquals(key, chainA) && !chainTokenEquals(chainA, key)) continue;
            if (value && typeof value === 'object' && !Array.isArray(value)) {
              return value as Record<string, unknown>;
            }
          }
          return null;
        })();
  if (!rowA) return null;
  const directValue = rowA[chainB];
  if (directValue !== undefined) {
    return normalizeProbability(toFiniteNumber(directValue));
  }
  for (const [key, value] of Object.entries(rowA)) {
    if (!chainTokenEquals(key, chainB) && !chainTokenEquals(chainB, key)) continue;
    return normalizeProbability(toFiniteNumber(value));
  }
  return null;
}

function readPairValueFromNumericMap(
  mapValue: unknown,
  chainA: string,
  chainB: string,
  chainOrderHints: string[],
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length === 0 || !keys.every((item) => isNumericToken(item))) return null;

  const idxA = chainOrderHints.findIndex((hint) => chainTokenEquals(hint, chainA));
  const idxB = chainOrderHints.findIndex((hint) => chainTokenEquals(hint, chainB));
  if (idxA >= 0 && idxB >= 0 && idxA !== idxB) {
    const ligandToTarget = readPairValueFromNestedMap(byChain, String(idxB), String(idxA));
    const targetToLigand = readPairValueFromNestedMap(byChain, String(idxA), String(idxB));
    if (ligandToTarget !== null && targetToLigand !== null && preferredDirectionalIptm !== null) {
      const ligandDelta = Math.abs(ligandToTarget - preferredDirectionalIptm);
      const targetDelta = Math.abs(targetToLigand - preferredDirectionalIptm);
      return ligandDelta <= targetDelta ? ligandToTarget : targetToLigand;
    }
    if (ligandToTarget !== null) return ligandToTarget;
    if (targetToLigand !== null) return targetToLigand;
  }

  if (keys.length === 2 && preferredDirectionalIptm !== null) {
    const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
    const forward = readPairValueFromNestedMap(byChain, first, second);
    const backward = readPairValueFromNestedMap(byChain, second, first);
    if (forward !== null && backward !== null) {
      const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
      const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
      return forwardDelta <= backwardDelta ? forward : backward;
    }
    if (forward !== null) return forward;
    if (backward !== null) return backward;
  }
  return null;
}

function readPairValueFromAnyTwoKeyMap(
  mapValue: unknown,
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  if (preferredDirectionalIptm === null) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length !== 2) return null;
  const [first, second] = keys;
  const forward = readPairValueFromNestedMap(byChain, first, second);
  const backward = readPairValueFromNestedMap(byChain, second, first);
  if (forward === null && backward === null) return null;
  if (forward !== null && backward !== null) {
    const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
    const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
    return forwardDelta <= backwardDelta ? forward : backward;
  }
  return forward ?? backward;
}

export function readPairIptmForChains(
  confidence: Record<string, unknown> | null,
  chainA: string | null,
  chainB: string | null,
  fallbackChainIds: string[]
): number | null {
  if (!confidence || !chainA || !chainB) return null;
  const sameChain = chainTokenEquals(chainA, chainB);

  const chainIdsRaw = readObjectPath(confidence, 'chain_ids');
  const chainIds =
    Array.isArray(chainIdsRaw) && chainIdsRaw.every((value) => typeof value === 'string')
      ? (chainIdsRaw as string[])
      : fallbackChainIds;
  const preferredDirectionalIptm = normalizeProbability(
    readFirstFiniteMetric(confidence, ['ligand_iptm', 'iptm'])
  );

  const pairMap = readObjectPath(confidence, 'pair_chains_iptm');
  if (!sameChain) {
    const ligandToTarget = readPairValueFromNestedMap(pairMap, chainB, chainA);
    if (ligandToTarget !== null) return ligandToTarget;
    const targetToLigand = readPairValueFromNestedMap(pairMap, chainA, chainB);
    if (targetToLigand !== null) return targetToLigand;
  }
  const numericMapped = readPairValueFromNumericMap(pairMap, chainA, chainB, chainIds, preferredDirectionalIptm);
  if (numericMapped !== null) return numericMapped;
  const twoKeyMapped = readPairValueFromAnyTwoKeyMap(pairMap, preferredDirectionalIptm);
  if (twoKeyMapped !== null) return twoKeyMapped;

  const matrix = readObjectPath(confidence, 'chain_pair_iptm') ?? readObjectPath(confidence, 'chain_pair_iptm_global');
  if (Array.isArray(matrix)) {
    const i = chainIds.findIndex((value) => chainTokenEquals(value, chainA));
    const j = chainIds.findIndex((value) => chainTokenEquals(value, chainB));
    if (i >= 0 && j >= 0 && i !== j) {
      const rowI = matrix[i];
      const rowJ = matrix[j];
      const ligandToTarget = Array.isArray(rowJ) ? normalizeProbability(toFiniteNumber(rowJ[i])) : null;
      const targetToLigand = Array.isArray(rowI) ? normalizeProbability(toFiniteNumber(rowI[j])) : null;
      if (ligandToTarget !== null && targetToLigand !== null && preferredDirectionalIptm !== null) {
        const ligandDelta = Math.abs(ligandToTarget - preferredDirectionalIptm);
        const targetDelta = Math.abs(targetToLigand - preferredDirectionalIptm);
        return ligandDelta <= targetDelta ? ligandToTarget : targetToLigand;
      }
      if (ligandToTarget !== null) return ligandToTarget;
      if (targetToLigand !== null) return targetToLigand;
    }
  }

  return null;
}

export function readChainMeanPlddtForChain(confidence: Record<string, unknown> | null, chainId: string | null): number | null {
  if (!confidence || !chainId) return null;
  const map = readObjectPath(confidence, 'chain_mean_plddt');
  if (!map || typeof map !== 'object' || Array.isArray(map)) return null;
  const value = toFiniteNumber((map as Record<string, unknown>)[chainId]);
  if (value === null) return null;
  return value >= 0 && value <= 1 ? value * 100 : value;
}

export function readFiniteMetricSeries(data: Record<string, unknown>, paths: string[]): number[] {
  const values: number[] = [];
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'number' && Number.isFinite(value)) values.push(value);
  }
  return values;
}

export function toneForPlddt(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const normalized = value <= 1 ? value * 100 : value;
  if (normalized >= 90) return 'excellent';
  if (normalized >= 70) return 'good';
  if (normalized >= 50) return 'medium';
  return 'low';
}

export function toneForProbability(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const normalized = value <= 1 ? value * 100 : value;
  if (normalized >= 90) return 'excellent';
  if (normalized >= 70) return 'good';
  if (normalized >= 50) return 'medium';
  return 'low';
}

export function toneForIptm(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const normalized = value <= 1 ? value : value / 100;
  if (normalized >= 0.8) return 'excellent';
  if (normalized >= 0.6) return 'good';
  if (normalized >= 0.4) return 'medium';
  return 'low';
}

export function toneForIc50(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value <= 0.1) return 'excellent';
  if (value <= 1) return 'good';
  if (value <= 10) return 'medium';
  return 'low';
}

export function mean(values: number[]): number {
  if (values.length === 0) return Number.NaN;
  return values.reduce((acc, value) => acc + value, 0) / values.length;
}

export function normalizePlddtValue(value: number): number {
  if (!Number.isFinite(value)) return 0;
  const normalized = value >= 0 && value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, normalized));
}

export function std(values: number[]): number {
  if (values.length <= 1) return 0;
  const m = mean(values);
  const variance = values.reduce((acc, value) => acc + (value - m) ** 2, 0) / values.length;
  return Math.sqrt(Math.max(0, variance));
}
