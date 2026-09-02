import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  downloadResultBlob,
  getTaskStatus,
  getTaskStatuses,
  parseResultBundle,
  submitPrediction
} from '../../api/backendApi';
import type {
  InputComponent,
  VirtualScreeningPredictionRecord,
  VirtualScreeningStructureBackend
} from '../../types/models';
import { assignChainIdsForComponents } from '../../utils/chainAssignments';
import { buildTaskRuntimeFailureMessage } from '../../utils/taskRuntime';
import {
  extractPredictionMetricsFromStatusInfo,
  extractPredictionResultPayload,
  inferPredictionRuntimeStateFromStatusPayload
} from './leadopt/hooks/leadOptPredictionHelpers';

export interface VirtualScreeningPredictionHit {
  id: string;
  smiles: string;
}

interface UseVirtualScreeningPredictionsParams {
  components: InputComponent[];
  initialRecords?: Record<string, VirtualScreeningPredictionRecord>;
  onRecordsChange?: (records: Record<string, VirtualScreeningPredictionRecord>) => void;
  onError?: (message: string) => void;
}

const BACKEND_KEYS = new Set<VirtualScreeningStructureBackend>(['boltz', 'protenix', 'alphafold3']);

function normalizeBackend(value: unknown): VirtualScreeningStructureBackend {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'af3') return 'alphafold3';
  return BACKEND_KEYS.has(token as VirtualScreeningStructureBackend)
    ? token as VirtualScreeningStructureBackend
    : 'boltz';
}

function hashIdentity(value: string): string {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}

