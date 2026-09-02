import type { ProjectTask, ProteinModification } from '../../types/models';
import { readPeptidePreviewFromProperties } from '../../utils/peptideTaskPreview';
import { asRecord } from './recordReaders';
import { readObjectPath, normalizeProbability, chainKeysMatch } from './taskDataCore';
import { toFiniteNumberArray, normalizeAtomPlddts } from './taskDataConfidence';

interface PeptideTaskSummary {
  designMode: 'linear' | 'cyclic' | 'bicyclic' | null;
  binderLength: number | null;
  iterations: number | null;
  populationSize: number | null;
  eliteSize: number | null;
  mutationRate: number | null;
  currentGeneration: number | null;
  totalGenerations: number | null;
  bestScore: number | null;
  candidateCount: number | null;
  completedTasks: number | null;
  pendingTasks: number | null;
  totalTasks: number | null;
  stage: string;
  statusMessage: string;
}

interface LeadOptTaskSummary {
  stage: string;
  summary: string;
  databaseId: string;
  databaseLabel: string;
  databaseSchema: string;
  transformCount: number | null;
  candidateCount: number | null;
  bucketCount: number | null;
  predictionTotal: number | null;
  predictionQueued: number | null;
  predictionRunning: number | null;
  predictionSuccess: number | null;
  predictionFailure: number | null;
  selectedFragmentIds: string[];
  selectedAtomIndices: number[];
  selectedFragmentQuery: string;
}

interface PeptideBestCandidatePreview {
  sequence: string;
  modifications: ProteinModification[];
  plddt: number | null;
  iptm: number | null;
  residuePlddts: number[] | null;
  binderChainId: string | null;
}

function readFiniteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === 'string' ? item.trim() : ''))
    .filter(Boolean);
}

function readFirstFiniteFromPayloadPaths(payloads: Record<string, unknown>[], paths: string[]): number | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readFiniteNumber(readObjectPath(payload, path));
      if (value !== null) return value;
    }
  }
  return null;
}

function readFirstTextFromPayloadPaths(payloads: Record<string, unknown>[], paths: string[]): string {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = String(readObjectPath(payload, path) || '').trim();
      if (value) return value;
    }
  }
  return '';
}

function readFirstRecordArrayFromPayloadPaths(
  payloads: Record<string, unknown>[],
  paths: string[]
): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = readObjectPath(payload, path);
      if (!Array.isArray(rows)) continue;
      const records = rows.filter(
        (item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item))
      );
      if (records.length > 0) return records;
    }
  }
  return [];
}

function normalizePeptideDesignMode(value: string): 'linear' | 'cyclic' | 'bicyclic' | null {
  const token = value.trim().toLowerCase().replace(/[\s_-]+/g, '');
  if (!token) return null;
  if (token === 'linear') return 'linear';
  if (token === 'cyclic' || token === 'cycle' || token === 'monocyclic') return 'cyclic';
  if (token === 'bicyclic' || token === 'bicycle' || token === 'doublecyclic') return 'bicyclic';
  return null;
}

function normalizeNonNegativeInteger(value: number | null): number | null {
  if (value === null) return null;
  return Math.max(0, Math.floor(value));
}

function readPeptideTaskCandidateCount(payloads: Record<string, unknown>[]): number | null {
  const direct = readFirstFiniteFromPayloadPaths(payloads, [
    'candidate_count',
    'num_candidates',
    'best_sequence_count',
    'peptide_design.candidate_count'
  ]);
  if (direct !== null) return Math.max(0, Math.floor(direct));
  const arrayPaths = [
    'best_sequences',
    'current_best_sequences',
    'candidates',
    'peptide_candidates',
    'peptide_design.best_sequences',
    'peptide_design.current_best_sequences',
    'peptide_design.candidates'
  ];
  for (const payload of payloads) {
    for (const path of arrayPaths) {
      const rows = readObjectPath(payload, path);
      if (Array.isArray(rows) && rows.length > 0) return rows.length;
    }
  }
  return null;
}

