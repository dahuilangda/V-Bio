import { useCallback, useEffect, useRef, useState } from 'react';
import {
  fetchLeadOptimizationHaloStatus,
  submitLeadOptimizationHaloOptimize,
  type LeadOptHaloOptimizeInput,
  type LeadOptHaloRoundEvent
} from '../../../../api/backendLeadOptimizationApi';
import { downloadResultBlob } from '../../../../api/backendTaskApi';
import { parseResultBundle } from '../../../../api/resultParser/resultBundleParser';

export interface LeadOptHaloCandidate {
  smiles: string;
  round?: number;
  source?: string;
  affinity_pic50?: number;
  ipsae?: number;
  ligand_plddt_mean?: number;
  final_reward?: number;
}

export interface LeadOptHaloRunState {
  progress: LeadOptHaloRoundEvent | null;
  candidates: LeadOptHaloCandidate[];
  roundsLog: Array<Record<string, unknown>>;
  mode: string;
  backend: string;
}

interface UseLeadOptHaloRunArgs {
  onTaskQueued: (payload: { taskId: string; input: LeadOptHaloOptimizeInput }) => void | Promise<void>;
  onTaskCompleted: (payload: {
    taskId: string;
    candidates: LeadOptHaloCandidate[];
    roundsLog: Array<Record<string, unknown>>;
    roundsCompleted: number | null;
    totalRounds: number | null;
    mode: string;
    backend: string;
  }) => void | Promise<void>;
  onTaskFailed: (payload: { taskId: string; error: string }) => void | Promise<void>;
}

const POLL_INTERVAL_MS = 4000;
// Hard ceiling on a single run's polling: without it a worker that dies
// without a terminal status (celery PENDING forever after Ignore()) would
// poll indefinitely and keep `running` latched, disabling the Run button.
const RUN_TIMEOUT_MS = 24 * 60 * 60 * 1000;
const TERMINAL_OK = new Set(['success', 'succeeded', 'completed']);
const TERMINAL_BAD = new Set(['failure', 'failed', 'revoked', 'rejected']);

/**
 * One submission → backend multi-round RL loop. Streams peptide-style
 * per-round progress (round N/total, best affinity, top candidates) from the
 * halo status endpoint, then parses the result archive into candidates.
 */
export function useLeadOptHaloRun({
  onTaskQueued,
  onTaskCompleted,
  onTaskFailed
}: UseLeadOptHaloRunArgs) {
  const [running, setRunning] = useState(false);
  const [runState, setRunState] = useState<LeadOptHaloRunState>({
    progress: null,
    candidates: [],
    roundsLog: [],
    mode: '',
    backend: ''
  });
  const [error, setError] = useState('');
  const pollTimerRef = useRef<number | null>(null);
  const runningRef = useRef(false);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const finishRun = useCallback(() => {
    runningRef.current = false;
    setRunning(false);
    stopPolling();
  }, [stopPolling]);

  const poll = useCallback(
    async (taskId: string, deadline = Date.now() + RUN_TIMEOUT_MS) => {
      if (Date.now() > deadline) {
        const message = `Optimization did not report a terminal state within ${Math.round(RUN_TIMEOUT_MS / 60000)} minutes.`;
        setError(message);
        finishRun();
        await onTaskFailed({ taskId, error: message });
        return;
      }
      try {
        const status = await fetchLeadOptimizationHaloStatus(taskId);
        const state = String(status.state || '').toLowerCase();
        const haloEvent = status.info?.payload?.halo || null;
        if (haloEvent) {
          setRunState((previous) => ({ ...previous, progress: haloEvent }));
        }
        if (TERMINAL_OK.has(state)) {
          let candidates: LeadOptHaloCandidate[] = [];
          let roundsLog: Array<Record<string, unknown>> = [];
          let roundsCompleted: number | null = null;
          let totalRounds: number | null = null;
          let mode = '';
          let backend = '';
          try {
            const blob = await downloadResultBlob(taskId, { mode: 'view' });
            const parsed = await parseResultBundle(blob);
            const halo = (parsed?.confidence as Record<string, unknown> | undefined)?.lead_opt_halo;
            if (halo && typeof halo === 'object') {
              const record = halo as Record<string, unknown>;
              const rawCandidates = Array.isArray(record.candidates) ? record.candidates : [];
              const toOptionalNumber = (value: unknown): number | undefined => {
                const parsed = Number(value);
                return Number.isFinite(parsed) ? parsed : undefined;
              };
              candidates = rawCandidates.map((row) => {
                const entry = (row || {}) as Record<string, unknown>;
                return {
                  smiles: String(entry.smiles || ''),
                  round: toOptionalNumber(entry.round),
                  source: entry.source ? String(entry.source) : undefined,
                  affinity_pic50: toOptionalNumber(entry.affinity_pic50),
                  ipsae: toOptionalNumber(entry.ipsae),
                  ligand_plddt_mean: toOptionalNumber(entry.ligand_plddt_mean),
                  final_reward: toOptionalNumber(entry.final_reward),
                } satisfies LeadOptHaloCandidate;
              }).filter((row) => row.smiles);
              roundsLog = Array.isArray(record.rounds_log) ? record.rounds_log as Array<Record<string, unknown>> : [];
              roundsCompleted = Number(record.rounds_completed) || null;
              totalRounds = Number(record.total_rounds) || null;
              mode = String(record.mode || '');
              backend = String(record.backend || '');
            }
          } catch (parseError) {
            setError(parseError instanceof Error ? parseError.message : 'Failed to parse halo results.');
          }
          setRunState({ progress: null, candidates, roundsLog, mode, backend });
          finishRun();
          await onTaskCompleted({ taskId, candidates, roundsLog, roundsCompleted, totalRounds, mode, backend });
          return;
        }
        if (TERMINAL_BAD.has(state)) {
          const details = String(status.info?.details || '').trim();
          setError(details || `Optimization failed (${state}).`);
          finishRun();
          await onTaskFailed({ taskId, error: details || `Optimization failed (${state}).` });
          return;
        }
      } catch (pollError) {
        // Transient poll failures keep polling; the run timeout below is the guard.
        if (pollError instanceof Error && /404|403/.test(pollError.message)) {
          setError(pollError.message);
          finishRun();
          await onTaskFailed({ taskId, error: pollError.message });
          return;
        }
      }
      if (!runningRef.current) return;
      pollTimerRef.current = window.setTimeout(() => {
        void poll(taskId, deadline);
      }, POLL_INTERVAL_MS);
    },
    [finishRun, onTaskCompleted, onTaskFailed]
  );

  const submit = useCallback(
    async (input: LeadOptHaloOptimizeInput) => {
      if (runningRef.current) return;
      setError('');
      setRunState({ progress: null, candidates: [], roundsLog: [], mode: '', backend: '' });
      runningRef.current = true;
      setRunning(true);
      try {
        const response = await submitLeadOptimizationHaloOptimize(input);
        await onTaskQueued({ taskId: response.task_id, input });
        void poll(response.task_id);
      } catch (submitError) {
        finishRun();
        setError(submitError instanceof Error ? submitError.message : 'Failed to start optimization.');
      }
    },
    [finishRun, onTaskQueued, poll]
  );

  return { running, runState, error, submit };
}
