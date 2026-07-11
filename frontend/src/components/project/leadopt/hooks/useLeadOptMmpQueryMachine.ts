import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  clusterLeadOptimizationMmp,
  downloadResultBlob,
  enumerateLeadOptimizationMmp,
  fetchLeadOptimizationMmpEvidence,
  fetchLeadOptimizationMmpQueryResult,
  getTaskStatus,
  type LeadOptMmpEvidenceResponse,
  parseResultBundle,
  predictLeadOptimizationCandidate,
  queryLeadOptimizationMmp
} from '../../../../api/backendApi';
import { buildTaskRuntimeFailureMessage } from '../../../../utils/taskRuntime';
import type {
  LeadOptDirection as Direction,
  LeadOptGroupedByEnvironment as GroupedByEnvironmentMode,
  LeadOptQueryMode as QueryMode,
  LeadOptQueryProperty as QueryProperty,
  LeadOptVariableMode as VariableMode
} from './useLeadOptMmpQueryForm';
import {
  hasResolvedPredictionMetrics,
  hasHydratedPredictionResult,
  buildPendingPredictionEntries,
  buildPendingPredictionSignature,
  buildLeadOptPredictionRecordKey,
  buildMissingResultArchiveMessage,
  buildQueuedPredictionRecord,
  computeHydrationRetryDelayMs,
  computeRuntimePollBatchSize,
  computeRuntimePollDelayMs,
  extractPredictionMetricsFromStatusInfo,
  extractPredictionResultPayload,
  inferPendingRuntimeStateFromError,
  inferPredictionRuntimeStateFromStatusPayload,
  isMmpQueryExpiredError,
  isResultArchiveMissingError,
  isResultArchivePendingError,
  mergePredictionRecordMapsNonRegressive,
  normalizePredictionBackendStrict,
  normalizePredictionMap,
  normalizeReferencePredictionMap,
  pickPredictionRenderContract,
  resolveNonRegressiveRuntimeState,
  shouldHydratePredictionRecord,
  shouldProbeTaskStatus,
  sliceRoundRobin,
  summarizePredictionRecords,
  asRecord,
  asRecordArray,
  formatMetric,
  readBoolean,
  readNumber,
  readText,
  sortScore,
  ENABLE_BACKGROUND_CANDIDATE_RESULT_HYDRATION,
  ENABLE_BACKGROUND_REFERENCE_RESULT_HYDRATION,
  RESULT_HYDRATION_MAX_RETRIES,
  RUNTIME_STATUS_CANDIDATE_BATCH_SIZE,
  RUNTIME_STATUS_HIDDEN_POLL_DELAY_MULTIPLIER,
  RUNTIME_STATUS_IDLE_POLL_DELAY_MS,
  RUNTIME_STATUS_REFERENCE_BATCH_SIZE,
  type LeadOptMmpPersistedSnapshot,
  type LeadOptPredictionRecord
} from './leadOptPredictionHelpers';

// Re-export names that external callers still import from this module.
export { buildLeadOptPredictionRecordKey, parseLeadOptPredictionRecordKey } from './leadOptPredictionHelpers';
export type { LeadOptMmpPersistedSnapshot, LeadOptPredictionRecord } from './leadOptPredictionHelpers';

interface VariableItemInput {
  query: string;
  mode: VariableMode;
  fragment_id?: string;
  atom_indices?: number[];
}

interface RunMmpQueryInput {
  canQuery: boolean;
  effectiveLigandSmiles: string;
  variableItems: VariableItemInput[];
  constantQuery: string;
  direction: Direction;
  queryProperty: QueryProperty;
  mmpDatabaseId: string;
  queryMode: QueryMode;
  groupedByEnvironment: GroupedByEnvironmentMode;
  minPairs: number;
  envRadius: number;
  selectedFragmentIds?: string[];
  selectedFragmentAtomIndices?: number[];
  onTaskQueued?: (payload: { taskId: string; requestPayload: Record<string, unknown> }) => void | Promise<void>;
  onTaskCompleted?: (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => void | Promise<void>;
  onTaskFailed?: (payload: { taskId: string; error: string }) => void | Promise<void>;
}

interface RunMmpQueryResult {
  queryId: string;
  transformCount: number;
  candidateCount: number;
}

interface UseLeadOptMmpQueryMachineParams {
  proteinSequence: string;
  targetChain: string;
  ligandChain: string;
  backend: string;
  onError: (message: string | null) => void;
  onPredictionQueued?: (payload: { taskId: string; backend: string; candidateSmiles: string }) => void | Promise<void>;
  onPredictionStateChange?: (payload: {
    records: Record<string, LeadOptPredictionRecord>;
    referenceRecords: Record<string, LeadOptPredictionRecord>;
    summary: {
      total: number;
      queued: number;
      running: number;
      success: number;
      failure: number;
      latestTaskId: string;
    };
  }) => void | Promise<void>;
}

type ClusterGroupBy = 'to' | 'from' | 'rule_env_radius';

interface PredictionRuntimePollerDeps {
  pendingEntries: Array<[string, LeadOptPredictionRecord]>;
  batchSize: number;
  cursorRef: { current: number };
  retryTimerRef: { current: Record<string, number> };
  retryCountRef: { current: Record<string, number> };
  setState: (updater: (prev: Record<string, LeadOptPredictionRecord>) => Record<string, LeadOptPredictionRecord>) => void;
  runtimeStatusDirectProbeAtRef: { current: Record<string, number> };
  targetChain: string;
  ligandChain: string;
  logLabel: string;
}

function createPredictionRuntimePoller(deps: PredictionRuntimePollerDeps): () => void {
  const {
    pendingEntries: pendingEntriesAll,
    batchSize,
    cursorRef,
    retryTimerRef,
    retryCountRef,
    setState,
    runtimeStatusDirectProbeAtRef,
    targetChain,
    ligandChain,
    logLabel
  } = deps;

  let cancelled = false;
  let timer: number | null = null;

  const computeDelayMs = (hasRunning: boolean, hasQueued: boolean) => {
    const baseDelay = computeRuntimePollDelayMs({ hasRunning, hasQueued });
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
      return baseDelay * RUNTIME_STATUS_HIDDEN_POLL_DELAY_MULTIPLIER;
    }
    return baseDelay;
  };

  const scheduleNext = (hasRunning: boolean, hasQueued: boolean) => {
    if (cancelled) return;
    timer = window.setTimeout(() => {
      void pollOnce();
    }, computeDelayMs(hasRunning, hasQueued));
  };

  const pollOnce = async () => {
    let hasRunning = false;
    let hasQueued = false;
    try {
      hasRunning = pendingEntriesAll.some(([, record]) => String(record.state || '').toUpperCase() === 'RUNNING');
      hasQueued = pendingEntriesAll.some(([, record]) => String(record.state || '').toUpperCase() === 'QUEUED');
      const pollBatchSize = computeRuntimePollBatchSize(
        pendingEntriesAll.length,
        batchSize
      );
      const { items: pendingEntries, nextCursor } = sliceRoundRobin(
        pendingEntriesAll,
        pollBatchSize,
        cursorRef.current
      );
      cursorRef.current = nextCursor;
      if (pendingEntries.length === 0) return;

      for (const [predictionKey, record] of pendingEntries) {
        if (cancelled) return;
        const taskId = readText(record.taskId).trim();
        if (!taskId) continue;
        if (!shouldProbeTaskStatus(runtimeStatusDirectProbeAtRef.current, taskId)) continue;
        let status: { task_id: string; state: string; info?: Record<string, unknown> } | undefined;
        try {
          status = await getTaskStatus(taskId);
        } catch (err) {
          console.error(`${logLabel} runtime status poll failed; skipping this cycle.`, err);
          continue;
        }
        if (!status) continue;
        try {
          if (cancelled) return;
          const runtimeState = inferPredictionRuntimeStateFromStatusPayload(status);
          if (!runtimeState) continue;
          if (runtimeState === 'SUCCESS') {
            const metrics = extractPredictionMetricsFromStatusInfo(status.info, targetChain, ligandChain);
            delete runtimeStatusDirectProbeAtRef.current[taskId];
            const retryTimer = retryTimerRef.current[predictionKey];
            if (retryTimer) {
              window.clearTimeout(retryTimer);
              delete retryTimerRef.current[predictionKey];
            }
            delete retryCountRef.current[predictionKey];
            setState((prev) => {
              const current = prev[predictionKey] || record;
              const nextPairIptm = metrics.hasMetrics ? metrics.pairIptm : current.pairIptm;
              const nextPairPae = metrics.hasMetrics ? metrics.pairPae : current.pairPae;
              const nextLigandPlddt = metrics.hasMetrics ? metrics.ligandPlddt : current.ligandPlddt;
              const nextLigandAtomPlddts = metrics.hasMetrics ? metrics.ligandAtomPlddts : current.ligandAtomPlddts;
              const renderContract = pickPredictionRenderContract(metrics, current);
              return {
                ...prev,
                [predictionKey]: {
                  ...current,
                  state: 'SUCCESS',
                  pairIptm: nextPairIptm,
                  interfaceMetricValue: metrics.hasMetrics ? metrics.interfaceMetricValue : current.interfaceMetricValue,
                  interfaceMetricLabel: metrics.hasMetrics ? metrics.interfaceMetricLabel : current.interfaceMetricLabel,
                  interfaceMetricSource: metrics.hasMetrics ? metrics.interfaceMetricSource : current.interfaceMetricSource,
                  pairPae: nextPairPae,
                  pairIptmResolved:
                    metrics.hasMetrics ||
                    current.pairIptmResolved === true ||
                    hasResolvedPredictionMetrics(current),
                  ligandPlddt: nextLigandPlddt,
                  ligandAtomPlddts: nextLigandAtomPlddts,
                  ...(renderContract.ligandRenderSmiles ? { ligandRenderSmiles: renderContract.ligandRenderSmiles } : {}),
                  ...(renderContract.ligandRenderAtomPlddts.length > 0 ? { ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts } : {}),
                  error: '',
                  updatedAt: Date.now()
                }
              };
            });
            continue;
          }
          if (runtimeState === 'FAILURE') {
            delete runtimeStatusDirectProbeAtRef.current[taskId];
            const retryTimer = retryTimerRef.current[predictionKey];
            if (retryTimer) {
              window.clearTimeout(retryTimer);
              delete retryTimerRef.current[predictionKey];
            }
            delete retryCountRef.current[predictionKey];
            const errorText = buildTaskRuntimeFailureMessage(
              status as { state: string; info?: Record<string, unknown> },
              'Prediction failed.'
            );
            setState((prev) => ({
              ...prev,
              [predictionKey]: {
                ...(prev[predictionKey] || record),
                state: 'FAILURE',
                error: errorText || 'Prediction failed.',
                updatedAt: Date.now()
              }
            }));
            continue;
          }
          if (runtimeState !== 'QUEUED' && runtimeState !== 'RUNNING') continue;
          setState((prev) => {
            const current = prev[predictionKey] || record;
            const nextRuntimeState = resolveNonRegressiveRuntimeState(current.state, runtimeState);
            if (!nextRuntimeState) return prev;
            if (String(current.state || '').toUpperCase() === nextRuntimeState) return prev;
            return {
              ...prev,
              [predictionKey]: {
                ...current,
                state: nextRuntimeState,
                updatedAt: Date.now()
              }
            };
          });
        } catch (err) {
          console.error(`${logLabel} status payload processing failed; keeping existing state.`, err);
          // Keep existing state on transient status errors.
        }
      }
    } finally {
      scheduleNext(hasRunning, hasQueued);
    }
  };