function readPeptideTaskSummary(task: ProjectTask): PeptideTaskSummary | null {
  const peptidePreview = readPeptidePreviewFromProperties(task.properties);
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const peptideDesign =
    confidence?.peptide_design && typeof confidence.peptide_design === 'object' && !Array.isArray(confidence.peptide_design)
      ? (confidence.peptide_design as Record<string, unknown>)
      : {};
  const peptideProgress =
    peptideDesign.progress && typeof peptideDesign.progress === 'object' && !Array.isArray(peptideDesign.progress)
      ? (peptideDesign.progress as Record<string, unknown>)
      : {};
  const topProgress =
    confidence?.progress && typeof confidence.progress === 'object' && !Array.isArray(confidence.progress)
      ? (confidence.progress as Record<string, unknown>)
      : {};
  const requestPayload =
    confidence?.request && typeof confidence.request === 'object' && !Array.isArray(confidence.request)
      ? (confidence.request as Record<string, unknown>)
      : {};
  const requestOptions =
    requestPayload.options && typeof requestPayload.options === 'object' && !Array.isArray(requestPayload.options)
      ? (requestPayload.options as Record<string, unknown>)
      : {};
  const inputPayload =
    confidence?.inputs && typeof confidence.inputs === 'object' && !Array.isArray(confidence.inputs)
      ? (confidence.inputs as Record<string, unknown>)
      : {};
  const inputOptions =
    inputPayload.options && typeof inputPayload.options === 'object' && !Array.isArray(inputPayload.options)
      ? (inputPayload.options as Record<string, unknown>)
      : {};
  const taskRecord = task as unknown as Record<string, unknown>;
  const taskOptions =
    taskRecord.options && typeof taskRecord.options === 'object' && !Array.isArray(taskRecord.options)
      ? (taskRecord.options as Record<string, unknown>)
      : {};
  const payloads = [
    confidence || {},
    topProgress,
    peptideDesign,
    peptideProgress,
    requestPayload,
    requestOptions,
    inputPayload,
    inputOptions,
    taskOptions,
    peptidePreview || {}
  ];

  const designMode = normalizePeptideDesignMode(
    readFirstTextFromPayloadPaths(payloads, [
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

  const binderLength = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
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

  const iterations = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
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

  const populationSize = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
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

  const eliteSize = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
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

  const mutationRateRaw = readFirstFiniteFromPayloadPaths(payloads, [
    'mutation_rate',
    'peptide_mutation_rate',
    'peptide_design.mutation_rate',
    'request.options.peptide_mutation_rate',
    'inputs.options.peptide_mutation_rate',
  ]);
  const mutationRate = mutationRateRaw === null ? null : mutationRateRaw > 1 && mutationRateRaw <= 100 ? mutationRateRaw / 100 : mutationRateRaw;

  const currentGeneration = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'current_generation',
      'generation',
      'iter',
      'progress.current_generation',
      'peptide_design.current_generation',
      'peptide_design.progress.current_generation'
    ])
  );
  const totalGenerations = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'total_generations',
      'generations',
      'max_generation',
      'progress.total_generations',
      'peptide_design.total_generations',
      'peptide_design.progress.total_generations'
    ])
  );
  const bestScore = readFirstFiniteFromPayloadPaths(payloads, [
    'best_score',
    'current_best_score',
    'score',
    'peptide_design.best_score',
    'peptide_design.current_best_score'
  ]);
  const completedTasks = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'completed_tasks',
      'done_tasks',
      'finished_tasks',
      'peptide_design.completed_tasks',
      'peptide_design.progress.completed_tasks'
    ])
  );
  const pendingTasks = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'pending_tasks',
      'queued_tasks',
      'peptide_design.pending_tasks',
      'peptide_design.progress.pending_tasks'
    ])
  );
  const totalTasksRaw = readFirstFiniteFromPayloadPaths(payloads, [
    'total_tasks',
    'task_total',
    'peptide_design.total_tasks',
    'peptide_design.progress.total_tasks'
  ]);
  const totalTasks = normalizeNonNegativeInteger(
    totalTasksRaw !== null ? totalTasksRaw : completedTasks !== null && pendingTasks !== null ? completedTasks + pendingTasks : null
  );
  const candidateCount = readPeptideTaskCandidateCount(payloads);
  const stage = readFirstTextFromPayloadPaths(payloads, [
    'current_status',
    'status_stage',
    'stage',
    'progress.current_status',
    'peptide_design.current_status',
    'peptide_design.status_stage',
    'peptide_design.stage',
    'peptide_design.progress.current_status'
  ]);
  const statusMessage = readFirstTextFromPayloadPaths(payloads, [
    'status_message',
    'message',
    'status',
    'progress.status_message',
    'peptide_design.status_message',
    'peptide_design.progress.status_message'
  ]);

  if (
    !peptidePreview &&
    !confidence &&
    designMode === null &&
    binderLength === null &&
    iterations === null &&
    populationSize === null &&
    eliteSize === null &&
    mutationRate === null &&
    currentGeneration === null &&
    totalGenerations === null &&
    bestScore === null &&
    candidateCount === null &&
    completedTasks === null &&
    pendingTasks === null &&
    totalTasks === null &&
    !stage &&
    !statusMessage
  ) {
    return null;
  }

  return {
    designMode,
    binderLength,
    iterations,
    populationSize,
    eliteSize,
    mutationRate,
    currentGeneration,
    totalGenerations,
    bestScore,
    candidateCount,
    completedTasks,
    pendingTasks,
    totalTasks,
    stage,
    statusMessage
  };
}

