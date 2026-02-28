import type { TaskState } from '../../types/models';

export type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';

export function mapTaskState(raw: string): TaskState {
  const normalized = raw.toUpperCase();
  if (normalized === 'SUCCESS') return 'SUCCESS';
  if (normalized === 'FAILURE') return 'FAILURE';
  if (normalized === 'REVOKED') return 'REVOKED';
  if (normalized === 'PENDING' || normalized === 'RECEIVED' || normalized === 'RETRY') return 'QUEUED';
  if (normalized === 'STARTED' || normalized === 'RUNNING' || normalized === 'PROGRESS') return 'RUNNING';
  return 'QUEUED';
}

function resolveNonRegressiveTaskState(currentStateInput: unknown, incomingState: TaskState): TaskState {
  const current = String(currentStateInput || '').trim().toUpperCase();
  if (!current) return incomingState;
  if (current === 'RUNNING' && incomingState === 'QUEUED') return 'RUNNING';
  if ((current === 'SUCCESS' || current === 'FAILURE' || current === 'REVOKED') && (incomingState === 'QUEUED' || incomingState === 'RUNNING')) {
    return current as TaskState;
  }
  return incomingState;
}

export function readStatusText(task: { info?: Record<string, unknown>; state: string }) {
  if (!task.info) return task.state;
  const s1 = task.info.status;
  const s2 = task.info.message;
  if (typeof s1 === 'string' && s1.trim()) return s1;
  if (typeof s2 === 'string' && s2.trim()) return s2;
  return task.state;
}

export function inferTaskStateFromStatusPayload(
  task: { info?: Record<string, unknown>; state: string },
  currentStateInput?: unknown
): TaskState {
  const mapped = mapTaskState(task.state);
  const statusText = readStatusText(task).trim().toLowerCase();
  const hinted =
    mapped === 'QUEUED' &&
    (statusText.includes('running') ||
      statusText.includes('started') ||
      statusText.includes('starting') ||
      statusText.includes('acquiring') ||
      statusText.includes('preparing') ||
      statusText.includes('uploading') ||
      statusText.includes('processing') ||
      statusText.includes('termination in progress'))
      ? 'RUNNING'
      : mapped;
  return resolveNonRegressiveTaskState(currentStateInput, hinted);
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
  if (!compactLeft || !compactRight) return false;
  return compactLeft === compactRight;
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
  const directA = byChain[chainA];
  const directB = byChain[chainB];
  const v1 =
    directA && typeof directA === 'object' && !Array.isArray(directA)
      ? normalizeProbability(toFiniteNumber((directA as Record<string, unknown>)[chainB]))
      : null;
  const v2 =
    directB && typeof directB === 'object' && !Array.isArray(directB)
      ? normalizeProbability(toFiniteNumber((directB as Record<string, unknown>)[chainA]))
      : null;
  if (v1 === null && v2 === null) return null;
  return Math.max(v1 ?? Number.NEGATIVE_INFINITY, v2 ?? Number.NEGATIVE_INFINITY);
}

function readPairValueFromNumericMap(
  mapValue: unknown,
  chainA: string,
  chainB: string,
  chainOrderHints: string[]
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length === 0 || !keys.every((item) => isNumericToken(item))) return null;

  const idxA = chainOrderHints.findIndex((hint) => chainTokenEquals(hint, chainA));
  const idxB = chainOrderHints.findIndex((hint) => chainTokenEquals(hint, chainB));
  if (idxA >= 0 && idxB >= 0 && idxA !== idxB) {
    const mapped = readPairValueFromNestedMap(byChain, String(idxA), String(idxB));
    if (mapped !== null) return mapped;
  }

  if (keys.length === 2) {
    const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
    const inferred = readPairValueFromNestedMap(byChain, first, second);
    if (inferred !== null) return inferred;
  }
  return null;
}

export function readPairIptmForChains(
  confidence: Record<string, unknown> | null,
  chainA: string | null,
  chainB: string | null,
  fallbackChainIds: string[]
): number | null {
  if (!confidence || !chainA || !chainB) return null;
  if (chainA === chainB) return null;

  const chainIdsRaw = readObjectPath(confidence, 'chain_ids');
  const chainIds =
    Array.isArray(chainIdsRaw) && chainIdsRaw.every((value) => typeof value === 'string')
      ? (chainIdsRaw as string[])
      : fallbackChainIds;

  const pairMap = readObjectPath(confidence, 'pair_chains_iptm');
  const direct = readPairValueFromNestedMap(pairMap, chainA, chainB);
  if (direct !== null) return direct;
  const numericMapped = readPairValueFromNumericMap(pairMap, chainA, chainB, chainIds);
  if (numericMapped !== null) return numericMapped;

  const matrix = readObjectPath(confidence, 'chain_pair_iptm') ?? readObjectPath(confidence, 'chain_pair_iptm_global');
  if (Array.isArray(matrix)) {
    const i = chainIds.findIndex((value) => chainTokenEquals(value, chainA));
    const j = chainIds.findIndex((value) => chainTokenEquals(value, chainB));
    if (i >= 0 && j >= 0) {
      const rowI = matrix[i];
      const rowJ = matrix[j];
      const m1 = Array.isArray(rowI) ? normalizeProbability(toFiniteNumber(rowI[j])) : null;
      const m2 = Array.isArray(rowJ) ? normalizeProbability(toFiniteNumber(rowJ[i])) : null;
      if (m1 !== null || m2 !== null) return Math.max(m1 ?? Number.NEGATIVE_INFINITY, m2 ?? Number.NEGATIVE_INFINITY);
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