export function buildVirtualScreeningPredictionRecordKey(
  backendInput: unknown,
  hitIdInput: unknown,
  smilesInput: unknown
): string {
  const backend = normalizeBackend(backendInput);
  const hitId = String(hitIdInput || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'hit';
  const smiles = String(smilesInput || '').trim();
  return `${backend}::${hitId}::${hashIdentity(`${String(hitIdInput || '').trim()}\n${smiles}`)}`;
}

export function compactVirtualScreeningPredictionRecords(
  records: Record<string, VirtualScreeningPredictionRecord>
): Record<string, VirtualScreeningPredictionRecord> {
  const next: Record<string, VirtualScreeningPredictionRecord> = {};
  for (const [key, record] of Object.entries(records || {})) {
    if (!record?.taskId) continue;
    next[key] = {
      taskId: record.taskId,
      backend: normalizeBackend(record.backend),
      state: record.state,
      ligandPlddt: record.ligandPlddt ?? null,
      interfaceMetricValue: record.interfaceMetricValue ?? null,
      interfaceMetricLabel: record.interfaceMetricLabel === 'ipTM' ? 'ipTM' : 'IPSAE',
      pairIptm: record.pairIptm ?? null,
      pairPae: record.pairPae ?? null,
      error: String(record.error || '').slice(0, 800),
      updatedAt: Number.isFinite(record.updatedAt) ? record.updatedAt : Date.now()
    };
  }
  return next;
}

function sameRecord(
  left: VirtualScreeningPredictionRecord | undefined,
  right: VirtualScreeningPredictionRecord | undefined
): boolean {
  if (left === right) return true;
  if (!left || !right) return false;
  return left.taskId === right.taskId &&
    left.backend === right.backend &&
    left.state === right.state &&
    left.ligandPlddt === right.ligandPlddt &&
    left.interfaceMetricValue === right.interfaceMetricValue &&
    left.interfaceMetricLabel === right.interfaceMetricLabel &&
    left.pairIptm === right.pairIptm &&
    left.pairPae === right.pairPae &&
    left.error === right.error &&
    left.updatedAt === right.updatedAt &&
    left.structureText === right.structureText &&
    left.structureFormat === right.structureFormat &&
    left.structureName === right.structureName &&
    left.ligandRenderSmiles === right.ligandRenderSmiles &&
    left.ligandRenderAtomPlddts === right.ligandRenderAtomPlddts &&
    left.resultBundleHydrated === right.resultBundleHydrated;
}

function sameRecordMap(
  left: Record<string, VirtualScreeningPredictionRecord>,
  right: Record<string, VirtualScreeningPredictionRecord>
): boolean {
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  if (leftKeys.length !== rightKeys.length) return false;
  return leftKeys.every((key) => sameRecord(left[key], right[key]));
}

function mergePersistedRecords(
  current: Record<string, VirtualScreeningPredictionRecord>,
  incoming: Record<string, VirtualScreeningPredictionRecord>
): Record<string, VirtualScreeningPredictionRecord> {
  const next: Record<string, VirtualScreeningPredictionRecord> = {};
  for (const [key, record] of Object.entries(incoming || {})) {
    const hydrated = current[key];
    if (!hydrated || hydrated.taskId !== record.taskId) {
      next[key] = record;
      continue;
    }
    next[key] = {
      ...hydrated,
      ...record,
      ...(hydrated.structureText ? { structureText: hydrated.structureText } : {}),
      ...(hydrated.structureFormat ? { structureFormat: hydrated.structureFormat } : {}),
      ...(hydrated.structureName ? { structureName: hydrated.structureName } : {}),
      ...(hydrated.ligandRenderSmiles ? { ligandRenderSmiles: hydrated.ligandRenderSmiles } : {}),
      ...(hydrated.ligandRenderAtomPlddts
        ? { ligandRenderAtomPlddts: hydrated.ligandRenderAtomPlddts }
        : {}),
      ...(hydrated.resultBundleHydrated ? { resultBundleHydrated: true } : {})
    };
  }
  return next;
}

function buildTriageComponents(components: InputComponent[], candidateSmiles: string): InputComponent[] {
  const targetComplex = components
    .filter((component) => component.type === 'protein' || component.type === 'ligand')
    .filter((component) => Boolean(String(component.sequence || '').trim()))
    .map((component) => component.type === 'protein'
      ? {
          ...component,
          // Preserve the target's MSA choice for per-hit structure prediction.
          useMsa: component.useMsa !== false,
          cyclic: false,
          modifications: []
        }
      : { ...component });
  return [
    ...targetComplex,
    {
      id: 'virtual-screening-triage-binder',
      type: 'ligand',
      numCopies: 1,
      sequence: candidateSmiles,
      inputMethod: 'smiles'
    }
  ];
}

export function useVirtualScreeningPredictions({
  components,
  initialRecords = {},
  onRecordsChange,
  onError
}: UseVirtualScreeningPredictionsParams) {
  const [records, setRecords] = useState<Record<string, VirtualScreeningPredictionRecord>>(initialRecords);
  const recordsRef = useRef(records);
  const onRecordsChangeRef = useRef(onRecordsChange);
  const onErrorRef = useRef(onError);
  const hydrationByTaskRef = useRef<Record<string, Promise<VirtualScreeningPredictionRecord | null>>>({});

  useEffect(() => {
    onRecordsChangeRef.current = onRecordsChange;
  }, [onRecordsChange]);

  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  useEffect(() => {
    const next = mergePersistedRecords(recordsRef.current, initialRecords || {});
    if (sameRecordMap(recordsRef.current, next)) return;
    recordsRef.current = next;
    setRecords(next);
  }, [initialRecords]);

  const commitRecords = useCallback((next: Record<string, VirtualScreeningPredictionRecord>) => {
    if (sameRecordMap(recordsRef.current, next)) return;
    recordsRef.current = next;
    setRecords(next);
    onRecordsChangeRef.current?.(compactVirtualScreeningPredictionRecords(next));
  }, []);

  const updateRecord = useCallback((key: string, record: VirtualScreeningPredictionRecord) => {
    commitRecords({
      ...recordsRef.current,
      [key]: record
    });
  }, [commitRecords]);

  const baseTriageComponents = useMemo(() => buildTriageComponents(components, 'C'), [components]);
  const chains = useMemo(() => {
    const assignments = assignChainIdsForComponents(baseTriageComponents);
    const targetIndex = baseTriageComponents.findIndex((component) => component.type === 'protein');
    return {
      targetChain: assignments[targetIndex]?.[0] || 'A',
      ligandChain: assignments[assignments.length - 1]?.[0] || 'B'
    };
  }, [baseTriageComponents]);
  const normalizedTargetSequence = useMemo(() => {
    const protein = components.find((component) => component.type === 'protein');
    return String(protein?.sequence || '').replace(/\s+/g, '').toUpperCase();
  }, [components]);
  const targetReady = useMemo(() => {
    const supported = components.filter((component) => component.type === 'protein' || component.type === 'ligand');
    return supported.some((component) => component.type === 'protein' && Boolean(component.sequence.trim())) &&
      supported.every((component) => Boolean(component.sequence.trim()));
  }, [components]);

  const pendingSignature = useMemo(
    () => Object.entries(records)
      .filter(([, record]) => {
        if (record.state !== 'QUEUED' && record.state !== 'RUNNING') return false;
        return Boolean(record.taskId) && !record.taskId.startsWith('local:');
      })
      .map(([key, record]) => `${key}|${record.taskId}|${record.state}`)
      .sort()
      .join('||'),
    [records]
  );

  useEffect(() => {
    if (!pendingSignature) return;
    let cancelled = false;
    let timer: number | null = null;

    const poll = async () => {
      const pending = Object.entries(recordsRef.current).filter(([, record]) => {
        if (record.state !== 'QUEUED' && record.state !== 'RUNNING') return false;
        return Boolean(record.taskId) && !record.taskId.startsWith('local:');
      });
      if (!pending.length) return;
      try {
        const statusByTaskId = await getTaskStatuses(pending.map(([, record]) => record.taskId));
        if (cancelled) return;
        let next = recordsRef.current;
        let changed = false;
        for (const [key, record] of pending) {
          const current = recordsRef.current[key] || record;
          if (current.taskId !== record.taskId) continue;
          const status = statusByTaskId[record.taskId];
          if (!status) continue;
          const runtimeState = inferPredictionRuntimeStateFromStatusPayload(status);
          if (!runtimeState) continue;
          let updated: VirtualScreeningPredictionRecord = current;
          if (runtimeState === 'SUCCESS') {
            const metrics = extractPredictionMetricsFromStatusInfo(status.info, chains.targetChain, chains.ligandChain);
            updated = {
              ...current,
              state: 'SUCCESS',
              ligandPlddt: metrics.ligandPlddt ?? current.ligandPlddt,
              interfaceMetricValue: metrics.interfaceMetricValue ?? current.interfaceMetricValue,
              interfaceMetricLabel: metrics.interfaceMetricLabel,
              pairIptm: metrics.pairIptm ?? current.pairIptm,
              pairPae: metrics.pairPae ?? current.pairPae,
              ...(metrics.ligandRenderSmiles ? { ligandRenderSmiles: metrics.ligandRenderSmiles } : {}),
              ...(metrics.ligandRenderAtomPlddts.length > 0
                ? { ligandRenderAtomPlddts: metrics.ligandRenderAtomPlddts }
                : {}),
              error: '',
              updatedAt: Date.now()
            };
          } else if (runtimeState === 'FAILURE') {
            updated = {
              ...current,
              state: 'FAILURE',
              error: buildTaskRuntimeFailureMessage(status, 'Structure prediction failed.'),
              updatedAt: Date.now()
            };
          } else if (runtimeState !== current.state) {
            updated = {
              ...current,
              state: runtimeState,
              error: '',
              updatedAt: Date.now()
            };
          }
          if (sameRecord(current, updated)) continue;
          if (!changed) next = { ...next };
          next[key] = updated;
          changed = true;
        }
        if (changed) {
          commitRecords(next);
          return;
        }
      } catch {
        // A transient status error should not turn a queued prediction into a failure.
      }
      if (cancelled) return;
      const hasRunning = pending.some(([, record]) => record.state === 'RUNNING');
      // A hidden tab doubles the delay instead of stopping entirely — progress resumes
      // promptly on return while a backgrounded tab halves its request rate.
      const hidden = document.visibilityState === 'hidden';
      const delay = hasRunning ? 4000 : 6500;
      timer = window.setTimeout(() => void poll(), hidden ? delay * 2 : delay);
    };

    void poll();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [chains.ligandChain, chains.targetChain, commitRecords, pendingSignature]);

  const runPrediction = useCallback(async (
    hit: VirtualScreeningPredictionHit,
    backendInput: VirtualScreeningStructureBackend
  ): Promise<VirtualScreeningPredictionRecord | null> => {
    const smiles = String(hit.smiles || '').trim();
    const backend = normalizeBackend(backendInput);
    const key = buildVirtualScreeningPredictionRecordKey(backend, hit.id, smiles);
    if (!smiles) {
      onErrorRef.current?.('This screening row does not contain a SMILES value.');
      return null;
    }
    if (!targetReady) {
      onErrorRef.current?.('Complete the target complex before predicting a structure.');
      return null;
    }

    const localTaskId = `local:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
    const queued: VirtualScreeningPredictionRecord = {
      taskId: localTaskId,
      backend,
      state: 'QUEUED',
      ligandPlddt: null,
      interfaceMetricValue: null,
      interfaceMetricLabel: 'IPSAE',
      pairIptm: null,
      pairPae: null,
      error: '',
      updatedAt: Date.now()
    };
    updateRecord(key, queued);

    try {
      const predictionComponents = buildTriageComponents(components, smiles);
      const taskId = await submitPrediction({
        projectId: 'virtual-screening-triage',
        projectName: 'Virtual Screening triage',
        proteinSequence: normalizedTargetSequence,
        ligandSmiles: smiles,
        workflow: 'prediction',
        components: predictionComponents,
        constraints: [],
        properties: {
          affinity: true,
          target: chains.targetChain,
          ligand: chains.ligandChain,
          binder: chains.ligandChain
        },
        backend,
        useMsa: predictionComponents.some(
          (component) => component.type === 'protein' && component.useMsa !== false
        ),
        seed: null,
        lowVram: false
      });
      const persisted: VirtualScreeningPredictionRecord = {
        ...queued,
        taskId,
        updatedAt: Date.now()
      };
      updateRecord(key, persisted);
      return persisted;
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to submit structure prediction.';
      const failed: VirtualScreeningPredictionRecord = {
        ...queued,
        state: 'FAILURE',
        error: message,
        updatedAt: Date.now()
      };
      updateRecord(key, failed);
      onErrorRef.current?.(message);
      return failed;
    }
  }, [
    chains.ligandChain,
    chains.targetChain,
    components,
    normalizedTargetSequence,
    targetReady,
    updateRecord
  ]);

  const ensureResult = useCallback(async (
    hit: VirtualScreeningPredictionHit,
    backendInput: VirtualScreeningStructureBackend
  ): Promise<VirtualScreeningPredictionRecord | null> => {
    const smiles = String(hit.smiles || '').trim();
    const backend = normalizeBackend(backendInput);
    const key = buildVirtualScreeningPredictionRecordKey(backend, hit.id, smiles);
    let record = recordsRef.current[key];
    if (!record) return null;
    if (record.structureText?.trim() && record.resultBundleHydrated) return record;
    if (!record.taskId || record.taskId.startsWith('local:')) return record;

    const existingPromise = hydrationByTaskRef.current[record.taskId];
    if (existingPromise) return existingPromise;

    const hydration = (async (): Promise<VirtualScreeningPredictionRecord | null> => {
      try {
        if (record.state !== 'SUCCESS') {
          const status = await getTaskStatus(record.taskId);
          const runtimeState = inferPredictionRuntimeStateFromStatusPayload(status);
          if (runtimeState === 'FAILURE') {
            const failed: VirtualScreeningPredictionRecord = {
              ...record,
              state: 'FAILURE',
              error: buildTaskRuntimeFailureMessage(status, 'Structure prediction failed.'),
              updatedAt: Date.now()
            };
            updateRecord(key, failed);
            return failed;
          }
          if (runtimeState !== 'SUCCESS') return record;
          const metrics = extractPredictionMetricsFromStatusInfo(status.info, chains.targetChain, chains.ligandChain);
          record = {
            ...record,
            state: 'SUCCESS',
            ligandPlddt: metrics.ligandPlddt ?? record.ligandPlddt,
            interfaceMetricValue: metrics.interfaceMetricValue ?? record.interfaceMetricValue,
            interfaceMetricLabel: metrics.interfaceMetricLabel,
            pairIptm: metrics.pairIptm ?? record.pairIptm,
            pairPae: metrics.pairPae ?? record.pairPae,
            error: '',
            updatedAt: Date.now()
          };
          updateRecord(key, record);
        }

        const blob = await downloadResultBlob(record.taskId, { mode: 'view' });
        const parsed = await parseResultBundle(blob);
        const payload = extractPredictionResultPayload(parsed, chains.targetChain, chains.ligandChain, smiles);
        if (!payload.structureText.trim()) {
          throw new Error('Prediction completed, but its result archive has no readable structure.');
        }
        const hydrated: VirtualScreeningPredictionRecord = {
          ...record,
          state: 'SUCCESS',
          ligandPlddt: payload.ligandPlddt ?? record.ligandPlddt,
          interfaceMetricValue: payload.interfaceMetricValue ?? record.interfaceMetricValue,
          interfaceMetricLabel: payload.interfaceMetricLabel,
          pairIptm: payload.pairIptm ?? record.pairIptm,
          pairPae: payload.pairPae ?? record.pairPae,
          structureText: payload.structureText,
          structureFormat: payload.structureFormat,
          structureName: payload.structureName,
          ligandRenderSmiles: payload.ligandRenderSmiles || smiles,
          ligandRenderAtomPlddts: payload.ligandRenderAtomPlddts,
          resultBundleHydrated: true,
          error: '',
          updatedAt: Date.now()
        };
        updateRecord(key, hydrated);
        return hydrated;
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Failed to load the structure result.';
        const current = recordsRef.current[key] || record;
        const withError: VirtualScreeningPredictionRecord = {
          ...current,
          error: message,
          updatedAt: Date.now()
        };
        updateRecord(key, withError);
        onErrorRef.current?.(message);
        return withError;
      } finally {
        delete hydrationByTaskRef.current[record.taskId];
      }
    })();
    hydrationByTaskRef.current[record.taskId] = hydration;
    return hydration;
  }, [chains.ligandChain, chains.targetChain, updateRecord]);

  const recordForHit = useCallback((
    hit: VirtualScreeningPredictionHit,
    backendInput: VirtualScreeningStructureBackend
  ): VirtualScreeningPredictionRecord | null => {
    const key = buildVirtualScreeningPredictionRecordKey(backendInput, hit.id, hit.smiles);
    return records[key] || null;
  }, [records]);

  return {
    records,
    targetReady,
    targetChain: chains.targetChain,
    ligandChain: chains.ligandChain,
    runPrediction,
    ensureResult,
    recordForHit
  };
}