function normalizePeptideCandidateSequence(value: string): string {
  return value.replace(/\s+/g, '').trim().toUpperCase();
}

function normalizePlddtScalar(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  if (value >= 0 && value <= 1) return value * 100;
  return value;
}

function readPeptideCandidateSequence(row: Record<string, unknown>): string {
  const sequence = readFirstTextFromPayloadPaths([row], [
    'peptide_sequence',
    'binder_sequence',
    'candidate_sequence',
    'designed_sequence',
    'sequence'
  ]);
  return normalizePeptideCandidateSequence(sequence);
}

function readPeptideCandidateModifications(row: Record<string, unknown>, sequenceLength: number): ProteinModification[] {
  const raw =
    readObjectPath(row, 'modifications') ??
    readObjectPath(row, 'protein_modifications') ??
    readObjectPath(row, 'residue_modifications') ??
    readObjectPath(row, 'residueMods') ??
    readObjectPath(row, 'residue_mods') ??
    readObjectPath(row, 'mods') ??
    readObjectPath(row, 'result.modifications') ??
    readObjectPath(row, 'prediction.modifications') ??
    readObjectPath(row, 'metadata.modifications') ??
    readObjectPath(row, 'structure_payload.modifications');
  if (!Array.isArray(raw)) return [];
  const rows: ProteinModification[] = [];
  const seen = new Set<number>();
  raw.forEach((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return;
    const record = item as Record<string, unknown>;
    const position = Math.floor(Number(record.position ?? record.residue_index ?? record.residue ?? record.pos));
    const ccd = String(record.ccd ?? record.code ?? record.residue_name ?? '').trim().toUpperCase();
    if (!Number.isFinite(position) || position < 1 || position > sequenceLength || !ccd || seen.has(position)) return;
    seen.add(position);
    rows.push({
      id: String(record.id || `peptide-mod-${position}-${ccd}-${index}`),
      position,
      baseResidue: String(record.baseResidue ?? record.base_residue ?? '').trim().toUpperCase().slice(0, 1),
      ccd,
      inputMethod: String(record.inputMethod ?? record.input_method ?? '').trim().toLowerCase() === 'jsme' ? 'jsme' : 'ccd',
      smiles: typeof record.smiles === 'string' ? record.smiles : undefined,
      label: typeof record.label === 'string' ? record.label : undefined
    });
  });
  return rows.sort((a, b) => a.position - b.position);
}


