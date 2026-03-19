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
  'current_best_sequences',
  'best_sequences',
  'candidates'
] as const;

const PEPTIDE_REQUEST_OPTION_KEYS = [
  'peptideDesignMode',
  'peptide_design_mode',
  'peptideBinderLength',
  'peptide_binder_length',
  'peptideIterations',
  'peptide_iterations',
  'peptidePopulationSize',
  'peptide_population_size',
  'peptideEliteSize',
  'peptide_elite_size',
  'peptideMutationRate',
  'peptide_mutation_rate'
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

  return merged;
}

export function derivePersistedResultConfidences(params: {
  parsedConfidenceValue: unknown;
  baseProjectConfidenceValue?: unknown;
  baseTaskConfidenceValue?: unknown;
}): {
  projectConfidence: Record<string, unknown>;
  taskConfidence: Record<string, unknown>;
  hasPeptidePayload: boolean;
} {
  const parsedConfidence = asRecord(params.parsedConfidenceValue);
  const persistedConfidenceFull = compactResultConfidenceForStorage(parsedConfidence, {
    preservePeptideCandidateStructureText: true
  });
  const persistedConfidenceCompact = compactResultConfidenceForStorage(parsedConfidence, {
    preservePeptideCandidateStructureText: false
  });
  const hasPeptidePayload = hasPeptideSummaryFields(persistedConfidenceFull);
  const persistedProjectConfidenceSource = hasPeptidePayload ? persistedConfidenceCompact : persistedConfidenceFull;
  const projectConfidence = hasPeptidePayload
    ? persistedProjectConfidenceSource
    : mergePeptideSummaryIntoParsedConfidence(
        persistedProjectConfidenceSource,
        asRecord(params.baseProjectConfidenceValue)
      );
  const taskConfidenceBase = asRecord(params.baseTaskConfidenceValue);
  const effectiveTaskBase =
    Object.keys(taskConfidenceBase).length > 0
      ? taskConfidenceBase
      : hasPeptidePayload
        ? null
        : asRecord(params.baseProjectConfidenceValue);
  const taskConfidence = mergePeptideSummaryIntoParsedConfidence(
    hasPeptidePayload ? persistedConfidenceCompact : persistedConfidenceFull,
    effectiveTaskBase
  );
  return {
    projectConfidence,
    taskConfidence,
    hasPeptidePayload
  };
}
