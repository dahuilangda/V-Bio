export const PEPTIDE_TASK_PREVIEW_KEY = 'peptide_preview';

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
}

function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

function readFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function firstFiniteMetric(payloads: Record<string, unknown>[], paths: string[]): number | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readFiniteNumber(readObjectPath(payload, path));
      if (value !== null) return value;
    }
  }
  return null;
}

function firstTextMetric(payloads: Record<string, unknown>[], paths: string[]): string {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readText(readObjectPath(payload, path));
      if (value) return value;
    }
  }
  return '';
}

function normalizePlddtValue(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  if (value >= 0 && value <= 1) return Math.max(0, Math.min(100, value * 100));
  return Math.max(0, Math.min(100, value));
}

function normalizeIptmValue(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  if (value > 1 && value <= 100) return value / 100;
  if (value < 0) return null;
  return value;
}

function normalizeInt(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  return Math.max(0, Math.floor(value));
}

function normalizePeptideDesignMode(value: string): 'linear' | 'cyclic' | 'bicyclic' | null {
  const token = value.trim().toLowerCase().replace(/[\s_-]+/g, '');
  if (!token) return null;
  if (token === 'linear') return 'linear';
  if (token === 'cyclic' || token === 'cycle' || token === 'monocyclic') return 'cyclic';
  if (token === 'bicyclic' || token === 'bicycle' || token === 'doublecyclic') return 'bicyclic';
  return null;
}

function toFiniteNumberArray(value: unknown): number[] {
  if (Array.isArray(value)) {
    return value
      .map((item) => readFiniteNumber(item))
      .filter((item): item is number => item !== null);
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    const scalarEntries = Object.entries(obj)
      .map(([key, item]) => ({
        key,
        keyNumber: Number(key),
        value: readFiniteNumber(item)
      }))
      .filter((entry) => entry.value !== null);
    const numericKeyEntries = scalarEntries.filter((entry) => Number.isFinite(entry.keyNumber));
    if (numericKeyEntries.length >= 3 && numericKeyEntries.length >= Math.floor(scalarEntries.length * 0.6)) {
      numericKeyEntries.sort((a, b) => a.keyNumber - b.keyNumber);
      return numericKeyEntries.map((entry) => entry.value as number);
    }
    const nested = [obj.values, obj.value, obj.plddt, obj.plddts, obj.residue_plddt, obj.residue_plddts, obj.scores];
    for (const item of nested) {
      if (Array.isArray(item)) {
        const parsed = toFiniteNumberArray(item);
        if (parsed.length > 0) return parsed;
      }
    }
  }
  return [];
}

function normalizeResiduePlddts(values: number[]): number[] {
  return values
    .map((value) => normalizePlddtValue(value))
    .filter((value): value is number => value !== null);
}

function readCandidateSequence(row: Record<string, unknown>): string {
  return (
    firstTextMetric([row], ['peptide_sequence', 'binder_sequence', 'candidate_sequence', 'designed_sequence', 'sequence'])
      .replace(/\s+/g, '')
      .trim()
      .toUpperCase()
  );
}