function comparePeptideCandidateRows(a: Record<string, unknown>, b: Record<string, unknown>, aIndex: number, bIndex: number): number {
  const aRank = readFirstFiniteFromPayloadPaths([a], ['rank', 'ranking', 'order']);
  const bRank = readFirstFiniteFromPayloadPaths([b], ['rank', 'ranking', 'order']);
  const aRankValue = aRank !== null ? Math.max(1, Math.floor(aRank)) : null;
  const bRankValue = bRank !== null ? Math.max(1, Math.floor(bRank)) : null;
  if (aRankValue !== null && bRankValue !== null && aRankValue !== bRankValue) {
    return aRankValue - bRankValue;
  }
  if (aRankValue !== null && bRankValue === null) return -1;
  if (aRankValue === null && bRankValue !== null) return 1;

  const aScore = readFirstFiniteFromPayloadPaths([a], ['composite_score', 'score', 'fitness', 'objective']);
  const bScore = readFirstFiniteFromPayloadPaths([b], ['composite_score', 'score', 'fitness', 'objective']);
  if (aScore !== null && bScore !== null && aScore !== bScore) {
    return bScore - aScore;
  }
  if (aScore !== null && bScore === null) return -1;
  if (aScore === null && bScore !== null) return 1;

  const aPlddt = normalizePlddtScalar(
    readFirstFiniteFromPayloadPaths([a], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt'])
  );
  const bPlddt = normalizePlddtScalar(
    readFirstFiniteFromPayloadPaths([b], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt'])
  );
  if (aPlddt !== null && bPlddt !== null && aPlddt !== bPlddt) {
    return bPlddt - aPlddt;
  }
  if (aPlddt !== null && bPlddt === null) return -1;
  if (aPlddt === null && bPlddt !== null) return 1;

  return aIndex - bIndex;
}

function alignResidueSeriesToSequence(values: number[], sequenceLength: number): number[] {
  const normalized = normalizeAtomPlddts(values);
  if (normalized.length === 0) return [];
  if (sequenceLength <= 0) return normalized;
  if (normalized.length === sequenceLength) return normalized;
  if (normalized.length < Math.min(sequenceLength, 4)) return [];
  if (normalized.length > sequenceLength) {
    const reduced: number[] = [];
    for (let i = 0; i < sequenceLength; i += 1) {
      const start = Math.floor((i * normalized.length) / sequenceLength);
      const end = Math.max(start + 1, Math.floor(((i + 1) * normalized.length) / sequenceLength));
      const chunk = normalized.slice(start, end);
      reduced.push(chunk.reduce((sum, value) => sum + value, 0) / chunk.length);
    }
    return reduced;
  }
  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * normalized.length) / sequenceLength);
    expanded.push(normalized[Math.min(normalized.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}

function readResidueSeriesByChain(
  value: unknown,
  sequenceLength: number,
  preferredChainId: string | null | undefined
): number[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  const record = value as Record<string, unknown>;
  const preferred = String(preferredChainId || '').trim();
  let best: { values: number[]; score: number } | null = null;
  for (const [chainId, raw] of Object.entries(record)) {
    const values = normalizeAtomPlddts(toFiniteNumberArray(raw));
    if (values.length === 0) continue;
    let score = 0;
    if (preferred && chainKeysMatch(chainId, preferred)) score += 48;
    score -= Math.abs(values.length - sequenceLength) * 4;
    if (values.length === sequenceLength) score += 30;
    if (values.length >= Math.max(1, sequenceLength - 2) && values.length <= sequenceLength + 2) score += 16;
    if (!best || score > best.score) {
      best = { values, score };
    }
  }
  if (!best) return [];
  return alignResidueSeriesToSequence(best.values, sequenceLength);
}

function readCandidateResiduePlddts(
  row: Record<string, unknown>,
  sequenceLength: number,
  preferredChainId: string | null | undefined
): number[] | null {
  const directCandidates = [
    'residue_plddts',
    'residue_plddt',
    'per_residue_plddt',
    'plddts',
    'residue_confidence',
    'residue_confidences',
    'residue_scores',
    'per_residue_confidence',
    'binder_residue_plddt',
    'binder_residue_plddts',
    'binder_plddt_per_residue',
    'plddt_per_residue',
    'plddt_by_residue',
    'aa_plddt',
    'aa_plddts',
    'ligand_residue_plddt',
    'ligand_residue_plddts',
    'token_plddt',
    'token_plddts',
    'confidence.residue_plddt',
    'confidence.residue_plddts',
    'confidence.plddts',
    'confidence.per_residue_plddt',
    'confidence.binder_residue_plddt',
    'confidence.token_plddt',
    'confidence.token_plddts',
    'metrics.residue_plddt',
    'metrics.per_residue_plddt',
    'scores.plddts',
    'scores.residue_plddt',
    'scores.per_residue_plddt'
  ];
  for (const path of directCandidates) {
    const direct = alignResidueSeriesToSequence(toFiniteNumberArray(readObjectPath(row, path)), sequenceLength);
    if (direct.length >= Math.min(sequenceLength, 4)) return direct;
  }

  const byChainCandidates = [
    readObjectPath(row, 'residue_plddt_by_chain'),
    readObjectPath(row, 'residuePlddtByChain'),
    readObjectPath(row, 'residue_plddts_by_chain'),
    readObjectPath(row, 'chain_residue_plddt'),
    readObjectPath(row, 'chain_plddt'),
    readObjectPath(row, 'chain_plddts'),
    readObjectPath(row, 'confidence.residue_plddt_by_chain'),
    readObjectPath(row, 'confidence.residuePlddtByChain'),
    readObjectPath(row, 'confidence.residue_plddts_by_chain'),
    readObjectPath(row, 'confidence.chain_residue_plddt'),
    readObjectPath(row, 'confidence.chain_plddt'),
    readObjectPath(row, 'confidence.chain_plddts')
  ];
  for (const candidate of byChainCandidates) {
    const values = readResidueSeriesByChain(candidate, sequenceLength, preferredChainId);
    if (values.length >= Math.min(sequenceLength, 4)) return values;
  }
  return null;
}

function readPeptideBestCandidatePreview(task: ProjectTask): PeptideBestCandidatePreview | null {
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;

  if (confidence) {
    const peptideDesign =
      confidence.peptide_design && typeof confidence.peptide_design === 'object' && !Array.isArray(confidence.peptide_design)
        ? (confidence.peptide_design as Record<string, unknown>)
        : {};
    const peptideProgress =
      peptideDesign.progress && typeof peptideDesign.progress === 'object' && !Array.isArray(peptideDesign.progress)
        ? (peptideDesign.progress as Record<string, unknown>)
        : {};
    const topProgress =
      confidence.progress && typeof confidence.progress === 'object' && !Array.isArray(confidence.progress)
        ? (confidence.progress as Record<string, unknown>)
        : {};

    const candidateRows = readFirstRecordArrayFromPayloadPaths(
      [confidence, peptideDesign, peptideProgress, topProgress],
      [
        'progress.current_best_sequences',
        'progress.best_sequences',
        'peptide_design.progress.current_best_sequences',
        'peptide_design.progress.best_sequences',
        'peptide_design.current_best_sequences',
        'current_best_sequences',
        'peptide_design.best_sequences',
        'best_sequences',
        'peptide_design.candidates',
        'candidates'
      ]
    );
    if (candidateRows.length > 0) {
      const rowOrder = new Map(candidateRows.map((row, index) => [row, index] as const));
      const sorted = [...candidateRows].sort((a, b) =>
        comparePeptideCandidateRows(a, b, rowOrder.get(a) ?? 0, rowOrder.get(b) ?? 0)
      );
      const best = sorted.find((row) => Boolean(readPeptideCandidateSequence(row))) || sorted[0];
      const sequence = best ? readPeptideCandidateSequence(best) : '';
      if (sequence) {
        const plddt = normalizePlddtScalar(
          readFirstFiniteFromPayloadPaths([best], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt'])
        );
        const iptm = normalizeProbability(
          readFirstFiniteFromPayloadPaths([best], ['pair_iptm_target_binder', 'pair_iptm', 'iptm'])
        );
        const binderChainId = readFirstTextFromPayloadPaths(
          [best, confidence, peptideDesign, peptideProgress, topProgress],
          ['binder_chain_id', 'model_ligand_chain_id', 'requested_ligand_chain_id', 'ligand_chain_id']
        );
        const residuePlddts = readCandidateResiduePlddts(best, sequence.length, binderChainId);
        const modifications = readPeptideCandidateModifications(best, sequence.length);

        return {
          sequence,
          modifications,
          plddt,
          iptm,
          residuePlddts,
          binderChainId: binderChainId || null
        };
      }
    }
  }

  const peptidePreview = readPeptidePreviewFromProperties(task.properties);
  const previewBest = peptidePreview
    ? (() => {
        const best = readObjectPath(peptidePreview, 'best_candidate');
        return best && typeof best === 'object' && !Array.isArray(best) ? (best as Record<string, unknown>) : null;
      })()
    : null;
  if (!previewBest) return null;

  const sequence = readPeptideCandidateSequence(previewBest);
  if (!sequence) return null;
  const plddt = normalizePlddtScalar(
    readFirstFiniteFromPayloadPaths([previewBest], ['plddt', 'binder_avg_plddt', 'ligand_mean_plddt', 'mean_plddt'])
  );
  const iptm = normalizeProbability(
    readFirstFiniteFromPayloadPaths([previewBest], ['pair_iptm_target_binder', 'pair_iptm', 'iptm'])
  );
  const binderChainId = readFirstTextFromPayloadPaths(
    [previewBest, peptidePreview || {}],
    ['binder_chain_id', 'model_ligand_chain_id', 'requested_ligand_chain_id', 'ligand_chain_id']
  );
  const residuePlddts = readCandidateResiduePlddts(previewBest, sequence.length, binderChainId);
  const modifications = readPeptideCandidateModifications(previewBest, sequence.length);
  return {
    sequence,
    modifications,
    plddt,
    iptm,
    residuePlddts,
    binderChainId: binderChainId || null
  };
}

function readLeadOptVariableItems(leadOptMmp: Record<string, unknown>): Array<Record<string, unknown>> {
  const selection = readObjectPath(leadOptMmp, 'selection');
  if (selection && typeof selection === 'object' && !Array.isArray(selection)) {
    const selectedItems = readObjectPath(selection as Record<string, unknown>, 'variable_items');
    if (Array.isArray(selectedItems)) {
      return selectedItems
        .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
        .map((item) => item as Record<string, unknown>);
    }
  }
  const queryPayload = readObjectPath(leadOptMmp, 'query_payload');
  if (queryPayload && typeof queryPayload === 'object' && !Array.isArray(queryPayload)) {
    const variableSpec = readObjectPath(queryPayload as Record<string, unknown>, 'variable_spec');
    if (variableSpec && typeof variableSpec === 'object' && !Array.isArray(variableSpec)) {
      const items = readObjectPath(variableSpec as Record<string, unknown>, 'items');
      if (Array.isArray(items)) {
        return items
          .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
          .map((item) => item as Record<string, unknown>);
      }
    }
  }
  return [];
}

function normalizeLeadOptTaskStage(value: string): string {
  const token = value.trim().toLowerCase();
  if (!token) return 'unknown';
  return token;
}

function readLeadOptTaskListMetaFromProperties(task: ProjectTask): Record<string, unknown> | null {
  const properties =
    task.properties && typeof task.properties === 'object' && !Array.isArray(task.properties)
      ? (task.properties as unknown as Record<string, unknown>)
      : null;
  if (!properties) return null;
  const meta = properties.lead_opt_list;
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    return meta as Record<string, unknown>;
  }
  return null;
}

function readLeadOptTaskStateMetaFromProperties(task: ProjectTask): Record<string, unknown> | null {
  const properties =
    task.properties && typeof task.properties === 'object' && !Array.isArray(task.properties)
      ? (task.properties as unknown as Record<string, unknown>)
      : null;
  if (!properties) return null;
  const meta = properties.lead_opt_state;
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    return meta as Record<string, unknown>;
  }
  return null;
}

function readLeadOptTaskSummary(task: ProjectTask): LeadOptTaskSummary | null {
  const leadOptListMeta = readLeadOptTaskListMetaFromProperties(task);
  const leadOptStateMeta = readLeadOptTaskStateMetaFromProperties(task);
  const leadOptConfidenceMeta =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? asRecord((task.confidence as Record<string, unknown>).lead_opt_mmp)
      : {};
  const leadOptMmp = (() => {
    if (!leadOptListMeta && !leadOptStateMeta && Object.keys(leadOptConfidenceMeta).length === 0) return null;
    const fromList = leadOptListMeta || {};
    const fromState = leadOptStateMeta || {};
    const fromConfidence = leadOptConfidenceMeta || {};
    return {
      ...fromConfidence,
      ...fromList,
      ...fromState,
      query_result:
        Object.keys(asRecord(fromList.query_result)).length > 0
          ? asRecord(fromList.query_result)
          : asRecord(fromConfidence.query_result),
      enumerated_candidates:
        Array.isArray(fromList.enumerated_candidates) && fromList.enumerated_candidates.length > 0
          ? fromList.enumerated_candidates
          : Array.isArray(fromConfidence.enumerated_candidates)
            ? fromConfidence.enumerated_candidates
            : [],
      prediction_summary: {
        ...asRecord(fromConfidence.prediction_summary),
        ...asRecord(fromList.prediction_summary),
        ...asRecord(fromState.prediction_summary),
      },
      prediction_by_smiles: {
        ...asRecord(fromConfidence.prediction_by_smiles),
        ...asRecord(fromList.prediction_by_smiles),
        ...asRecord(fromState.prediction_by_smiles),
      },
      reference_prediction_by_backend: {
        ...asRecord(fromConfidence.reference_prediction_by_backend),
        ...asRecord(fromList.reference_prediction_by_backend),
        ...asRecord(fromState.reference_prediction_by_backend),
      },
    } as Record<string, unknown>;
  })();
  if (!leadOptMmp) return null;
  const queryResultRaw = readObjectPath(leadOptMmp, 'query_result');
  const queryResult =
    queryResultRaw && typeof queryResultRaw === 'object' && !Array.isArray(queryResultRaw)
      ? (queryResultRaw as Record<string, unknown>)
      : {};

  const stage = normalizeLeadOptTaskStage(
    String(
      leadOptMmp.prediction_stage ||
      leadOptMmp.stage ||
      leadOptMmp.prediction_state ||
      ''
    )
  );
  const predictionSummary =
    leadOptMmp.prediction_summary && typeof leadOptMmp.prediction_summary === 'object' && !Array.isArray(leadOptMmp.prediction_summary)
      ? (leadOptMmp.prediction_summary as Record<string, unknown>)
      : {};
  const predictionMap =
    leadOptMmp.prediction_by_smiles && typeof leadOptMmp.prediction_by_smiles === 'object' && !Array.isArray(leadOptMmp.prediction_by_smiles)
      ? (leadOptMmp.prediction_by_smiles as Record<string, unknown>)
      : {};
  const predictionMapCounts = (() => {
    const entries = Object.values(predictionMap);
    if (entries.length === 0) {
      return {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      };
    }
    let queued = 0;
    let running = 0;
    let success = 0;
    let failure = 0;
    for (const item of entries) {
      const state = String(asRecord(item).state || '').trim().toUpperCase();
      if (state === 'RUNNING') running += 1;
      else if (state === 'SUCCESS') success += 1;
      else if (state === 'FAILURE') failure += 1;
      else queued += 1;
    }
    return {
      total: entries.length,
      queued,
      running,
      success,
      failure
    };
  })();
  const hasPredictionMapCounts = predictionMapCounts.total > 0;
  const bucketCountFromSummary = readFiniteNumber(predictionSummary.total);
  const bucketCount = bucketCountFromSummary !== null ? Math.max(0, Math.floor(bucketCountFromSummary)) : predictionMapCounts.total;
  const predictionQueued = (() => {
    if (hasPredictionMapCounts) return predictionMapCounts.queued;
    const value = readFiniteNumber(predictionSummary.queued);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionRunning = (() => {
    if (hasPredictionMapCounts) return predictionMapCounts.running;
    const value = readFiniteNumber(predictionSummary.running);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionSuccess = (() => {
    if (hasPredictionMapCounts) return predictionMapCounts.success;
    const value = readFiniteNumber(predictionSummary.success);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionFailure = (() => {
    if (hasPredictionMapCounts) return predictionMapCounts.failure;
    const value = readFiniteNumber(predictionSummary.failure);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionTotal = (() => {
    if (hasPredictionMapCounts) return predictionMapCounts.total;
    const value = readFiniteNumber(predictionSummary.total);
    if (value !== null) return Math.max(0, Math.floor(value));
    if (
      predictionQueued !== null ||
      predictionRunning !== null ||
      predictionSuccess !== null ||
      predictionFailure !== null
    ) {
      return (predictionQueued || 0) + (predictionRunning || 0) + (predictionSuccess || 0) + (predictionFailure || 0);
    }
    return null;
  })();
  const transformCount = readFiniteNumber(leadOptMmp.transform_count);
  const candidateCount = readFiniteNumber(leadOptMmp.candidate_count);
  const selectedFragmentIds = (() => {
    const selectionIds = readStringArray(
      readObjectPath(leadOptMmp, 'selection.selected_fragment_ids') ?? leadOptMmp.selected_fragment_ids
    );
    if (selectionIds.length > 0) return Array.from(new Set(selectionIds));
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    return Array.from(
      new Set(
        variableItems
          .map((item) => String(item.fragment_id || '').trim())
          .filter(Boolean)
      )
    );
  })();
  const selectedAtomIndices = (() => {
    const selectionAtomsRaw =
      readObjectPath(leadOptMmp, 'selection.selected_fragment_atom_indices') ?? leadOptMmp.selected_fragment_atom_indices;
    if (Array.isArray(selectionAtomsRaw)) {
      return Array.from(
        new Set(
          selectionAtomsRaw
            .map((value) => Number(value))
            .filter((value) => Number.isFinite(value) && value >= 0)
            .map((value) => Math.floor(value))
        )
      );
    }
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    return Array.from(
      new Set(
        variableItems.flatMap((item) => {
          if (!Array.isArray(item.atom_indices)) return [];
          return item.atom_indices
            .map((value) => Number(value))
            .filter((value) => Number.isFinite(value) && value >= 0)
            .map((value) => Math.floor(value));
        })
      )
    );
  })();
  const selectedFragmentQuery = (() => {
    const selectionQueries = readStringArray(
      readObjectPath(leadOptMmp, 'selection.variable_queries') ?? leadOptMmp.variable_queries
    );
    if (selectionQueries.length > 0) return selectionQueries[0];
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    for (const item of variableItems) {
      const query = String(item.query || '').trim();
      if (query) return query;
    }
    return String(leadOptMmp.selected_fragment_query || '').trim();
  })();

  const stageLabel = (() => {
    if (stage === 'prediction_running' || stage === 'running') return 'Running';
    if (stage === 'prediction_completed' || stage === 'completed') return 'Completed';
    if (stage === 'prediction_failed' || stage === 'failed') return 'Failed';
    if (stage === 'prediction_queued' || stage === 'queued') return 'Queued';
    if (stage === 'idle') return 'Idle';
    return stage ? stage.replace(/_/g, ' ') : 'Unknown';
  })();

  const summary = (() => {
    const parts: string[] = [stageLabel];
    if (transformCount !== null) parts.push(`${Math.max(0, Math.floor(transformCount))} transforms`);
    if (candidateCount !== null) parts.push(`${Math.max(0, Math.floor(candidateCount))} candidates`);
    if (bucketCount > 0) parts.push(`${bucketCount} buckets`);
    if (predictionTotal !== null && predictionTotal > 0) {
      parts.push(
        `q${predictionQueued || 0}/r${predictionRunning || 0}/s${predictionSuccess || 0}/f${predictionFailure || 0}`
      );
    }
    return parts.join(' · ');
  })();
  const databaseId = String(
    leadOptMmp.mmp_database_id || queryResult.mmp_database_id || ''
  ).trim();
  const databaseSchema = String(
    leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema || ''
  ).trim();
  const databaseLabel = String(
    leadOptMmp.mmp_database_label || queryResult.mmp_database_label || databaseSchema || databaseId || ''
  ).trim();

  return {
    stage,
    summary,
    databaseId,
    databaseLabel,
    databaseSchema,
    transformCount: transformCount === null ? null : Math.max(0, Math.floor(transformCount)),
    candidateCount: candidateCount === null ? null : Math.max(0, Math.floor(candidateCount)),
    bucketCount,
    predictionTotal,
    predictionQueued,
    predictionRunning,
    predictionSuccess,
    predictionFailure,
    selectedFragmentIds,
    selectedAtomIndices,
    selectedFragmentQuery
  };
}

function hasLeadOptPredictionRuntime(task: ProjectTask): boolean {
  const properties =
    task.properties && typeof task.properties === 'object' && !Array.isArray(task.properties)
      ? (task.properties as unknown as Record<string, unknown>)
      : null;
  if (!properties) return false;
  const stateMeta =
    properties.lead_opt_state && typeof properties.lead_opt_state === 'object' && !Array.isArray(properties.lead_opt_state)
      ? (properties.lead_opt_state as Record<string, unknown>)
      : null;
  if (!stateMeta) return false;
  const predictionMap =
    stateMeta.prediction_by_smiles &&
    typeof stateMeta.prediction_by_smiles === 'object' &&
    !Array.isArray(stateMeta.prediction_by_smiles)
      ? (stateMeta.prediction_by_smiles as Record<string, unknown>)
      : {};
  const confidencePredictionMap =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? asRecord(asRecord((task.confidence as Record<string, unknown>).lead_opt_mmp).prediction_by_smiles)
      : {};
  const mergedPredictionMap = {
    ...confidencePredictionMap,
    ...predictionMap
  };
  for (const item of Object.values(mergedPredictionMap)) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const state = String((item as Record<string, unknown>).state || '').trim().toUpperCase();
    if (state === 'QUEUED' || state === 'RUNNING') return true;
  }
  return false;
}

export type {
  PeptideTaskSummary,
  LeadOptTaskSummary,
  PeptideBestCandidatePreview
};

export {
  readFiniteNumber,
  readStringArray,
  readFirstFiniteFromPayloadPaths,
  readFirstTextFromPayloadPaths,
  readFirstRecordArrayFromPayloadPaths,
  normalizePeptideDesignMode,
  normalizeNonNegativeInteger,
  readPeptideTaskCandidateCount,
  readPeptideTaskSummary,
  normalizePeptideCandidateSequence,
  normalizePlddtScalar,
  readPeptideCandidateSequence,
  readPeptideCandidateModifications,
  comparePeptideCandidateRows,
  alignResidueSeriesToSequence,
  readResidueSeriesByChain,
  readCandidateResiduePlddts,
  readPeptideBestCandidatePreview,
  readLeadOptVariableItems,
  normalizeLeadOptTaskStage,
  readLeadOptTaskListMetaFromProperties,
  readLeadOptTaskStateMetaFromProperties,
  readLeadOptTaskSummary,
  hasLeadOptPredictionRuntime
};
