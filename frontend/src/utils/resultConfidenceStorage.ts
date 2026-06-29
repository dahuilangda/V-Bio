import { compactResultConfidenceForStorage } from '../api/backendResultParserApi';

const PEPTIDE_RUNTIME_SETUP_KEYS = [
  'design_mode',
  'mode',
  'binder_length',
  'length',
  'iterations',
  'population_size',
  'elite_size',
  'mutation_rate'
] as const;

const PEPTIDE_RUNTIME_PROGRESS_KEYS = [
  'current_generation',
  'generation',
  'total_generations',
  'completed_tasks',
  'pending_tasks',
  'total_tasks',
  'candidate_count',
  'best_score',
  'current_best_score',
  'progress_percent',
  'current_status',
  'status_stage',
  'stage',
  'status_message',
  'generation_total_tasks',
  'generation_completed_tasks',
  'generation_running_tasks',
  'generation_queued_tasks',
  'elapsed_seconds',
  'estimated_remaining_seconds',
  'estimated_completion_time',
  'candidates_evaluated',
  'adaptive_mutation_rate',
  'stagnant_generations',
  'current_best_sequences',
  'best_sequences',
  'candidates'
] as const;

const PEPTIDE_REQUEST_OPTION_KEYS = [
  'seed',
  'affinityMode',
  'peptideDesignMode',
  'peptide_design_mode',
  'peptideBinderLength',
  'peptide_binder_length',
  'peptideUseInitialSequence',
  'peptide_use_initial_sequence',
  'peptideInitialSequence',
  'peptide_initial_sequence',
  'peptideSequenceMask',
  'peptide_sequence_mask',
  'peptideIterations',
  'peptide_iterations',
  'peptidePopulationSize',
  'peptide_population_size',
  'peptideEliteSize',
  'peptide_elite_size',
  'peptideMutationRate',
  'peptide_mutation_rate',
  'peptideResiduePool',
  'peptide_residue_pool',
  'peptideCustomResidueDefinitions',
  'peptide_custom_residue_definitions',
  'peptideNonNaturalMin',
  'peptide_non_natural_min',
  'peptideNonNaturalMax',
  'peptide_non_natural_max',
  'peptideBicyclicLinkerCcd',
  'peptide_bicyclic_linker_ccd',
  'peptideBicyclicCysPositionMode',
  'peptide_bicyclic_cys_position_mode',
  'peptideBicyclicFixTerminalCys',
  'peptide_bicyclic_fix_terminal_cys',
  'peptideBicyclicIncludeExtraCys',
  'peptide_bicyclic_include_extra_cys',
  'peptideBicyclicCys1Pos',
  'peptide_bicyclic_cys1_pos',
  'peptideBicyclicCys2Pos',
  'peptide_bicyclic_cys2_pos',
  'peptideBicyclicCys3Pos',
  'peptide_bicyclic_cys3_pos'
] as const;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function hasMeaningfulValue(value: unknown): boolean {
  if (value === undefined || value === null) return false;
  if (typeof value === 'string') return value.trim().length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === 'object') return Object.keys(asRecord(value)).length > 0;
  return true;
}

function copyMissingFields(
  target: Record<string, unknown>,
  source: Record<string, unknown>,
  keys: readonly string[]
): boolean {
  let changed = false;
  for (const key of keys) {
    const sourceValue = source[key];
    if (!hasMeaningfulValue(sourceValue)) continue;
    if (hasMeaningfulValue(target[key])) continue;
    target[key] = sourceValue;
    changed = true;
  }
  return changed;
}

export function hasPeptideSummaryFields(value: Record<string, unknown>): boolean {
  if (Object.keys(asRecord(value.peptide_design)).length > 0) return true;
  if (Object.keys(asRecord(value.progress)).length > 0) return true;
  const requestOptions = asRecord(asRecord(value.request).options);
  return PEPTIDE_REQUEST_OPTION_KEYS.some((key) => hasMeaningfulValue(requestOptions[key]));
}