function compareCandidateRows(a: Record<string, unknown>, b: Record<string, unknown>, aIndex: number, bIndex: number): number {
  const aRank = firstFiniteMetric([a], ['rank', 'ranking', 'order']);
  const bRank = firstFiniteMetric([b], ['rank', 'ranking', 'order']);
  const aRankValue = aRank === null ? null : Math.max(1, Math.floor(aRank));
  const bRankValue = bRank === null ? null : Math.max(1, Math.floor(bRank));
  if (aRankValue !== null && bRankValue !== null && aRankValue !== bRankValue) return aRankValue - bRankValue;
  if (aRankValue !== null && bRankValue === null) return -1;
  if (aRankValue === null && bRankValue !== null) return 1;

  const aScore = firstFiniteMetric([a], ['composite_score', 'score', 'fitness', 'objective']);
  const bScore = firstFiniteMetric([b], ['composite_score', 'score', 'fitness', 'objective']);
  if (aScore !== null && bScore !== null && aScore !== bScore) return bScore - aScore;
  if (aScore !== null && bScore === null) return -1;
  if (aScore === null && bScore !== null) return 1;

  const aPlddt = normalizePlddtValue(firstFiniteMetric([a], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt']));
  const bPlddt = normalizePlddtValue(firstFiniteMetric([b], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt']));
  if (aPlddt !== null && bPlddt !== null && aPlddt !== bPlddt) return bPlddt - aPlddt;
  if (aPlddt !== null && bPlddt === null) return -1;
  if (aPlddt === null && bPlddt !== null) return 1;

  return aIndex - bIndex;
}

function firstRecordArray(payloads: Record<string, unknown>[], paths: string[]): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = asRecordArray(readObjectPath(payload, path));
      if (rows.length > 0) return rows;
    }
  }
  return [];
}

function readPeptideCandidateCount(payloads: Record<string, unknown>[]): number | null {
  const direct = firstFiniteMetric(payloads, [
    'candidate_count',
    'num_candidates',
    'best_sequence_count',
    'peptide_design.candidate_count'
  ]);
  if (direct !== null) return normalizeInt(direct);
  const rows = firstRecordArray(payloads, [
    'peptide_design.best_sequences',
    'peptide_design.current_best_sequences',
    'peptide_design.candidates',
    'best_sequences',
    'current_best_sequences',
    'candidates',
    'progress.best_sequences',
    'progress.current_best_sequences'
  ]);
  return rows.length > 0 ? rows.length : null;
}

function buildPeptidePreview(payload: Record<string, unknown>): Record<string, unknown> | null {
  if (Object.keys(payload).length === 0) return null;
  const peptideDesign = asRecord(payload.peptide_design);
  const peptideProgress = asRecord(peptideDesign.progress);
  const topProgress = asRecord(payload.progress);
  const requestPayload = asRecord(payload.request);
  const requestOptions = asRecord(requestPayload.options);
  const inputPayload = asRecord(payload.inputs);
  const inputOptions = asRecord(inputPayload.options);
  const payloads = [payload, peptideDesign, peptideProgress, topProgress, requestPayload, requestOptions, inputPayload, inputOptions];

  const designMode = normalizePeptideDesignMode(
    firstTextMetric(payloads, [
      'design_mode',
      'mode',
      'peptide_design_mode',
      'peptideDesignMode',
      'peptide_design.design_mode',
      'peptide_design.mode',
      'request.options.peptide_design_mode',
      'request.options.peptideDesignMode',
      'inputs.options.peptide_design_mode',
      'inputs.options.peptideDesignMode'
    ])
  );
  const binderLength = normalizeInt(
    firstFiniteMetric(payloads, [
      'binder_length',
      'length',
      'peptide_binder_length',
      'peptideBinderLength',
      'peptide_design.binder_length',
      'peptide_design.length',
      'request.options.peptide_binder_length',
      'request.options.peptideBinderLength',
      'inputs.options.peptide_binder_length',
      'inputs.options.peptideBinderLength'
    ])
  );
  const iterations = normalizeInt(
    firstFiniteMetric(payloads, [
      'peptide_iterations',
      'peptideIterations',
      'generations',
      'total_generations',
      'peptide_design.iterations',
      'peptide_design.generations',
      'peptide_design.total_generations',
      'request.options.peptide_iterations',
      'request.options.peptideIterations',
      'inputs.options.peptide_iterations',
      'inputs.options.peptideIterations'
    ])
  );
  const populationSize = normalizeInt(
    firstFiniteMetric(payloads, [
      'population_size',
      'peptide_population_size',
      'peptidePopulationSize',
      'peptide_design.population_size',
      'request.options.peptide_population_size',
      'request.options.peptidePopulationSize',
      'inputs.options.peptide_population_size',
      'inputs.options.peptidePopulationSize'
    ])
  );
  const eliteSize = normalizeInt(
    firstFiniteMetric(payloads, [
      'elite_size',
      'num_elites',
      'peptide_elite_size',
      'peptideEliteSize',
      'peptide_design.elite_size',
      'request.options.peptide_elite_size',
      'request.options.peptideEliteSize',
      'inputs.options.peptide_elite_size',
      'inputs.options.peptideEliteSize'
    ])
  );
  const mutationRateRaw = firstFiniteMetric(payloads, [
    'mutation_rate',
    'peptide_mutation_rate',
    'peptideMutationRate',
    'peptide_design.mutation_rate',
    'request.options.peptide_mutation_rate',
    'request.options.peptideMutationRate',
    'inputs.options.peptide_mutation_rate',
    'inputs.options.peptideMutationRate'
  ]);
  const mutationRate = mutationRateRaw === null ? null : mutationRateRaw > 1 && mutationRateRaw <= 100 ? mutationRateRaw / 100 : mutationRateRaw;
  const currentGeneration = normalizeInt(
    firstFiniteMetric(payloads, [
      'current_generation',
      'generation',
      'iter',
      'progress.current_generation',
      'peptide_design.current_generation',
      'peptide_design.progress.current_generation'
    ])
  );
  const totalGenerations = normalizeInt(
    firstFiniteMetric(payloads, [
      'total_generations',
      'generations',
      'max_generation',
      'progress.total_generations',
      'peptide_design.total_generations',
      'peptide_design.progress.total_generations'
    ])
  );
  const bestScore = firstFiniteMetric(payloads, [
    'best_score',
    'current_best_score',
    'score',
    'peptide_design.best_score',
    'peptide_design.current_best_score'
  ]);
  const completedTasks = normalizeInt(
    firstFiniteMetric(payloads, [
      'completed_tasks',
      'done_tasks',
      'finished_tasks',
      'peptide_design.completed_tasks',
      'peptide_design.progress.completed_tasks'
    ])
  );
  const pendingTasks = normalizeInt(
    firstFiniteMetric(payloads, [
      'pending_tasks',
      'queued_tasks',
      'peptide_design.pending_tasks',
      'peptide_design.progress.pending_tasks'
    ])
  );
  const totalTasksRaw = firstFiniteMetric(payloads, ['total_tasks', 'task_total', 'peptide_design.total_tasks', 'peptide_design.progress.total_tasks']);
  const totalTasks = normalizeInt(totalTasksRaw !== null ? totalTasksRaw : completedTasks !== null && pendingTasks !== null ? completedTasks + pendingTasks : null);
  const candidateCount = readPeptideCandidateCount(payloads);
  const currentStatus = firstTextMetric(payloads, [
    'current_status',
    'status_stage',
    'stage',
    'progress.current_status',
    'peptide_design.current_status',
    'peptide_design.status_stage',
    'peptide_design.stage',
    'peptide_design.progress.current_status'
  ]);
  const statusMessage = firstTextMetric(payloads, [
    'status_message',
    'message',
    'status',
    'progress.status_message',
    'peptide_design.status_message',
    'peptide_design.progress.status_message'
  ]);

  const candidateRows = firstRecordArray(payloads, [
    'peptide_design.best_sequences',
    'peptide_design.current_best_sequences',
    'peptide_design.candidates',
    'best_sequences',
    'current_best_sequences',
    'candidates',
    'progress.best_sequences',
    'progress.current_best_sequences'
  ]);
  let bestCandidate: Record<string, unknown> | null = null;
  if (candidateRows.length > 0) {
    const order = new Map(candidateRows.map((row, index) => [row, index] as const));
    const sorted = [...candidateRows].sort((a, b) => compareCandidateRows(a, b, order.get(a) ?? 0, order.get(b) ?? 0));
    const best = sorted.find((row) => Boolean(readCandidateSequence(row))) || sorted[0];
    const sequence = readCandidateSequence(best);
    if (sequence) {
      const residuePlddts = normalizeResiduePlddts(
        toFiniteNumberArray(
          readObjectPath(best, 'residue_plddts') ??
            readObjectPath(best, 'residue_plddt') ??
            readObjectPath(best, 'per_residue_plddt') ??
            readObjectPath(best, 'plddts')
        )
      );
      bestCandidate = {
        sequence,
        plddt: normalizePlddtValue(firstFiniteMetric([best], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt'])),
        iptm: normalizeIptmValue(firstFiniteMetric([best], ['pair_iptm_target_binder', 'pair_iptm', 'iptm'])),
        score: firstFiniteMetric([best], ['composite_score', 'score', 'fitness', 'objective']),
        rank: normalizeInt(firstFiniteMetric([best], ['rank', 'ranking', 'order'])),
        generation: normalizeInt(firstFiniteMetric([best], ['generation', 'iteration', 'iter'])),
        binder_chain_id: firstTextMetric([best, payload, peptideDesign, peptideProgress, topProgress], [
          'binder_chain_id',
          'model_ligand_chain_id',
          'requested_ligand_chain_id',
          'ligand_chain_id'
        ]),
        residue_plddts: residuePlddts.length >= Math.min(sequence.length, 4) ? residuePlddts : []
      };
    }
  }

  const preview: Record<string, unknown> = {};
  if (designMode) preview.design_mode = designMode;
  if (binderLength !== null) preview.binder_length = binderLength;
  if (iterations !== null) preview.iterations = iterations;
  if (populationSize !== null) preview.population_size = populationSize;
  if (eliteSize !== null) preview.elite_size = eliteSize;
  if (mutationRate !== null) preview.mutation_rate = mutationRate;
  if (currentGeneration !== null) preview.current_generation = currentGeneration;
  if (totalGenerations !== null) preview.total_generations = totalGenerations;
  if (bestScore !== null) preview.best_score = bestScore;
  if (candidateCount !== null) preview.candidate_count = candidateCount;
  if (completedTasks !== null) preview.completed_tasks = completedTasks;
  if (pendingTasks !== null) preview.pending_tasks = pendingTasks;
  if (totalTasks !== null) preview.total_tasks = totalTasks;
  if (currentStatus) preview.current_status = currentStatus;
  if (statusMessage) preview.status_message = statusMessage;
  if (bestCandidate) preview.best_candidate = bestCandidate;

  return Object.keys(preview).length > 0 ? preview : null;
}

export function buildQueuedPeptidePreviewFromOptions(options: Record<string, unknown>): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    request: {
      options: { ...options }
    },
    peptide_design: {
      current_status: 'queued',
      status_message: 'Task submitted and waiting in queue'
    },
    progress: {
      current_status: 'queued',
      status_message: 'Task submitted and waiting in queue'
    }
  };
  return buildPeptidePreview(payload) || {};
}

export function readPeptidePreviewFromProperties(properties: unknown): Record<string, unknown> | null {
  const props = asRecord(properties);
  const preview = asRecord(props[PEPTIDE_TASK_PREVIEW_KEY]);
  return Object.keys(preview).length > 0 ? preview : null;
}

export function mergePeptidePreviewIntoProperties(baseProperties: unknown, confidenceLike: unknown): Record<string, unknown> | null {
  const payload = asRecord(confidenceLike);
  const nextPreview = buildPeptidePreview(payload);
  if (!nextPreview) return null;

  const base = asRecord(baseProperties);
  const prevPreview = asRecord(base[PEPTIDE_TASK_PREVIEW_KEY]);
  const mergedPreview: Record<string, unknown> = {
    ...prevPreview,
    ...nextPreview
  };
  const mergedBestCandidate = {
    ...asRecord(prevPreview.best_candidate),
    ...asRecord(nextPreview.best_candidate)
  };
  if (Object.keys(mergedBestCandidate).length > 0) {
    mergedPreview.best_candidate = mergedBestCandidate;
  }

  const merged = {
    ...base,
    [PEPTIDE_TASK_PREVIEW_KEY]: mergedPreview
  };
  if (JSON.stringify(merged) === JSON.stringify(base)) return null;
  return merged;
}