  scheduleNext(true, false);

  return () => {
    cancelled = true;
    if (timer) {
      window.clearTimeout(timer);
    }
  };
}

export function useLeadOptMmpQueryMachine({
  proteinSequence,
  targetChain,
  ligandChain,
  backend,
  onError,
  onPredictionQueued,
  onPredictionStateChange
}: UseLeadOptMmpQueryMachineParams) {
  const [loading, setLoading] = useState(false);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [queryNotice, setQueryNotice] = useState('');

  const [queryId, setQueryId] = useState('');
  const [activeQueryMode, setActiveQueryMode] = useState<QueryMode>('one-to-many');
  const [clusterGroupBy, setClusterGroupBy] = useState<ClusterGroupBy>('to');
  const [queryMinPairs, setQueryMinPairs] = useState(1);
  const [globalCount, setGlobalCount] = useState(0);
  const [queryStats, setQueryStats] = useState<Record<string, unknown>>({});

  const [transforms, setTransforms] = useState<Array<Record<string, unknown>>>([]);
  const [clusters, setClusters] = useState<Array<Record<string, unknown>>>([]);
  const [activeTransformId, setActiveTransformId] = useState('');
  const [activeEvidence, setActiveEvidence] = useState<LeadOptMmpEvidenceResponse | null>(null);

  const [selectedTransformIds, setSelectedTransformIds] = useState<string[]>([]);
  const [selectedClusterIds, setSelectedClusterIds] = useState<string[]>([]);
  const [enumeratedCandidates, setEnumeratedCandidates] = useState<Array<Record<string, unknown>>>([]);
  const [lastPredictionTaskId, setLastPredictionTaskId] = useState('');
  const [lastMmpTaskId, setLastMmpTaskId] = useState('');
  const [mmpRunVersion, setMmpRunVersion] = useState(0);
  const [predictionBySmiles, setPredictionBySmiles] = useState<Record<string, LeadOptPredictionRecord>>({});
  const [referencePredictionByBackend, setReferencePredictionByBackend] = useState<Record<string, LeadOptPredictionRecord>>({});
  const [runtimeStatusPollingEnabled, setRuntimeStatusPollingEnabled] = useState(false);

  const mmpQueryInFlightRef = useRef(false);
  const lastMmpQueryAtRef = useRef(0);
  const queryResultCacheRef = useRef<Map<string, Record<string, unknown>>>(new Map());
  const queryIdRef = useRef('');
  const predictionHydrationRetryCountRef = useRef<Record<string, number>>({});
  const predictionHydrationRetryTimerRef = useRef<Record<string, number>>({});
  const predictionHydrationInFlightRef = useRef<Set<string>>(new Set());
  const referenceHydrationRetryCountRef = useRef<Record<string, number>>({});
  const referenceHydrationRetryTimerRef = useRef<Record<string, number>>({});
  const referenceHydrationInFlightRef = useRef<Set<string>>(new Set());
  // Keep a stable hook slot for dev fast-refresh compatibility.
  const runtimeStatusDirectProbeAtRef = useRef<Record<string, number>>({});
  const predictionRuntimePollCursorRef = useRef(0);
  const referenceRuntimePollCursorRef = useRef(0);

  useEffect(() => {
    queryIdRef.current = queryId;
  }, [queryId]);

  useEffect(() => {
    return () => {
      for (const timerId of Object.values(predictionHydrationRetryTimerRef.current)) {
        window.clearTimeout(timerId);
      }
      for (const timerId of Object.values(referenceHydrationRetryTimerRef.current)) {
        window.clearTimeout(timerId);
      }
      predictionHydrationRetryTimerRef.current = {};
      referenceHydrationRetryTimerRef.current = {};
      predictionHydrationRetryCountRef.current = {};
      referenceHydrationRetryCountRef.current = {};
      runtimeStatusDirectProbeAtRef.current = {};
      predictionHydrationInFlightRef.current.clear();
      referenceHydrationInFlightRef.current.clear();
    };
  }, []);

  const hasSelection = selectedTransformIds.length > 0;
  const pendingCandidateEntries = useMemo(
    () => buildPendingPredictionEntries(predictionBySmiles),
    [predictionBySmiles]
  );
  const pendingReferenceEntries = useMemo(
    () => buildPendingPredictionEntries(referencePredictionByBackend),
    [referencePredictionByBackend]
  );
  const pendingCandidateSignature = useMemo(
    () => buildPendingPredictionSignature(pendingCandidateEntries),
    [pendingCandidateEntries]
  );
  const pendingReferenceSignature = useMemo(
    () => buildPendingPredictionSignature(pendingReferenceEntries),
    [pendingReferenceEntries]
  );

  const clearSelections = useCallback(() => {
    setSelectedTransformIds([]);
    setSelectedClusterIds([]);
  }, []);

  useEffect(() => {
    if (!runtimeStatusPollingEnabled) return;
    return createPredictionRuntimePoller({
      pendingEntries: pendingCandidateEntries,
      batchSize: RUNTIME_STATUS_CANDIDATE_BATCH_SIZE,
      cursorRef: predictionRuntimePollCursorRef,
      retryTimerRef: predictionHydrationRetryTimerRef,
      retryCountRef: predictionHydrationRetryCountRef,
      setState: setPredictionBySmiles,
      runtimeStatusDirectProbeAtRef,
      targetChain,
      ligandChain,
      logLabel: 'Candidate'
    });
  }, [ligandChain, pendingCandidateEntries, pendingCandidateSignature, runtimeStatusPollingEnabled, targetChain]);

  useEffect(() => {
    if (!runtimeStatusPollingEnabled) return;
    return createPredictionRuntimePoller({
      pendingEntries: pendingReferenceEntries,
      batchSize: RUNTIME_STATUS_REFERENCE_BATCH_SIZE,
      cursorRef: referenceRuntimePollCursorRef,
      retryTimerRef: referenceHydrationRetryTimerRef,
      retryCountRef: referenceHydrationRetryCountRef,
      setState: setReferencePredictionByBackend,
      runtimeStatusDirectProbeAtRef,
      targetChain,
      ligandChain,
      logLabel: 'Reference'
    });
  }, [ligandChain, pendingReferenceEntries, pendingReferenceSignature, runtimeStatusPollingEnabled, targetChain]);

  useEffect(() => {
    if (!runtimeStatusPollingEnabled) return;
    const hasPendingCandidates = pendingCandidateEntries.length > 0;
    const hasPendingReferences = pendingReferenceEntries.length > 0;
    if (hasPendingCandidates || hasPendingReferences) return;
    const timer = window.setTimeout(() => {
      setRuntimeStatusPollingEnabled(false);
    }, RUNTIME_STATUS_IDLE_POLL_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
    };
  }, [
    pendingCandidateEntries.length,
    pendingCandidateSignature,
    pendingReferenceEntries.length,
    pendingReferenceSignature,
    runtimeStatusPollingEnabled
  ]);

  useEffect(() => {
    if (!ENABLE_BACKGROUND_CANDIDATE_RESULT_HYDRATION) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    const hydrationEntries = Object.entries(predictionBySmiles)
      .filter(([, record]) => shouldHydratePredictionRecord(record))
      .filter(([smiles]) => !predictionHydrationInFlightRef.current.has(smiles))
      .sort((a, b) => Number(a[1]?.updatedAt || 0) - Number(b[1]?.updatedAt || 0))
      .slice(0, 1);
    if (hydrationEntries.length === 0) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        for (const [smiles, record] of hydrationEntries) {
          if (cancelled) return;
          const taskId = readText(record.taskId).trim();
          if (!taskId || taskId.startsWith('local:')) continue;
          if (predictionHydrationInFlightRef.current.has(smiles)) continue;
          predictionHydrationInFlightRef.current.add(smiles);
          try {
            const blob = await downloadResultBlob(taskId, { mode: 'view' });
            if (cancelled) return;
            const parsed = await parseResultBundle(blob);
            if (!parsed) continue;
            const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain, smiles);
            setPredictionBySmiles((prev) => {
              const current = prev[smiles] || record;
              if (!current) return prev;
              const renderContract = pickPredictionRenderContract(resultPayload, current);
              return {
                ...prev,
                [smiles]: {
                  ...current,
                  state: 'SUCCESS',
                  pairIptm: resultPayload.pairIptm,
                  interfaceMetricValue: resultPayload.interfaceMetricValue,
                  interfaceMetricLabel: resultPayload.interfaceMetricLabel,
                  interfaceMetricSource: resultPayload.interfaceMetricSource,
                  pairPae: resultPayload.pairPae,
                  pairIptmResolved: true,
                  ligandPlddt: resultPayload.ligandPlddt,
                  ligandAtomPlddts: resultPayload.ligandAtomPlddts,
                  ...(renderContract.ligandRenderSmiles ? { ligandRenderSmiles: renderContract.ligandRenderSmiles } : {}),
                  ...(renderContract.ligandRenderAtomPlddts.length > 0 ? { ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts } : {}),
                  ...(resultPayload.structureText.trim()
                    ? {
                        structureText: resultPayload.structureText,
                        structureFormat: resultPayload.structureFormat,
                        structureName: resultPayload.structureName
                      }
                    : {}),
                  resultBundleHydrated: true,
                  error: '',
                  updatedAt: Date.now()
                }
              };
            });
            const retryTimer = predictionHydrationRetryTimerRef.current[smiles];
            if (retryTimer) {
              window.clearTimeout(retryTimer);
              delete predictionHydrationRetryTimerRef.current[smiles];
            }
            delete predictionHydrationRetryCountRef.current[smiles];
          } catch (error) {
            if (isResultArchiveMissingError(error)) {
              setPredictionBySmiles((prev) => {
                const current = prev[smiles] || record;
                if (!current) return prev;
                return {
                  ...prev,
                  [smiles]: {
                    ...current,
                    state: 'FAILURE',
                    error: buildMissingResultArchiveMessage(current.taskId || record.taskId),
                    updatedAt: Date.now()
                  }
                };
              });
              continue;
            }
            if (isResultArchivePendingError(error)) {
              const attempt = Number(predictionHydrationRetryCountRef.current[smiles] || 0) + 1;
              if (attempt > RESULT_HYDRATION_MAX_RETRIES) {
                delete predictionHydrationRetryCountRef.current[smiles];
                continue;
              }
              predictionHydrationRetryCountRef.current[smiles] = attempt;
              if (!predictionHydrationRetryTimerRef.current[smiles]) {
                const delayMs = computeHydrationRetryDelayMs(attempt);
                predictionHydrationRetryTimerRef.current[smiles] = window.setTimeout(() => {
                  delete predictionHydrationRetryTimerRef.current[smiles];
                  setPredictionBySmiles((prev) => {
                    const current = prev[smiles];
                    if (!current) return prev;
                    return {
                      ...prev,
                      [smiles]: {
                        ...current,
                        updatedAt: Date.now()
                      }
                    };
                  });
                }, delayMs);
              }
              continue;
            }
            setPredictionBySmiles((prev) => {
              const current = prev[smiles] || record;
              if (!current) return prev;
              return {
                ...prev,
                [smiles]: {
                  ...current,
                  error: readText(error instanceof Error ? error.message : error).trim() || current.error || '',
                  updatedAt: Date.now()
                }
              };
            });
          } finally {
            predictionHydrationInFlightRef.current.delete(smiles);
          }
        }
      })();
    }, 900);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [ligandChain, predictionBySmiles, targetChain]);

  useEffect(() => {
    if (!ENABLE_BACKGROUND_REFERENCE_RESULT_HYDRATION) return;
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    const hydrationEntries = Object.entries(referencePredictionByBackend)
      .filter(([, record]) => shouldHydratePredictionRecord(record))
      .filter(([backendKey]) => !referenceHydrationInFlightRef.current.has(backendKey))
      .sort((a, b) => Number(a[1]?.updatedAt || 0) - Number(b[1]?.updatedAt || 0))
      .slice(0, 1);
    if (hydrationEntries.length === 0) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      void (async () => {
        for (const [backendKey, record] of hydrationEntries) {
          if (cancelled) return;
          const taskId = readText(record.taskId).trim();
          if (!taskId || taskId.startsWith('local:')) continue;
          if (referenceHydrationInFlightRef.current.has(backendKey)) continue;
          referenceHydrationInFlightRef.current.add(backendKey);
          try {
            const blob = await downloadResultBlob(taskId, { mode: 'view' });
            if (cancelled) return;
            const parsed = await parseResultBundle(blob);
            if (!parsed) continue;
            const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain);
            setReferencePredictionByBackend((prev) => {
              const current = prev[backendKey] || record;
              if (!current) return prev;
              const renderContract = pickPredictionRenderContract(resultPayload, current);
              return {
                ...prev,
                [backendKey]: {
                  ...current,
                  state: 'SUCCESS',
                  pairIptm: resultPayload.pairIptm,
                  interfaceMetricValue: resultPayload.interfaceMetricValue,
                  interfaceMetricLabel: resultPayload.interfaceMetricLabel,
                  interfaceMetricSource: resultPayload.interfaceMetricSource,
                  pairPae: resultPayload.pairPae,
                  pairIptmResolved: true,
                  ligandPlddt: resultPayload.ligandPlddt,
                  ligandAtomPlddts: resultPayload.ligandAtomPlddts,
                  ...(renderContract.ligandRenderSmiles ? { ligandRenderSmiles: renderContract.ligandRenderSmiles } : {}),
                  ...(renderContract.ligandRenderAtomPlddts.length > 0 ? { ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts } : {}),
                  ...(resultPayload.structureText.trim()
                    ? {
                        structureText: resultPayload.structureText,
                        structureFormat: resultPayload.structureFormat,
                        structureName: resultPayload.structureName
                      }
                    : {}),
                  resultBundleHydrated: true,
                  error: '',
                  updatedAt: Date.now()
                }
              };
            });
            const retryTimer = referenceHydrationRetryTimerRef.current[backendKey];
            if (retryTimer) {
              window.clearTimeout(retryTimer);
              delete referenceHydrationRetryTimerRef.current[backendKey];
            }
            delete referenceHydrationRetryCountRef.current[backendKey];
          } catch (error) {
            if (isResultArchiveMissingError(error)) {
              setReferencePredictionByBackend((prev) => {
                const current = prev[backendKey] || record;
                if (!current) return prev;
                return {
                  ...prev,
                  [backendKey]: {
                    ...current,
                    state: 'FAILURE',
                    error: buildMissingResultArchiveMessage(current.taskId || record.taskId),
                    updatedAt: Date.now()
                  }
                };
              });
              continue;
            }
            if (isResultArchivePendingError(error)) {
              const attempt = Number(referenceHydrationRetryCountRef.current[backendKey] || 0) + 1;
              if (attempt > RESULT_HYDRATION_MAX_RETRIES) {
                delete referenceHydrationRetryCountRef.current[backendKey];
                continue;
              }
              referenceHydrationRetryCountRef.current[backendKey] = attempt;
              if (!referenceHydrationRetryTimerRef.current[backendKey]) {
                const delayMs = computeHydrationRetryDelayMs(attempt);
                referenceHydrationRetryTimerRef.current[backendKey] = window.setTimeout(() => {
                  delete referenceHydrationRetryTimerRef.current[backendKey];
                  setReferencePredictionByBackend((prev) => {
                    const current = prev[backendKey];
                    if (!current) return prev;
                    return {
                      ...prev,
                      [backendKey]: {
                        ...current,
                        updatedAt: Date.now()
                      }
                    };
                  });
                }, delayMs);
              }
              continue;
            }
            setReferencePredictionByBackend((prev) => {
              const current = prev[backendKey] || record;
              if (!current) return prev;
              return {
                ...prev,
                [backendKey]: {
                  ...current,
                  error: readText(error instanceof Error ? error.message : error).trim() || current.error || '',
                  updatedAt: Date.now()
                }
              };
            });
          } finally {
            referenceHydrationInFlightRef.current.delete(backendKey);
          }
        }
      })();
    }, 900);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [ligandChain, referencePredictionByBackend, targetChain]);

  useEffect(() => {
    if (typeof onPredictionStateChange !== 'function') return;
    const records = predictionBySmiles;
    const summary = summarizePredictionRecords(records);
    void onPredictionStateChange({ records, referenceRecords: referencePredictionByBackend, summary });
  }, [onPredictionStateChange, predictionBySmiles, referencePredictionByBackend]);

  const runCluster = useCallback(
    async (
      id: string,
      minPairs: number,
      groupBy: ClusterGroupBy = clusterGroupBy
    ): Promise<Array<Record<string, unknown>>> => {
      if (!id) return [];
      try {
        const clusterResponse = await clusterLeadOptimizationMmp({
          query_id: id,
          group_by: groupBy,
          min_pairs: minPairs
        });
        const rows = Array.isArray(clusterResponse.clusters)
          ? (clusterResponse.clusters as Array<Record<string, unknown>>)
          : [];
        setClusterGroupBy(groupBy);
        setQueryMinPairs(Math.max(1, minPairs));
        setClusters(rows);
        const cached = queryResultCacheRef.current.get(id);
        if (cached) {
          queryResultCacheRef.current.set(id, {
            ...cached,
            clusters: rows,
            min_pairs: Math.max(1, minPairs),
            cluster_group_by: groupBy
          });
        }
        return rows;
      } catch (e) {
        onError(e instanceof Error ? e.message : 'MMP cluster failed.');
        return [];
      }
    },
    [clusterGroupBy, onError]
  );

  const runMmpQuery = useCallback(
    async ({
      canQuery,
      effectiveLigandSmiles,
      variableItems,
      constantQuery,
      direction,
      queryProperty,
      mmpDatabaseId,
      queryMode,
      groupedByEnvironment,
      minPairs,
      envRadius,
      selectedFragmentIds,
      selectedFragmentAtomIndices,
      onTaskQueued,
      onTaskCompleted,
      onTaskFailed
    }: RunMmpQueryInput): Promise<RunMmpQueryResult | null> => {
      if (!canQuery) {
        onError('Ligand SMILES is missing or editing is disabled, cannot run query.');
        return null;
      }
      if (variableItems.length === 0) {
        onError('Please select a fragment or input variable query first.');
        return null;
      }
      const normalizedDatabaseId = readText(mmpDatabaseId).trim();
      if (!normalizedDatabaseId) {
        onError('No ready MMP database selected. Please choose a ready database before running.');
        return null;
      }
      const now = Date.now();
      if (mmpQueryInFlightRef.current) return null;
      if (now - lastMmpQueryAtRef.current < 350) return null;
      lastMmpQueryAtRef.current = now;
      mmpQueryInFlightRef.current = true;

      onError(null);
      setQueryNotice('Running MMP query...');
      setLoading(true);
      const startedAt = Date.now();
      let queuedTaskId = '';
      try {
        const selectedProperty = readText(queryProperty).trim();
        const selectedDirection = readText(direction).trim();
        const aggregationType = queryMode === 'many-to-many' ? 'group_by_fragment' : 'individual_transforms';
        const groupedByEnvironmentFlag =
          queryMode !== 'many-to-many'
            ? undefined
            : groupedByEnvironment === 'on'
              ? true
              : groupedByEnvironment === 'off'
                ? false
                : undefined;
        const propertyTargets: Record<string, unknown> = {};
        if (selectedProperty) {
          propertyTargets.property = selectedProperty;
          if (selectedDirection === 'increase' || selectedDirection === 'decrease') {
            propertyTargets.direction = selectedDirection;
          }
        }
        const requestPayload = {
          query_mol: effectiveLigandSmiles,
          variable_spec: {
            mode: variableItems[0]?.mode || 'substructure',
            items: variableItems
          },
          selected_fragment_ids: Array.from(
            new Set(
              (Array.isArray(selectedFragmentIds) ? selectedFragmentIds : [])
                .map((item) => String(item || '').trim())
                .filter(Boolean)
            )
          ),
          selected_fragment_atom_indices: Array.from(
            new Set(
              (Array.isArray(selectedFragmentAtomIndices) ? selectedFragmentAtomIndices : [])
                .map((value) => Number(value))
                .filter((value) => Number.isFinite(value) && value >= 0)
                .map((value) => Math.floor(value))
            )
          ),
          constant_spec: constantQuery.trim() ? { query: constantQuery.trim(), mode: 'substructure' } : {},
          property_targets: propertyTargets,
          mmp_database_id: normalizedDatabaseId,
          query_mode: queryMode,
          aggregation_type: aggregationType,
          ...(groupedByEnvironmentFlag === undefined ? {} : { grouped_by_environment: groupedByEnvironmentFlag }),
          min_pairs: minPairs,
          rule_env_radius: envRadius,
          max_results: queryMode === 'many-to-many' ? 600 : 400
        } as Record<string, unknown>;
        const response = await queryLeadOptimizationMmp(requestPayload, {
          onEnqueued: async (taskId) => {
            queuedTaskId = String(taskId || '').trim();
            if (!queuedTaskId) return;
            setLastMmpTaskId(queuedTaskId);
            if (typeof onTaskQueued === 'function') {
              try {
                await onTaskQueued({ taskId: queuedTaskId, requestPayload });
              } catch (queueErr) {
                onError(queueErr instanceof Error ? queueErr.message : 'Failed to persist MMP task row.');
              }
            }
          }
        });
        const nextTransforms = Array.isArray(response.transforms)
          ? (response.transforms as Array<Record<string, unknown>>)
          : [];
        const nextClusters = Array.isArray(response.clusters)
          ? (response.clusters as Array<Record<string, unknown>>)
          : [];
        const responseRecord = asRecord(response);
        const responseAggregationType = readText(responseRecord.aggregation_type).trim() || aggregationType;
        const responseGroupedByEnvironment = readBoolean(responseRecord.grouped_by_environment, false);
        const nextQueryId = readText(response.query_id);
        setQueryId(nextQueryId);
        setActiveQueryMode(queryMode);
        setQueryMinPairs(Math.max(1, minPairs));
        setLastMmpTaskId(readText(response.task_id));
        setMmpRunVersion((prev) => prev + 1);
        setTransforms(nextTransforms);
        setClusters(nextClusters);
        setGlobalCount(readNumber(response.global_count));
        setQueryStats((response.stats as Record<string, unknown>) || {});
        setActiveEvidence(null);
        setActiveTransformId('');
        clearSelections();
        setEnumeratedCandidates([]);
        setPredictionBySmiles({});
        setRuntimeStatusPollingEnabled(false);
        if (nextQueryId) {
          const cachePayload = {
            query_id: nextQueryId,
            task_id: queuedTaskId || readText(response.task_id),
            query_mode: readText(response.query_mode),
            aggregation_type: responseAggregationType,
            grouped_by_environment: responseGroupedByEnvironment,
            property_targets: propertyTargets,
            rule_env_radius: Math.max(0, envRadius),
            mmp_database_id: readText(response.mmp_database_id),
            mmp_database_label: readText(response.mmp_database_label),
            mmp_database_schema: readText(response.mmp_database_schema),
            variable_spec: responseRecord.variable_spec || {},
            constant_spec: responseRecord.constant_spec || {},
            transforms: nextTransforms,
            global_transforms: Array.isArray(response.global_transforms) ? response.global_transforms : nextTransforms,
            clusters: nextClusters,
            count: readNumber(response.count),
            global_count: readNumber(response.global_count),
            stats: (response.stats as Record<string, unknown>) || {},
            min_pairs: Math.max(1, minPairs),
            cluster_group_by: clusterGroupBy,
          } as Record<string, unknown>;
          queryResultCacheRef.current.set(nextQueryId, cachePayload);
        }
        const completedTaskId = queuedTaskId || readText(response.task_id);
        let candidateCount = 0;
        let persistedCandidates: Array<Record<string, unknown>> = [];
        if (nextTransforms.length === 0) {
          setQueryNotice(`MMP query complete (query_id=${response.query_id}) with 0 transforms.`);
          onError('MMP query returned no transforms. Try smaller fragment, min_pairs=1, larger env radius, or clear constant.');
        } else {
          setQueryNotice(
            readText(response.task_id)
              ? `MMP complete. task=${readText(response.task_id).slice(0, 12)} query=${readText(response.query_id).slice(0, 12)}`
              : `MMP complete. query=${readText(response.query_id).slice(0, 12)}`
          );
          try {
            const enumerate = await enumerateLeadOptimizationMmp({
              query_id: nextQueryId,
              task_id: completedTaskId,
              property_constraints: {},
              max_candidates: 360,
              compact: true
            });
            const rows = Array.isArray(enumerate.candidates)
              ? (enumerate.candidates as Array<Record<string, unknown>>)
              : [];
            setEnumeratedCandidates(rows);
            candidateCount = rows.length;
            persistedCandidates = rows;
          } catch (enumerateError) {
            onError(enumerateError instanceof Error ? enumerateError.message : 'Failed to build result rows.');
          }
        }
        const persistedQueryResult = {
          query_id: nextQueryId,
          query_mode: queryMode,
          aggregation_type: responseAggregationType,
          grouped_by_environment: responseGroupedByEnvironment,
          property_targets: propertyTargets,
          rule_env_radius: Math.max(0, envRadius),
          mmp_database_id: readText(response.mmp_database_id),
          mmp_database_label: readText(response.mmp_database_label),
          mmp_database_schema: readText(response.mmp_database_schema),
          transforms: nextTransforms,
          global_transforms: Array.isArray(response.global_transforms) ? response.global_transforms : nextTransforms,
          clusters: nextClusters,
          count: readNumber(response.count),
          global_count: readNumber(response.global_count),
          min_pairs: Math.max(1, minPairs),
          task_id: completedTaskId,
          stats: (response.stats as Record<string, unknown>) || {}
        } as Record<string, unknown>;
        if (completedTaskId && typeof onTaskCompleted === 'function') {
          await onTaskCompleted({
            taskId: completedTaskId,
            queryId: nextQueryId,
            transformCount: nextTransforms.length,
            candidateCount,
            elapsedSeconds: Math.max(0, (Date.now() - startedAt) / 1000),
            resultSnapshot: {
              query_result: persistedQueryResult,
              enumerated_candidates: persistedCandidates
            }
          });
        }
        return {
          queryId: nextQueryId,
          transformCount: nextTransforms.length,
          candidateCount
        };
      } catch (e) {
        setQueryNotice('');
        const message = e instanceof Error ? e.message : 'MMP query failed.';
        onError(message);
        if (queuedTaskId && typeof onTaskFailed === 'function') {
          await onTaskFailed({ taskId: queuedTaskId, error: message });
        }
        return null;
      } finally {
        mmpQueryInFlightRef.current = false;
        setLoading(false);
      }
      return null;
    },
    [clearSelections, clusterGroupBy, onError]
  );

  const loadQueryRun = useCallback(
    async (
      nextQueryId: string,
      options?: {
        taskId?: string;
      }
    ) => {
      const normalizedId = readText(nextQueryId).trim();
      if (!normalizedId) return;
      setLoading(true);
      onError(null);
      try {
        const queryResult = await fetchLeadOptimizationMmpQueryResult(normalizedId);
        const queryResultRecord = asRecord(queryResult);
        const nextTransforms = Array.isArray(queryResult.transforms)
          ? (queryResult.transforms as Array<Record<string, unknown>>)
          : [];
        const nextMinPairs = Math.max(1, readNumber(asRecord(queryResult).min_pairs || 1));
        setQueryMinPairs(nextMinPairs);
        const nextClusters = Array.isArray(queryResult.clusters)
          ? (queryResult.clusters as Array<Record<string, unknown>>)
          : [];
        setClusters(nextClusters);
        setQueryId(normalizedId);
        const nextMode = readText(queryResult.query_mode) === 'many-to-many' ? 'many-to-many' : 'one-to-many';
        setActiveQueryMode(nextMode);
        const responseAggregationType =
          readText(queryResultRecord.aggregation_type).trim() || (nextMode === 'many-to-many' ? 'group_by_fragment' : 'individual_transforms');
        const responseGroupedByEnvironment = readBoolean(queryResultRecord.grouped_by_environment, false);
        const responseTaskId = readText(queryResultRecord.task_id || options?.taskId);
        if (responseTaskId) {
          setLastMmpTaskId(responseTaskId);
        }
        const savedGroupBy = readText(queryResultRecord.cluster_group_by).toLowerCase();
        const nextGroupBy = savedGroupBy === 'from' || savedGroupBy === 'rule_env_radius' ? savedGroupBy : 'to';
        setClusterGroupBy(nextGroupBy as ClusterGroupBy);
        setTransforms(nextTransforms);
        setGlobalCount(readNumber(queryResult.global_count));
        setQueryStats((queryResult.stats as Record<string, unknown>) || {});
        setActiveTransformId('');
        setActiveEvidence(null);
        clearSelections();
        queryResultCacheRef.current.set(normalizedId, {
          query_id: normalizedId,
          task_id: responseTaskId,
          query_mode: nextMode,
          aggregation_type: responseAggregationType,
          grouped_by_environment: responseGroupedByEnvironment,
          mmp_database_id: readText(queryResult.mmp_database_id),
          mmp_database_label: readText(queryResult.mmp_database_label),
          mmp_database_schema: readText(queryResult.mmp_database_schema),
          transforms: nextTransforms,
          global_transforms: Array.isArray(queryResult.global_transforms) ? queryResult.global_transforms : nextTransforms,
          clusters: nextClusters,
          count: readNumber(queryResult.count),
          global_count: readNumber(queryResult.global_count),
          min_pairs: nextMinPairs,
          cluster_group_by: nextGroupBy,
          stats: (queryResult.stats as Record<string, unknown>) || {}
        });
        setQueryNotice(`Loaded MMP query summary (${nextTransforms.length} transforms).`);
      } catch (error) {
        if (isMmpQueryExpiredError(error)) {
          // Query cache may expire on backend; keep persisted task snapshot as source of truth.
          setQueryNotice('Loaded saved MMP snapshot. Query cache expired on backend.');
          onError(null);
        } else {
          onError(error instanceof Error ? error.message : 'Failed to load saved MMP query.');
        }
      } finally {
        setLoading(false);
      }
    },
    [clearSelections, onError]
  );

  const loadEvidence = useCallback(
    async (transformId: string) => {
      if (!transformId) {
        setActiveEvidence(null);
        return;
      }
      setEvidenceLoading(true);
      try {
        const evidence = await fetchLeadOptimizationMmpEvidence(transformId);
        setActiveEvidence(evidence);
      } catch (e) {
        setActiveEvidence(null);
        onError(e instanceof Error ? e.message : 'Failed to load transform evidence.');
      } finally {
        setEvidenceLoading(false);
      }
    },
    [onError]
  );

  const handleTransformClick = useCallback(
    async (row: Record<string, unknown>) => {
      const transformId = readText(row.transform_id);
      if (!transformId) return;
      setActiveTransformId(transformId);
      await loadEvidence(transformId);
    },
    [loadEvidence]
  );

  const runEnumerate = useCallback(async () => {
    if (!queryId) {
      onError('Please run MMP query first.');
      return;
    }
    onError(null);
    setLoading(true);
    try {
      const result = await enumerateLeadOptimizationMmp({
        query_id: queryId,
        task_id: lastMmpTaskId,
        transform_ids: selectedTransformIds,
        cluster_ids: selectedClusterIds,
        property_constraints: {},
        max_candidates: 200,
        compact: true
      });
      const rows = Array.isArray(result.candidates) ? (result.candidates as Array<Record<string, unknown>>) : [];
      setEnumeratedCandidates(rows);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'MMP enumeration failed.');
    } finally {
      setLoading(false);
    }
  }, [lastMmpTaskId, onError, queryId, selectedClusterIds, selectedTransformIds]);

  const setClusterGrouping = useCallback(
    async (groupBy: ClusterGroupBy) => {
      const normalized: ClusterGroupBy =
        groupBy === 'from' || groupBy === 'rule_env_radius' ? groupBy : 'to';
      setClusterGroupBy(normalized);
      if (!queryId) return;
      await runCluster(queryId, queryMinPairs, normalized);
    },
    [queryId, queryMinPairs, runCluster]
  );

  const submitPredictCandidateTask = useCallback(
    async ({
      candidateSmiles,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues,
      variableAtomIndices,
      referenceTargetFilename,
      referenceTargetFileContent,
      referenceLigandFilename,
      referenceLigandFileContent
    }: {
      candidateSmiles: string;
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
      variableAtomIndices?: number[];
      referenceTargetFilename?: string;
      referenceTargetFileContent?: string;
      referenceLigandFilename?: string;
      referenceLigandFileContent?: string;
    }) => {
      const nextSmiles = String(candidateSmiles || '').trim();
      if (!nextSmiles) {
        throw new Error('Candidate SMILES is required.');
      }
      if (!referenceReady) {
        throw new Error('Please upload reference target+ligand first.');
      }
      const selectedBackend = normalizePredictionBackendStrict(backendOverride || backend);
      if (!selectedBackend) {
        throw new Error(`Unsupported backend '${String(backendOverride || backend || '').trim()}' for lead optimization prediction.`);
      }
      const effectiveProteinSequence = String(referenceProteinSequence || '').trim() || String(proteinSequence || '').trim();
      const normalizedReferenceTargetFilename = String(referenceTargetFilename || '').trim();
      const normalizedReferenceTargetFileContent = String(referenceTargetFileContent || '').trim();
      const normalizedReferenceLigandFilename = String(referenceLigandFilename || '').trim();
      const normalizedReferenceLigandFileContent = String(referenceLigandFileContent || '').trim();
      const explicitTemplateStructureText = String(referenceTemplateStructureText || '').trim();
      const explicitTemplateFormat = referenceTemplateFormat === 'pdb' ? 'pdb' : 'cif';
      if (!effectiveProteinSequence && !explicitTemplateStructureText) {
        throw new Error('Protein sequence/template is unavailable. Upload reference target first or provide sequence.');
      }
      let normalizedTargetChain = String(targetChain || '').trim();
      let normalizedLigandChain = String(ligandChain || '').trim();
      if (!normalizedTargetChain || !normalizedLigandChain) {
        throw new Error('Target chain and ligand chain are required for lead optimization prediction.');
      }
      if (normalizedTargetChain.toUpperCase() === normalizedLigandChain.toUpperCase()) {
        throw new Error(
          `Target chain and ligand chain cannot be the same ('${normalizedTargetChain}'). Re-upload reference to resolve chain mapping.`
        );
      }
      if (selectedBackend === 'protenix' && !effectiveProteinSequence) {
        throw new Error('Protenix backend requires protein sequence. Please provide sequence or use Boltz/AlphaFold3 template mode.');
      }
      if (selectedBackend === 'pocketxmol') {
        if (!normalizedReferenceTargetFileContent) {
          throw new Error('PocketXMol backend requires uploaded reference target file content.');
        }
        if (!normalizedReferenceLigandFileContent) {
          throw new Error('PocketXMol backend requires uploaded reference ligand file content.');
        }
        const normalizedVariableIndices = Array.from(
          new Set(
            (Array.isArray(variableAtomIndices) ? variableAtomIndices : [])
              .map((value) => Number(value))
              .filter((value) => Number.isFinite(value) && value >= 0)
              .map((value) => Math.floor(value))
          )
        );
        if (normalizedVariableIndices.length === 0) {
          throw new Error('PocketXMol backend requires variable atom indices from lead-opt selection.');
        }
      }
      const shouldSendPocketReferenceFiles = selectedBackend === 'pocketxmol';
      const taskId = await predictLeadOptimizationCandidate({
        candidateSmiles: nextSmiles,
        proteinSequence: effectiveProteinSequence,
        backend: selectedBackend,
        targetChain: normalizedTargetChain,
        ligandChain: normalizedLigandChain,
        referenceTemplateStructureText: explicitTemplateStructureText,
        referenceTemplateFormat: explicitTemplateFormat,
        pocketResidues,
        variableAtomIndices,
        referenceTargetFilename: shouldSendPocketReferenceFiles ? normalizedReferenceTargetFilename : undefined,
        referenceTargetFileContent: shouldSendPocketReferenceFiles ? normalizedReferenceTargetFileContent : undefined,
        referenceLigandFilename: shouldSendPocketReferenceFiles ? normalizedReferenceLigandFilename : undefined,
        referenceLigandFileContent: shouldSendPocketReferenceFiles ? normalizedReferenceLigandFileContent : undefined,
        useMsaServer: true,
        seed: null
      });
      return taskId;
    },
    [backend, ligandChain, proteinSequence, targetChain]
  );

  const runPredictCandidate = useCallback(
    async ({
      candidateSmiles,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues,
      variableAtomIndices,
      referenceTargetFilename,
      referenceTargetFileContent,
      referenceLigandFilename,
      referenceLigandFileContent
    }: {
      candidateSmiles: string;
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
      variableAtomIndices?: number[];
      referenceTargetFilename?: string;
      referenceTargetFileContent?: string;
      referenceLigandFilename?: string;
      referenceLigandFileContent?: string;
    }) => {
      onError(null);
      setLoading(true);
      try {
        const normalizedSmiles = String(candidateSmiles || '').trim();
        const selectedBackend = normalizePredictionBackendStrict(backendOverride || backend);
        if (!selectedBackend) {
          throw new Error('Invalid backend for candidate prediction.');
        }
        const predictionKey = buildLeadOptPredictionRecordKey(selectedBackend, normalizedSmiles);
        if (!predictionKey) {
          throw new Error('Invalid prediction key.');
        }
        const retryTimer = predictionHydrationRetryTimerRef.current[predictionKey];
        if (retryTimer) {
          window.clearTimeout(retryTimer);
          delete predictionHydrationRetryTimerRef.current[predictionKey];
        }
        delete predictionHydrationRetryCountRef.current[predictionKey];
        const previousRecord = predictionBySmiles[predictionKey];
        const localTaskId = `local:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
        setPredictionBySmiles((prev) => ({
          ...prev,
          [predictionKey]: {
            ...(prev[predictionKey] || previousRecord || {}),
            ...buildQueuedPredictionRecord(localTaskId, selectedBackend)
          }
        }));
        if (typeof onPredictionQueued === 'function') {
          void Promise.resolve(onPredictionQueued({ taskId: localTaskId, backend: selectedBackend, candidateSmiles: normalizedSmiles })).catch(() => {
            // Keep local queue-state persistence best-effort only.
          });
        }
        const taskId = await submitPredictCandidateTask({
          candidateSmiles: normalizedSmiles,
          backend: selectedBackend,
          referenceReady,
          referenceProteinSequence,
          referenceTemplateStructureText,
          referenceTemplateFormat,
          pocketResidues,
          variableAtomIndices,
          referenceTargetFilename,
          referenceTargetFileContent,
          referenceLigandFilename,
          referenceLigandFileContent
        });
        setLastPredictionTaskId(taskId);
        setPredictionBySmiles((prev) => ({
          ...prev,
          [predictionKey]: {
            ...buildQueuedPredictionRecord(taskId, selectedBackend)
          }
        }));
        setRuntimeStatusPollingEnabled(true);
        if (typeof onPredictionQueued === 'function') {
          void Promise.resolve(onPredictionQueued({ taskId, backend: selectedBackend, candidateSmiles: normalizedSmiles })).catch(() => {
            // Keep runtime state progression independent from persistence latency/failures.
          });
        }
      } catch (e) {
        const normalizedSmiles = String(candidateSmiles || '').trim();
        const selectedBackend = normalizePredictionBackendStrict(backendOverride || backend);
        if (!selectedBackend) {
          const message = e instanceof Error ? e.message : 'Candidate prediction failed.';
          onError(message);
          return;
        }
        const predictionKey = buildLeadOptPredictionRecordKey(selectedBackend, normalizedSmiles);
        if (!predictionKey) {
          const message = e instanceof Error ? e.message : 'Candidate prediction failed.';
          onError(message);
          return;
        }
        const message = e instanceof Error ? e.message : 'Candidate prediction failed.';
        setPredictionBySmiles((prev) => {
          const current = prev[predictionKey];
          if (!current) {
            return {
              ...prev,
              [predictionKey]: {
                ...buildQueuedPredictionRecord(`local:failed:${Date.now()}`, selectedBackend),
                state: 'FAILURE',
                error: message,
                updatedAt: Date.now()
              }
            };
          }
          return {
            ...prev,
            [predictionKey]: {
              ...current,
              backend: selectedBackend,
              state: 'FAILURE',
              error: message,
              updatedAt: Date.now()
            }
          };
        });
        onError(message);
      } finally {
        setLoading(false);
      }
    },
    [backend, onError, onPredictionQueued, predictionBySmiles, submitPredictCandidateTask]
  );

  const runPredictReferenceForBackend = useCallback(
    async ({
      candidateSmiles,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues,
      variableAtomIndices,
      referenceTargetFilename,
      referenceTargetFileContent,
      referenceLigandFilename,
      referenceLigandFileContent
    }: {
      candidateSmiles: string;
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
      variableAtomIndices?: number[];
      referenceTargetFilename?: string;
      referenceTargetFileContent?: string;
      referenceLigandFilename?: string;
      referenceLigandFileContent?: string;
    }) => {
      const selectedBackend = normalizePredictionBackendStrict(backendOverride || backend);
      if (!selectedBackend) {
        onError('Invalid backend for reference prediction.');
        return;
      }
      const referenceSmiles = String(candidateSmiles || '').trim();
      if (!referenceSmiles) return;
      const retryTimer = referenceHydrationRetryTimerRef.current[selectedBackend];
      if (retryTimer) {
        window.clearTimeout(retryTimer);
        delete referenceHydrationRetryTimerRef.current[selectedBackend];
      }
      delete referenceHydrationRetryCountRef.current[selectedBackend];
      const existing = referencePredictionByBackend[selectedBackend];
      const existingState = String(existing?.state || '').toUpperCase();
      if (existingState === 'QUEUED' || existingState === 'RUNNING') return;
      if (existingState === 'SUCCESS') {
        const hasMetrics = hasHydratedPredictionResult(existing);
        if (existing && hasMetrics) return;
      }

      const localTaskId = `local:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
      setReferencePredictionByBackend((prev) => ({
        ...prev,
        [selectedBackend]: {
          ...(prev[selectedBackend] || existing || {}),
          ...buildQueuedPredictionRecord(localTaskId, selectedBackend)
        }
      }));
      try {
        const taskId = await submitPredictCandidateTask({
          candidateSmiles: referenceSmiles,
          backend: selectedBackend,
          referenceReady,
          referenceProteinSequence,
          referenceTemplateStructureText,
          referenceTemplateFormat,
          pocketResidues,
          variableAtomIndices,
          referenceTargetFilename,
          referenceTargetFileContent,
          referenceLigandFilename,
          referenceLigandFileContent
        });
        setLastPredictionTaskId(taskId);
        setReferencePredictionByBackend((prev) => ({
          ...prev,
          [selectedBackend]: {
            ...buildQueuedPredictionRecord(taskId, selectedBackend)
          }
        }));
        setRuntimeStatusPollingEnabled(true);
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Reference prediction failed.';
        setReferencePredictionByBackend((prev) => {
          const current = prev[selectedBackend];
          if (!current) {
            return {
              ...prev,
              [selectedBackend]: {
                ...buildQueuedPredictionRecord(`local:failed:${Date.now()}`, selectedBackend),
                state: 'FAILURE',
                error: message,
                updatedAt: Date.now()
              }
            };
          }
          return {
            ...prev,
            [selectedBackend]: {
              ...current,
              state: 'FAILURE',
              error: message,
              updatedAt: Date.now()
            }
          };
        });
        onError(message);
      }
    },
    [backend, onError, referencePredictionByBackend, submitPredictCandidateTask]
  );

  const runPredictBatch = useCallback(
    async ({
      candidateSmilesList,
      backend: backendOverride,
      referenceReady,
      referenceProteinSequence,
      referenceTemplateStructureText,
      referenceTemplateFormat,
      pocketResidues
    }: {
      candidateSmilesList: string[];
      backend?: string;
      referenceReady: boolean;
      referenceProteinSequence?: string;
      referenceTemplateStructureText?: string;
      referenceTemplateFormat?: 'cif' | 'pdb';
      pocketResidues: Array<Record<string, unknown>>;
    }) => {
      const batch = Array.from(
        new Set((candidateSmilesList || []).map((item) => String(item || '').trim()).filter(Boolean))
      ).slice(0, 24);
      if (batch.length === 0) {
        onError('Please select at least one candidate first.');
        return;
      }
      onError(null);
      setLoading(true);
      let success = 0;
      let failure = 0;
      let lastTaskId = '';
      const selectedBackend = normalizePredictionBackendStrict(backendOverride || backend);
      if (!selectedBackend) {
        onError('Invalid backend for batch prediction.');
        setLoading(false);
        return;
      }
      try {
        for (const smiles of batch) {
          const predictionKey = buildLeadOptPredictionRecordKey(selectedBackend, smiles);
          if (!predictionKey) {
            failure += 1;
            continue;
          }
          const previousRecord = predictionBySmiles[predictionKey];
          const localTaskId = `local:${Date.now()}:${Math.random().toString(36).slice(2, 8)}`;
          setPredictionBySmiles((prev) => ({
            ...prev,
            [predictionKey]: {
              ...(prev[predictionKey] || previousRecord || {}),
              ...buildQueuedPredictionRecord(localTaskId, selectedBackend)
            }
          }));
          try {
            const taskId = await submitPredictCandidateTask({
              candidateSmiles: smiles,
              backend: selectedBackend,
              referenceReady,
              referenceProteinSequence,
              referenceTemplateStructureText,
              referenceTemplateFormat,
              pocketResidues
            });
            success += 1;
            lastTaskId = taskId;
            setPredictionBySmiles((prev) => ({
              ...prev,
              [predictionKey]: {
                ...buildQueuedPredictionRecord(taskId, selectedBackend)
              }
            }));
            setRuntimeStatusPollingEnabled(true);
            if (typeof onPredictionQueued === 'function') {
              void Promise.resolve(onPredictionQueued({ taskId, backend: selectedBackend, candidateSmiles: smiles })).catch(() => {
                // Keep runtime state progression independent from persistence latency/failures.
              });
            }
          } catch (_error) {
            failure += 1;
            setPredictionBySmiles((prev) => {
              const current = prev[predictionKey];
              if (!current || !String(current.taskId || '').startsWith('local:')) return prev;
              if (previousRecord) {
                return {
                  ...prev,
                  [predictionKey]: previousRecord
                };
              }
              const next = { ...prev };
              delete next[predictionKey];
              return next;
            });
          }
        }
      } finally {
        setLoading(false);
      }
      if (lastTaskId) setLastPredictionTaskId(lastTaskId);
      if (failure > 0) {
        onError(`${failure}/${batch.length} prediction submissions failed. ${success} submitted.`);
      } else {
        onError(null);
      }
    },
    [backend, onError, onPredictionQueued, predictionBySmiles, submitPredictCandidateTask]
  );

  const ensurePredictionResult = useCallback(
    async (candidateSmiles: string, backendInput?: string): Promise<LeadOptPredictionRecord | null> => {
      const normalizedSmiles = String(candidateSmiles || '').trim();
      if (!normalizedSmiles) return null;
      const predictionKey = buildLeadOptPredictionRecordKey(backendInput || backend, normalizedSmiles);
      if (!predictionKey) return null;
      const existing = predictionBySmiles[predictionKey];
      if (!existing) return null;
      if (String(existing.state || '').toUpperCase() !== 'SUCCESS') return existing;
      if (readText(existing.structureText).trim() && hasHydratedPredictionResult(existing)) {
        return existing;
      }

      const taskId = readText(existing.taskId).trim();
      if (!taskId) return existing;
      try {
        const status = await getTaskStatus(taskId);
        const runtimeState = inferPredictionRuntimeStateFromStatusPayload(status);
        if (runtimeState === 'QUEUED' || runtimeState === 'RUNNING') {
          const nextRuntimeState = resolveNonRegressiveRuntimeState(existing.state, runtimeState);
          if (!nextRuntimeState) return existing;
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: nextRuntimeState,
            error: '',
            updatedAt: Date.now()
          };
          setPredictionBySmiles((prev) => ({
            ...prev,
            [predictionKey]: nextRecord
          }));
          return nextRecord;
        }
        if (runtimeState === 'FAILURE') {
          const errorText = buildTaskRuntimeFailureMessage(
            status as { state: string; info?: Record<string, unknown> },
            'Prediction failed.'
          );
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: 'FAILURE',
            error: errorText || 'Prediction failed.',
            updatedAt: Date.now()
          };
          setPredictionBySmiles((prev) => ({
            ...prev,
            [predictionKey]: nextRecord
          }));
          return nextRecord;
        }
      } catch (err) {
        console.error('Candidate ensurePredictionResult status probe failed; falling through to result download.', err);
        // Ignore transient status failures and fall through to result download.
      }
      try {
        const blob = await downloadResultBlob(taskId, { mode: 'view' });
        const parsed = await parseResultBundle(blob);
        const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain, normalizedSmiles);
        const renderContract = pickPredictionRenderContract(resultPayload, existing);
        const nextRecord: LeadOptPredictionRecord = {
          ...existing,
          state: 'SUCCESS',
          pairIptm: resultPayload.pairIptm,
          interfaceMetricValue: resultPayload.interfaceMetricValue,
          interfaceMetricLabel: resultPayload.interfaceMetricLabel,
          interfaceMetricSource: resultPayload.interfaceMetricSource,
          pairPae: resultPayload.pairPae,
          pairIptmResolved: true,
          ligandPlddt: resultPayload.ligandPlddt,
          ligandAtomPlddts: resultPayload.ligandAtomPlddts,
          ...(renderContract.ligandRenderSmiles ? { ligandRenderSmiles: renderContract.ligandRenderSmiles } : {}),
          ...(renderContract.ligandRenderAtomPlddts.length > 0 ? { ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts } : {}),
          ...(resultPayload.structureText.trim()
            ? {
                structureText: resultPayload.structureText,
                structureFormat: resultPayload.structureFormat,
                structureName: resultPayload.structureName
              }
            : {}),
          resultBundleHydrated: true,
          error: '',
          updatedAt: Date.now()
        };
        setPredictionBySmiles((prev) => ({
          ...prev,
          [predictionKey]: nextRecord
        }));
        const retryTimer = predictionHydrationRetryTimerRef.current[predictionKey];
        if (retryTimer) {
          window.clearTimeout(retryTimer);
          delete predictionHydrationRetryTimerRef.current[predictionKey];
        }
        delete predictionHydrationRetryCountRef.current[predictionKey];
        return nextRecord;
      } catch (error) {
        if (isResultArchiveMissingError(error)) {
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: 'FAILURE',
            error: buildMissingResultArchiveMessage(taskId),
            updatedAt: Date.now()
          };
          setPredictionBySmiles((prev) => ({
            ...prev,
            [predictionKey]: nextRecord
          }));
          return nextRecord;
        }
        if (isResultArchivePendingError(error)) {
          const pendingState = resolveNonRegressiveRuntimeState(existing.state, inferPendingRuntimeStateFromError(error)) || 'RUNNING';
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: pendingState,
            error: '',
            updatedAt: Date.now()
          };
          setPredictionBySmiles((prev) => ({
            ...prev,
            [predictionKey]: nextRecord
          }));
          const attempt = Number(predictionHydrationRetryCountRef.current[predictionKey] || 0) + 1;
          if (attempt > RESULT_HYDRATION_MAX_RETRIES) {
            delete predictionHydrationRetryCountRef.current[predictionKey];
            return nextRecord;
          }
          predictionHydrationRetryCountRef.current[predictionKey] = attempt;
          if (!predictionHydrationRetryTimerRef.current[predictionKey]) {
            const delayMs = computeHydrationRetryDelayMs(attempt);
            predictionHydrationRetryTimerRef.current[predictionKey] = window.setTimeout(() => {
              delete predictionHydrationRetryTimerRef.current[predictionKey];
              setPredictionBySmiles((prev) => {
                const current = prev[predictionKey];
                if (!current) return prev;
                return {
                  ...prev,
                  [predictionKey]: {
                    ...current,
                    state: pendingState,
                    error: '',
                    updatedAt: Date.now()
                  }
                };
              });
            }, delayMs);
          }
          return nextRecord;
        }
        onError(error instanceof Error ? error.message : 'Failed to load prediction result.');
        return existing;
      }
    },
    [backend, ligandChain, onError, predictionBySmiles, targetChain]
  );

  const ensureReferencePredictionResult = useCallback(
    async (backendKeyInput: string): Promise<LeadOptPredictionRecord | null> => {
      const backendKey = normalizePredictionBackendStrict(backendKeyInput);
      if (!backendKey) return null;
      const existing = referencePredictionByBackend[backendKey];
      if (!existing) return null;
      if (String(existing.state || '').toUpperCase() !== 'SUCCESS') return existing;
      if (readText(existing.structureText).trim() && hasHydratedPredictionResult(existing)) {
        return existing;
      }

      const taskId = readText(existing.taskId).trim();
      if (!taskId) return existing;
      try {
        const status = await getTaskStatus(taskId);
        const runtimeState = inferPredictionRuntimeStateFromStatusPayload(status);
        if (runtimeState === 'QUEUED' || runtimeState === 'RUNNING') {
          const nextRuntimeState = resolveNonRegressiveRuntimeState(existing.state, runtimeState);
          if (!nextRuntimeState) return existing;
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: nextRuntimeState,
            error: '',
            updatedAt: Date.now()
          };
          setReferencePredictionByBackend((prev) => ({
            ...prev,
            [backendKey]: nextRecord
          }));
          return nextRecord;
        }
        if (runtimeState === 'FAILURE') {
          const errorText = buildTaskRuntimeFailureMessage(
            status as { state: string; info?: Record<string, unknown> },
            'Prediction failed.'
          );
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: 'FAILURE',
            error: errorText || 'Prediction failed.',
            updatedAt: Date.now()
          };
          setReferencePredictionByBackend((prev) => ({
            ...prev,
            [backendKey]: nextRecord
          }));
          return nextRecord;
        }
      } catch (err) {
        console.error('Reference ensurePredictionResult status probe failed; falling through to result download.', err);
        // Ignore transient status failures and fall through to result download.
      }
      try {
        const blob = await downloadResultBlob(taskId, { mode: 'view' });
        const parsed = await parseResultBundle(blob);
        const resultPayload = extractPredictionResultPayload(parsed, targetChain, ligandChain);
        const renderContract = pickPredictionRenderContract(resultPayload, existing);
        const nextRecord: LeadOptPredictionRecord = {
          ...existing,
          state: 'SUCCESS',
          pairIptm: resultPayload.pairIptm,
          interfaceMetricValue: resultPayload.interfaceMetricValue,
          interfaceMetricLabel: resultPayload.interfaceMetricLabel,
          interfaceMetricSource: resultPayload.interfaceMetricSource,
          pairPae: resultPayload.pairPae,
          pairIptmResolved: true,
          ligandPlddt: resultPayload.ligandPlddt,
          ligandAtomPlddts: resultPayload.ligandAtomPlddts,
          ...(renderContract.ligandRenderSmiles ? { ligandRenderSmiles: renderContract.ligandRenderSmiles } : {}),
          ...(renderContract.ligandRenderAtomPlddts.length > 0 ? { ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts } : {}),
          ...(resultPayload.structureText.trim()
            ? {
                structureText: resultPayload.structureText,
                structureFormat: resultPayload.structureFormat,
                structureName: resultPayload.structureName
              }
            : {}),
          resultBundleHydrated: true,
          error: '',
          updatedAt: Date.now()
        };
        setReferencePredictionByBackend((prev) => ({
          ...prev,
          [backendKey]: nextRecord
        }));
        const retryTimer = referenceHydrationRetryTimerRef.current[backendKey];
        if (retryTimer) {
          window.clearTimeout(retryTimer);
          delete referenceHydrationRetryTimerRef.current[backendKey];
        }
        delete referenceHydrationRetryCountRef.current[backendKey];
        return nextRecord;
      } catch (error) {
        if (isResultArchiveMissingError(error)) {
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: 'FAILURE',
            error: buildMissingResultArchiveMessage(taskId),
            updatedAt: Date.now()
          };
          setReferencePredictionByBackend((prev) => ({
            ...prev,
            [backendKey]: nextRecord
          }));
          return nextRecord;
        }
        if (isResultArchivePendingError(error)) {
          const pendingState = resolveNonRegressiveRuntimeState(existing.state, inferPendingRuntimeStateFromError(error)) || 'RUNNING';
          const nextRecord: LeadOptPredictionRecord = {
            ...existing,
            state: pendingState,
            error: '',
            updatedAt: Date.now()
          };
          setReferencePredictionByBackend((prev) => ({
            ...prev,
            [backendKey]: nextRecord
          }));
          const attempt = Number(referenceHydrationRetryCountRef.current[backendKey] || 0) + 1;
          if (attempt > RESULT_HYDRATION_MAX_RETRIES) {
            delete referenceHydrationRetryCountRef.current[backendKey];
            return nextRecord;
          }
          referenceHydrationRetryCountRef.current[backendKey] = attempt;
          if (!referenceHydrationRetryTimerRef.current[backendKey]) {
            const delayMs = computeHydrationRetryDelayMs(attempt);
            referenceHydrationRetryTimerRef.current[backendKey] = window.setTimeout(() => {
              delete referenceHydrationRetryTimerRef.current[backendKey];
              setReferencePredictionByBackend((prev) => {
                const current = prev[backendKey];
                if (!current) return prev;
                return {
                  ...prev,
                  [backendKey]: {
                    ...current,
                    state: pendingState,
                    error: '',
                    updatedAt: Date.now()
                  }
                };
              });
            }, delayMs);
          }
          return nextRecord;
        }
        onError(error instanceof Error ? error.message : 'Failed to load prediction result.');
        return existing;
      }
    },
    [ligandChain, onError, referencePredictionByBackend, targetChain]
  );

  // Keep result-bundle hydration on-demand to avoid heavy background downloads
  // when users are only browsing candidate/task lists.

  const toggleTransformSelection = useCallback((transformId: string) => {
    if (!transformId) return;
    setSelectedTransformIds((prev) => {
      if (prev.includes(transformId)) return prev.filter((item) => item !== transformId);
      return [...prev, transformId];
    });
  }, []);

  const toggleClusterSelection = useCallback((clusterId: string) => {
    if (!clusterId) return;
    setSelectedClusterIds((prev) => {
      if (prev.includes(clusterId)) return prev.filter((item) => item !== clusterId);
      return [...prev, clusterId];
    });
  }, []);

  const selectTopTransforms = useCallback(
    (limit = 12) => {
      const next = [...transforms]
        .sort((a, b) => {
          const [be, bp, bd] = sortScore(b.evidence_strength, b.n_pairs, b.median_delta);
          const [ae, ap, ad] = sortScore(a.evidence_strength, a.n_pairs, a.median_delta);
          if (be !== ae) return be - ae;
          if (bp !== ap) return bp - ap;
          return Math.abs(bd) - Math.abs(ad);
        })
        .slice(0, Math.max(1, limit))
        .map((item) => readText(item.transform_id))
        .filter(Boolean);
      setSelectedTransformIds(next);
    },
    [transforms]
  );

  const selectTopClusters = useCallback(
    (limit = 6) => {
      const next = [...clusters]
        .sort((a, b) => {
          const [bs, bi, bd] = sortScore(b.cluster_size, b['%median_improved'], b.median_delta);
          const [as, ai, ad] = sortScore(a.cluster_size, a['%median_improved'], a.median_delta);
          if (bs !== as) return bs - as;
          if (bi !== ai) return bi - ai;
          return Math.abs(bd) - Math.abs(ad);
        })
        .slice(0, Math.max(1, limit))
        .map((item) => readText(item.cluster_id) || readText(item.group_key))
        .filter(Boolean);
      setSelectedClusterIds(next);
    },
    [clusters]
  );

  const evidencePairs = useMemo(() => {
    if (!activeEvidence) return [] as Array<Record<string, unknown>>;
    return Array.isArray(activeEvidence.pairs) ? activeEvidence.pairs : [];
  }, [activeEvidence]);

  const activeTransformSummary = useMemo(() => {
    const transform = (activeEvidence?.transform as Record<string, unknown>) || {};
    return {
      nPairs: readText(transform.n_pairs) || readText(activeEvidence?.n_pairs) || String(evidencePairs.length),
      medianDelta: formatMetric(transform.median_delta),
      iqr: formatMetric(transform.iqr),
      std: formatMetric(transform.std),
      percentImproved: formatMetric(transform.percent_improved || transform['%improved']),
      directionality: formatMetric(transform.directionality),
      evidenceStrength: formatMetric(transform.evidence_strength)
    };
  }, [activeEvidence, evidencePairs.length]);

  const hydrateFromSnapshot = useCallback((snapshot: LeadOptMmpPersistedSnapshot | null | undefined) => {
    const payload = asRecord(snapshot);
    const queryResult = asRecord(payload.query_result);
    const nextQueryId = readText(queryResult.query_id);
    if (!nextQueryId) return;
    const nextTaskId = readText(queryResult.task_id || payload.task_id);
    const nextTransforms = asRecordArray(queryResult.transforms);
    const nextClusters = asRecordArray(queryResult.clusters);
    const nextCandidates = asRecordArray(payload.enumerated_candidates);
    const nextPredictions = normalizePredictionMap(payload.prediction_by_smiles);
    const nextReferenceByBackend = normalizeReferencePredictionMap(payload.reference_prediction_by_backend);

    setQueryId(nextQueryId);
    setLastMmpTaskId(nextTaskId);
    const nextMode = readText(queryResult.query_mode).toLowerCase() === 'many-to-many' ? 'many-to-many' : 'one-to-many';
    const nextAggregationType =
      readText(queryResult.aggregation_type).trim() || (nextMode === 'many-to-many' ? 'group_by_fragment' : 'individual_transforms');
    const nextGroupedByEnvironment = readBoolean(queryResult.grouped_by_environment, false);
    setActiveQueryMode(nextMode);
    const nextMinPairs = Math.max(1, readNumber(queryResult.min_pairs || 1));
    setQueryMinPairs(nextMinPairs);
    const savedGroupBy = readText(queryResult.cluster_group_by).toLowerCase();
    setClusterGroupBy(
      savedGroupBy === 'from' || savedGroupBy === 'rule_env_radius' ? (savedGroupBy as ClusterGroupBy) : 'to'
    );
    const shouldMergeRuntimeState = queryIdRef.current === nextQueryId;
    const nextStats = asRecord(queryResult.stats);
    const cacheCurrent = asRecord(queryResultCacheRef.current.get(nextQueryId));
    const cachedTransforms = asRecordArray(cacheCurrent.transforms);
    const cachedClusters = asRecordArray(cacheCurrent.clusters);
    const cachedStats = asRecord(cacheCurrent.stats);
    const keepPreviousTransforms = shouldMergeRuntimeState && nextTransforms.length === 0;
    const keepPreviousClusters = shouldMergeRuntimeState && nextClusters.length === 0;
    const keepPreviousCandidates = shouldMergeRuntimeState && nextCandidates.length === 0;
    const nextGlobalCount = readNumber(queryResult.global_count);

    setTransforms((prev) => (keepPreviousTransforms && prev.length > 0 ? prev : nextTransforms));
    setClusters((prev) => (keepPreviousClusters && prev.length > 0 ? prev : nextClusters));
    setEnumeratedCandidates((prev) => (keepPreviousCandidates && prev.length > 0 ? prev : nextCandidates));
    setGlobalCount((prev) => (shouldMergeRuntimeState && nextGlobalCount <= 0 && prev > 0 ? prev : nextGlobalCount));
    setQueryStats((prev) => {
      if (Object.keys(nextStats).length > 0) return nextStats;
      if (shouldMergeRuntimeState && Object.keys(prev).length > 0) return prev;
      return cachedStats;
    });
    setActiveTransformId('');
    setActiveEvidence(null);
    setSelectedTransformIds([]);
    setSelectedClusterIds([]);
    setPredictionBySmiles((prev) =>
      shouldMergeRuntimeState ? mergePredictionRecordMapsNonRegressive(prev, nextPredictions) : nextPredictions
    );
    setReferencePredictionByBackend((prev) =>
      shouldMergeRuntimeState ? mergePredictionRecordMapsNonRegressive(prev, nextReferenceByBackend) : nextReferenceByBackend
    );
    // Keep runtime polling armed after snapshot hydration. The pollers themselves
    // only call status API for QUEUED/RUNNING records with real task IDs.
    setRuntimeStatusPollingEnabled(true);
    if (!(keepPreviousCandidates && nextCandidates.length === 0)) {
      setQueryNotice(`Loaded saved MMP rows (${nextCandidates.length}).`);
    }
    const cacheTransforms =
      nextTransforms.length > 0 ? nextTransforms : shouldMergeRuntimeState ? cachedTransforms : [];
    const cacheClusters =
      nextClusters.length > 0 ? nextClusters : shouldMergeRuntimeState ? cachedClusters : [];
    const cacheStatsPayload =
      Object.keys(nextStats).length > 0 ? nextStats : shouldMergeRuntimeState ? cachedStats : {};
    const cacheGlobalCount =
      nextGlobalCount > 0 ? nextGlobalCount : shouldMergeRuntimeState ? readNumber(cacheCurrent.global_count) : nextGlobalCount;
    queryResultCacheRef.current.set(nextQueryId, {
      query_id: nextQueryId,
      task_id: nextTaskId,
      query_mode: nextMode,
      aggregation_type: nextAggregationType,
      grouped_by_environment: nextGroupedByEnvironment,
      mmp_database_id: readText(queryResult.mmp_database_id),
      mmp_database_label: readText(queryResult.mmp_database_label),
      mmp_database_schema: readText(queryResult.mmp_database_schema),
      transforms: cacheTransforms,
      global_transforms: asRecordArray(queryResult.global_transforms),
      clusters: cacheClusters,
      count: readNumber(queryResult.count),
      global_count: cacheGlobalCount,
      min_pairs: nextMinPairs,
      cluster_group_by:
        savedGroupBy === 'from' || savedGroupBy === 'rule_env_radius' ? savedGroupBy : 'to',
      stats: cacheStatsPayload
    });
  }, []);

  return {
    loading,
    evidenceLoading,
    queryNotice,
    queryId,
    activeQueryMode,
    clusterGroupBy,
    queryMinPairs,
    globalCount,
    queryStats,
    transforms,
    clusters,
    activeTransformId,
    activeEvidence,
    selectedTransformIds,
    selectedClusterIds,
    enumeratedCandidates,
    predictionBySmiles,
    referencePredictionByBackend,
    lastPredictionTaskId,
    lastMmpTaskId,
    mmpRunVersion,
    hasSelection,
    runMmpQuery,
    loadQueryRun,
    runCluster,
    setClusterGrouping,
    runEnumerate,
    runPredictCandidate,
    runPredictReferenceForBackend,
    runPredictBatch,
    ensurePredictionResult,
    ensureReferencePredictionResult,
    handleTransformClick,
    toggleTransformSelection,
    toggleClusterSelection,
    selectTopTransforms,
    selectTopClusters,
    clearSelections,
    hydrateFromSnapshot,
    evidencePairs,
    activeTransformSummary
  };
}