export function mergePeptideSummaryIntoParsedConfidence(
  parsedConfidenceValue: Record<string, unknown>,
  baseConfidenceValue: Record<string, unknown> | null | undefined
): Record<string, unknown> {
  const baseConfidence = asRecord(baseConfidenceValue);
  if (Object.keys(baseConfidence).length === 0 || !hasPeptideSummaryFields(baseConfidence)) {
    return parsedConfidenceValue;
  }

  const merged: Record<string, unknown> = { ...parsedConfidenceValue };

  const mergedRequest = asRecord(merged.request);
  const mergedRequestOptions = { ...asRecord(mergedRequest.options) };
  const baseRequestOptions = asRecord(asRecord(baseConfidence.request).options);
  const requestChanged = copyMissingFields(mergedRequestOptions, baseRequestOptions, PEPTIDE_REQUEST_OPTION_KEYS);
  if (requestChanged) {
    merged.request = {
      ...mergedRequest,
      options: mergedRequestOptions
    };
  }

  const mergedPeptide = { ...asRecord(merged.peptide_design) };
  const basePeptide = asRecord(baseConfidence.peptide_design);
  const peptideChangedBySetup = copyMissingFields(mergedPeptide, basePeptide, PEPTIDE_RUNTIME_SETUP_KEYS);
  const peptideChangedByProgress = copyMissingFields(mergedPeptide, basePeptide, PEPTIDE_RUNTIME_PROGRESS_KEYS);

  const mergedPeptideProgress = { ...asRecord(mergedPeptide.progress) };
  const basePeptideProgress = asRecord(basePeptide.progress);
  const baseTopProgress = asRecord(baseConfidence.progress);
  const peptideProgressChanged =
    copyMissingFields(mergedPeptideProgress, basePeptideProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS) ||
    copyMissingFields(mergedPeptideProgress, baseTopProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS);
  if (peptideProgressChanged) {
    mergedPeptide.progress = mergedPeptideProgress;
  }

  if (
    peptideChangedBySetup ||
    peptideChangedByProgress ||
    peptideProgressChanged ||
    (Object.keys(mergedPeptide).length === 0 && Object.keys(basePeptide).length > 0)
  ) {
    merged.peptide_design = mergedPeptide;
  }

  const mergedProgress = { ...asRecord(merged.progress) };
  const topProgressChanged =
    copyMissingFields(mergedProgress, baseTopProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS) ||
    copyMissingFields(mergedProgress, mergedPeptideProgress, PEPTIDE_RUNTIME_PROGRESS_KEYS);
  if (topProgressChanged) {
    merged.progress = mergedProgress;
  }

  if (!hasMeaningfulValue(merged.best_sequences) && hasMeaningfulValue(baseConfidence.best_sequences)) {
    merged.best_sequences = baseConfidence.best_sequences;
  }
  if (!hasMeaningfulValue(merged.current_best_sequences) && hasMeaningfulValue(baseConfidence.current_best_sequences)) {
    merged.current_best_sequences = baseConfidence.current_best_sequences;
  }

  return compactResultConfidenceForStorage(merged, {
    preservePeptideCandidateStructureText: false
  });
}

export function derivePersistedResultConfidences(params: {
  parsedConfidenceValue: unknown;
  baseProjectConfidenceValue?: unknown;
  baseTaskConfidenceValue?: unknown;
  baseTaskInputOptions?: unknown;
}): {
  projectConfidence: Record<string, unknown>;
  taskConfidence: Record<string, unknown>;
  hasPeptidePayload: boolean;
} {
  const parsedConfidence = asRecord(params.parsedConfidenceValue);
  const persistedConfidenceCompact = compactResultConfidenceForStorage(parsedConfidence, {
    preservePeptideCandidateStructureText: false
  });
  const hasPeptidePayload = hasPeptideSummaryFields(persistedConfidenceCompact);
  const baseTaskInputOptions = asRecord(params.baseTaskInputOptions);
  const inputOptionsConfidence = Object.keys(baseTaskInputOptions).length > 0
    ? { request: { options: baseTaskInputOptions } }
    : {};
  const persistedProjectConfidenceSource = persistedConfidenceCompact;
  const projectConfidence = mergePeptideSummaryIntoParsedConfidence(
    persistedProjectConfidenceSource,
    {
      ...inputOptionsConfidence,
      ...asRecord(params.baseProjectConfidenceValue),
      request: {
        ...asRecord(asRecord(params.baseProjectConfidenceValue).request),
        options: {
          ...asRecord(asRecord(asRecord(params.baseProjectConfidenceValue).request).options),
          ...baseTaskInputOptions
        }
      }
    }
  );
  const taskConfidenceBase = asRecord(params.baseTaskConfidenceValue);
  const effectiveTaskBaseSource =
    Object.keys(taskConfidenceBase).length > 0
      ? taskConfidenceBase
      : asRecord(params.baseProjectConfidenceValue);
  const effectiveTaskBase = {
    ...inputOptionsConfidence,
    ...effectiveTaskBaseSource,
    request: {
      ...asRecord(effectiveTaskBaseSource.request),
      options: {
        ...asRecord(asRecord(effectiveTaskBaseSource.request).options),
        ...baseTaskInputOptions
      }
    }
  };
  const taskConfidence = mergePeptideSummaryIntoParsedConfidence(
    persistedConfidenceCompact,
    effectiveTaskBase
  );
  return {
    projectConfidence,
    taskConfidence,
    hasPeptidePayload
  };
}
