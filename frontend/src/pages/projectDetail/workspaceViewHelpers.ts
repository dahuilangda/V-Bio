import type { InputComponent, PredictionConstraint, ProjectTask } from '../../types/models';
import {
  buildLeadOptPredictionRecordKey,
  parseLeadOptPredictionRecordKey,
  type LeadOptPredictionRecord
} from '../../components/project/leadopt/hooks/leadOptPredictionHelpers';
import { readLeadOptTaskSummary } from '../projectTasks/taskDataUtils';

export function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

export function hasExplicitPeptideResiduePool(task: ProjectTask | null | undefined): boolean {
  const properties = asRecord(task?.properties);
  const options = asRecord(properties.__vbio_input_options_v1);
  return Array.isArray(options.peptideResiduePool) || Array.isArray(options.peptide_residue_pool);
}



export function readNestedObjectPath(payload: Record<string, unknown>, path: string): unknown {
  let current: unknown = payload;
  for (const token of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[token];
  }
  return current;
}

export function readFirstRecordArrayFromObjectPaths(payloads: Record<string, unknown>[], paths: string[]): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = asRecordArray(readNestedObjectPath(payload, path));
      if (rows.length > 0) return rows;
    }
  }
  return [];
}

export function summarizePeptideDesignCandidates(confidence: Record<string, unknown>) {
  const peptide = asRecord(confidence.peptide_design);
  const rows = readFirstRecordArrayFromObjectPaths(
    [confidence, peptide, asRecord(peptide.progress), asRecord(confidence.progress)],
    [
      'peptide_design.best_sequences',
      'best_sequences',
      'current_best_sequences',
      'progress.best_sequences',
      'progress.current_best_sequences',
      'candidates'
    ]
  );
  return {
    count: rows.length,
    top: rows.slice(0, 5).map((row, index) => ({
      index,
      rank: toFiniteNumber(row.rank ?? row.ranking ?? row.order),
      generation: toFiniteNumber(row.generation ?? row.iteration ?? row.iter),
      score: toFiniteNumber(row.score ?? row.composite_score ?? row.fitness ?? row.objective),
      plddt: normalizePlddtMetric(row.plddt ?? row.binder_avg_plddt ?? row.ligand_mean_plddt ?? row.mean_plddt),
      interfaceMetric: toFiniteNumber(row.ligand_ipsae_max ?? row.ipsae_dom ?? row.pair_iptm ?? row.pairIptm ?? row.iptm),
      sequenceLength: readText(row.peptide_sequence ?? row.binder_sequence ?? row.candidate_sequence ?? row.designed_sequence ?? row.sequence).trim().length,
      structureName: readText(row.structureName ?? row.structure_name ?? row.structure_file ?? row.structure_path).trim()
    }))
  };
}

export function summarizeCopilotComponents(components: InputComponent[] | null | undefined) {
  if (!Array.isArray(components)) return [];
  return components.map((component, index) => ({
    index,
    id: readText(component.id).trim(),
    type: readText(component.type).trim(),
    sequenceLength: readText(component.sequence).trim().length,
    numCopies: component.numCopies,
    inputMethod: component.inputMethod || undefined,
    useMsa: component.useMsa,
    cyclic: component.cyclic,
    modificationsCount: Array.isArray(component.modifications) ? component.modifications.length : 0
  }));
}

export function summarizeCopilotConstraints(constraints: PredictionConstraint[] | null | undefined) {
  if (!Array.isArray(constraints)) return [];
  return constraints.map((constraint, index) => ({
    index,
    id: readText((constraint as { id?: string }).id).trim(),
    type: readText((constraint as { type?: string }).type).trim()
  }));
}

export function summarizeCopilotTask(task: ProjectTask | null | undefined) {
  if (!task) return null;
  const confidence = asRecord(task.confidence);
  const affinity = asRecord(task.affinity);
  return {
    id: readText(task.id).trim(),
    project_id: readText(task.project_id).trim(),
    name: readText(task.name).trim(),
    summary: readText(task.summary).trim(),
    task_id: readText(task.task_id).trim(),
    task_state: readText(task.task_state).trim(),
    status_text: readText(task.status_text).trim(),
    error_text: readText(task.error_text).trim(),
    backend: readText(task.backend).trim(),
    seed: task.seed,
    structure_name: readText(task.structure_name).trim(),
    submitted_at: task.submitted_at,
    completed_at: task.completed_at,
    duration_seconds: task.duration_seconds,
    components: summarizeCopilotComponents(task.components),
    constraints: summarizeCopilotConstraints(task.constraints),
    properties: summarizeCopilotTaskProperties(task.properties),
    // Surface the ACTUAL metric values (not just key names) so the model can quote confidence /
    // affinity numbers in an analysis. Scalars are kept verbatim; nested objects/arrays are kept as
    // keys only (to bound payload size) — the scalar top-level fields (avgPlddt, iptm, pae, etc.)
    // are the values a "analyze this task" answer needs.
    affinitySummary: summarizeMetricValues(affinity),
    confidenceSummary: {
      ...summarizeMetricValues(confidence),
      peptideDesign: summarizePeptideDesignCandidates(confidence)
    }
  };
}

/**
 * Bounded projection of task.properties for the Copilot context. task.properties is host state,
 * but for lead-optimization tasks it embeds the FULL MMP result snapshot (hundreds of enumerated
 * candidates plus a per-candidate prediction map) — passing it raw made the page's context exceed
 * the model budget and the Copilot could not converse at all. Scalars and short strings pass
 * through; lead-opt keys get purpose-built summaries (stages, counts, selection, ranked head
 * samples); every other nested object reduces to its key names / counts. The full tables stay on
 * the page — the Copilot quotes the sample and the counts, never a machine-clipped tail.
 */
export function summarizeCopilotTaskProperties(value: unknown): Record<string, unknown> {
  const properties = asRecord(value);
  if (Object.keys(properties).length === 0) return {};
  const out: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(properties)) {
    if (entry === null || entry === undefined) continue;
    if (typeof entry === 'number' || typeof entry === 'boolean') {
      out[key] = entry;
    } else if (typeof entry === 'string') {
      const text = entry.trim();
      if (text) out[key] = text.slice(0, 200);
    } else if (key === 'lead_opt_list') {
      out[key] = summarizeLeadOptListForCopilot(entry);
    } else if (key === 'lead_opt_state') {
      out[key] = summarizeLeadOptStateForCopilot(entry);
    } else if (Array.isArray(entry)) {
      out[key] = { count: entry.length };
    } else {
      out[key] = { keys: Object.keys(asRecord(entry)).slice(0, 12) };
    }
  }
  return out;
}

function summarizeLeadOptListForCopilot(value: unknown): Record<string, unknown> {
  const listMeta = asRecord(value);
  const queryResult = asRecord(listMeta.query_result);
  const candidates = Array.isArray(listMeta.enumerated_candidates)
    ? listMeta.enumerated_candidates
    : [];
  return {
    stage: readText(listMeta.stage).trim(),
    prediction_stage: readText(listMeta.prediction_stage).trim(),
    query_id: readText(listMeta.query_id || queryResult.query_id).trim(),
    task_id: readText(listMeta.task_id || queryResult.task_id).trim(),
    transform_count: toFiniteNumber(listMeta.transform_count),
    candidate_count: toFiniteNumber(listMeta.candidate_count) ?? candidates.length,
    bucket_count: toFiniteNumber(listMeta.bucket_count),
    mmp_database_id: readText(listMeta.mmp_database_id).trim(),
    mmp_database_label: readText(listMeta.mmp_database_label).trim(),
    target_chain: readText(listMeta.target_chain).trim(),
    ligand_chain: readText(listMeta.ligand_chain).trim(),
    selection: asRecord(listMeta.selection),
    query_result: {
      query_mode: readText(queryResult.query_mode).trim(),
      aggregation_type: readText(queryResult.aggregation_type).trim(),
      count: toFiniteNumber(queryResult.count),
      global_count: toFiniteNumber(queryResult.global_count),
      min_pairs: toFiniteNumber(queryResult.min_pairs)
    },
    enumerated_candidates: {
      count: candidates.length,
      top: candidates.slice(0, 8).map((item) => summarizeLeadOptCandidateRowForCopilot(item))
    }
  };
}

function summarizeLeadOptCandidateRowForCopilot(value: unknown): Record<string, unknown> {
  const row = asRecord(value);
  const properties = asRecord(row.properties);
  const deltas = asRecord(row.property_deltas);
  return {
    smiles: readText(row.smiles).trim(),
    ...(toFiniteNumber(row.n_pairs) === null ? {} : { n_pairs: toFiniteNumber(row.n_pairs) }),
    ...(toFiniteNumber(row.median_delta) === null ? {} : { median_delta: toFiniteNumber(row.median_delta) }),
    ...(Object.keys(properties).length > 0 ? { properties } : {}),
    ...(Object.keys(deltas).length > 0 ? { property_deltas: deltas } : {})
  };
}

function summarizeLeadOptStateForCopilot(value: unknown): Record<string, unknown> {
  const stateMeta = asRecord(value);
  const predictions = asPredictionRecordMap(stateMeta.prediction_by_smiles);
  const reference = asPredictionRecordMap(stateMeta.reference_prediction_by_backend);
  return {
    stage: readText(stateMeta.stage).trim(),
    prediction_stage: readText(stateMeta.prediction_stage).trim(),
    query_id: readText(stateMeta.query_id).trim(),
    task_id: readText(stateMeta.task_id).trim(),
    prediction_task_id: readText(stateMeta.prediction_task_id).trim(),
    prediction_candidate_smiles: readText(stateMeta.prediction_candidate_smiles).trim(),
    prediction_summary: asRecord(stateMeta.prediction_summary),
    ...(readText(stateMeta.selected_backend).trim()
      ? { selected_backend: readText(stateMeta.selected_backend).trim() }
      : {}),
    target_chain: readText(stateMeta.target_chain).trim(),
    ligand_chain: readText(stateMeta.ligand_chain).trim(),
    prediction_by_smiles: {
      ...summarizeLeadOptPredictions(predictions),
      top: rankLeadOptPredictionRecordsForCopilot(predictions, 8)
    },
    reference_prediction_by_backend: rankLeadOptPredictionRecordsForCopilot(reference, 5)
  };
}

function rankLeadOptPredictionRecordsForCopilot(
  records: Record<string, LeadOptPredictionRecord>,
  limit: number
): Array<Record<string, unknown>> {
  return Object.entries(records)
    .map(([key, record]) => {
      const parsed = parseLeadOptPredictionRecordKey(key);
      const interfaceMetric = toFiniteNumber(
        record.interfaceMetricValue ?? record.pairIptm
      );
      return { record, smiles: readText(parsed.smiles).trim(), interfaceMetric };
    })
    .filter(({ smiles, record }) => Boolean(smiles || readText(record.taskId).trim()))
    .sort((left, right) => {
      const leftDone = left.record.state === 'SUCCESS' ? 1 : 0;
      const rightDone = right.record.state === 'SUCCESS' ? 1 : 0;
      if (leftDone !== rightDone) return rightDone - leftDone;
      if (left.interfaceMetric !== null && right.interfaceMetric !== null) {
        return right.interfaceMetric - left.interfaceMetric;
      }
      if (left.interfaceMetric !== null) return -1;
      if (right.interfaceMetric !== null) return 1;
      return 0;
    })
    .slice(0, limit)
    .map(({ record, smiles }) => ({
      ...(smiles ? { smiles } : {}),
      backend: readText(record.backend).trim(),
      state: readText(record.state).trim(),
      ...(toFiniteNumber(record.pairIptm) === null ? {} : { pairIptm: toFiniteNumber(record.pairIptm) }),
      ...(toFiniteNumber(record.interfaceMetricValue) === null
        ? {}
        : {
            interfaceMetric: toFiniteNumber(record.interfaceMetricValue),
            interfaceMetricLabel: readText(record.interfaceMetricLabel).trim()
          }),
      ...(toFiniteNumber(record.ligandPlddt) === null ? {} : { ligandPlddt: toFiniteNumber(record.ligandPlddt) }),
      ...(toFiniteNumber(record.pairPae) === null ? {} : { pairPae: toFiniteNumber(record.pairPae) }),
      taskId: readText(record.taskId).trim(),
      error: readText(record.error).trim().slice(0, 160)
    }));
}

/**
 * Project a metrics object (confidence / affinity) into a bounded summary that keeps scalar VALUES
 * (numbers, booleans, short strings) alongside their keys, so the model can quote real metric
 * numbers in an analysis answer. Nested objects and arrays are reduced to their key names only to
 * bound the payload size — the scalar top-level fields carry the load-bearing numbers (avgPlddt,
 * iptm, ipTM, pAE, affinity scores, etc.).
 */
function summarizeMetricValues(metrics: Record<string, unknown>, maxEntries = 32): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  let count = 0;
  for (const [key, value] of Object.entries(metrics)) {
    if (count >= maxEntries) break;
    if (value === null || value === undefined) continue;
    if (typeof value === 'number' || typeof value === 'boolean') {
      out[key] = value;
      count += 1;
    } else if (typeof value === 'string' && value.length <= 120) {
      out[key] = value;
      count += 1;
    } else if (typeof value === 'object') {
      // Nested object/array: keep its keys so the model knows the structure, but not the full tree
      // (could be large residue-level arrays). One entry for the key map.
      const nested = value as Record<string, unknown>;
      const keys = Array.isArray(nested) ? undefined : Object.keys(nested).slice(0, 12);
      out[key] = keys ? { keys } : { count: Array.isArray(nested) ? nested.length : 0 };
      count += 1;
    }
  }
  return out;
}

export function normalizeCopilotPrefillComponents(value: unknown): InputComponent[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item, index) => {
      const row = asRecord(item);
      let componentType = readText(row.type).trim().toLowerCase();
      componentType = componentType.replace(/[-\s]+/g, '_');
      if (componentType === 'peptide' || componentType === 'polypeptide') componentType = 'protein';
      if (
        componentType === 'small_molecule' ||
        componentType === 'smallmolecule' ||
        componentType === 'molecule' ||
        componentType === 'compound' ||
        componentType === 'drug' ||
        componentType === 'smiles' ||
        componentType === 'ccd'
      ) {
        componentType = 'ligand';
      }
      if (componentType !== 'protein' && componentType !== 'ligand' && componentType !== 'dna' && componentType !== 'rna') return null;
      const rawSequence =
        readText(row.sequence).trim() ||
        readText(row.value).trim() ||
        readText(row.input).trim() ||
        (componentType === 'ligand'
          ? readText(row.smiles).trim() || readText(row.ccd).trim() || readText(row.ligand).trim()
          : '');
      const sequence = componentType === 'ligand' ? rawSequence : rawSequence.replace(/\s+/g, '').toUpperCase();
      if (!sequence) return null;
      const component: InputComponent = {
        id: `copilot-${componentType}-${index + 1}`,
        type: componentType as InputComponent['type'],
        sequence,
        numCopies: Math.max(1, Math.floor(Number(row.numCopies) || 1)),
      };
      if (componentType === 'protein') {
        component.useMsa = row.useMsa === false ? false : true;
      }
      if (componentType === 'ligand') {
        component.inputMethod = readText(row.inputMethod).trim() === 'ccd' ? 'ccd' : 'smiles';
      }
      return component;
    })
    .filter((component): component is InputComponent => Boolean(component));
}

export function normalizeCopilotComponentPartial(value: unknown): Partial<InputComponent> {
  const row = asRecord(value);
  const patch: Partial<InputComponent> = {};
  let componentType = readText(row.type).trim().toLowerCase().replace(/[-\s]+/g, '_');
  if (componentType === 'peptide' || componentType === 'polypeptide') componentType = 'protein';
  if (
    componentType === 'small_molecule' ||
    componentType === 'smallmolecule' ||
    componentType === 'molecule' ||
    componentType === 'compound' ||
    componentType === 'drug' ||
    componentType === 'smiles' ||
    componentType === 'ccd'
  ) {
    componentType = 'ligand';
  }
  if (componentType === 'protein' || componentType === 'ligand' || componentType === 'dna' || componentType === 'rna') {
    patch.type = componentType;
  }
  const rawSequence =
    readText(row.sequence).trim() ||
    readText(row.value).trim() ||
    readText(row.input).trim() ||
    readText(row.smiles).trim() ||
    readText(row.ccd).trim() ||
    readText(row.ligand).trim();
  if (rawSequence) {
    patch.sequence = patch.type === 'ligand' ? rawSequence : rawSequence.replace(/\s+/g, '').toUpperCase();
  }
  const copies = Math.floor(Number(row.numCopies));
  if (Number.isFinite(copies) && copies >= 1) patch.numCopies = copies;
  if (typeof row.useMsa === 'boolean') patch.useMsa = row.useMsa;
  const inputMethod = readText(row.inputMethod).trim();
  if (inputMethod === 'smiles' || inputMethod === 'ccd') patch.inputMethod = inputMethod;
  return patch;
}

export function findCopilotComponentIndex(components: InputComponent[], selectorValue: unknown): number {
  const selector = asRecord(selectorValue);
  const index = Math.floor(Number(selector.index));
  if (Number.isFinite(index) && index >= 1 && index <= components.length) return index - 1;
  const id = readText(selector.id).trim();
  if (id) {
    const byId = components.findIndex((component) => component.id === id);
    if (byId >= 0) return byId;
  }
  const type = readText(selector.type).trim().toLowerCase();
  const sequenceContains = readText(selector.sequenceContains).trim();
  return components.findIndex((component) => {
    if (type && component.type !== type) return false;
    if (sequenceContains && !readText(component.sequence).includes(sequenceContains)) return false;
    return Boolean(type || sequenceContains);
  });
}

export function applyCopilotComponentPatchOperations(components: InputComponent[], operationsValue: unknown): InputComponent[] {
  if (!Array.isArray(operationsValue) || operationsValue.length === 0) return components;
  let next = [...components];
  let changed = false;
  operationsValue.slice(0, 16).forEach((operationValue) => {
    const operation = asRecord(operationValue);
    const op = readText(operation.op).trim().toLowerCase();
    if (op === 'append') {
      const [component] = normalizeCopilotPrefillComponents([operation.component]);
      if (component) {
        next = [...next, { ...component, id: component.id || `copilot-${component.type}-${next.length + 1}` }];
        changed = true;
      }
      return;
    }
    if (op === 'replace_all') {
      const componentsReplacement = normalizeCopilotPrefillComponents(operation.components);
      if (componentsReplacement.length > 0) {
        next = componentsReplacement;
        changed = true;
      }
      return;
    }
    const targetIndex = findCopilotComponentIndex(next, operation.selector);
    if (targetIndex < 0) return;
    if (op === 'remove') {
      next = next.filter((_, index) => index !== targetIndex);
      changed = true;
      return;
    }
    if (op === 'update') {
      const patch = normalizeCopilotComponentPartial(operation.component);
      if (Object.keys(patch).length === 0) return;
      next = next.map((component, index) => (index === targetIndex ? { ...component, ...patch } : component));
      changed = true;
    }
  });
  return changed && next.length > 0 ? next : components;
}

export function readFiniteNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function asPredictionRecordMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, LeadOptPredictionRecord>;
}

export function hasPersistedIpsaeMetric(value: unknown): boolean {
  const row = asRecord(value);
  return (
    toFiniteNumber(row.ligand_ipsae_max ?? row.ligandIpsaeMax) !== null ||
    toFiniteNumber(row.ipsae_dom ?? row.ipsaeDom) !== null
  );
}

export function normalizePredictionState(value: unknown): 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' {
  const token = readText(value).trim().toUpperCase();
  if (token === 'RUNNING' || token === 'SUCCESS' || token === 'FAILURE') return token;
  return 'QUEUED';
}

export function predictionStatePriority(state: unknown): number {
  const normalized = normalizePredictionState(state);
  if (normalized === 'QUEUED') return 1;
  if (normalized === 'RUNNING') return 2;
  return 3;
}

export function hasPredictionRecordMetrics(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  if (toFiniteNumber(record.interfaceMetricValue) !== null) return true;
  if (toFiniteNumber(record.pairIptm) !== null) return true;
  if (toFiniteNumber(record.pairPae) !== null) return true;
  if (toFiniteNumber(record.ligandPlddt) !== null) return true;
  return Array.isArray(record.ligandAtomPlddts) && record.ligandAtomPlddts.length > 0;
}

export function normalizePredictionInterfaceSource(value: unknown): 'ipsae' | 'iptm' | 'none' {
  const token = readText(value).trim().toLowerCase();
  if (token === 'ipsae' || token === 'iptm') return token;
  return 'none';
}

export function mergePredictionInterfaceMetric(
  primary: LeadOptPredictionRecord,
  secondary: LeadOptPredictionRecord
): Pick<LeadOptPredictionRecord, 'interfaceMetricValue' | 'interfaceMetricLabel' | 'interfaceMetricSource'> {
  const primarySource = normalizePredictionInterfaceSource(primary.interfaceMetricSource);
  const secondarySource = normalizePredictionInterfaceSource(secondary.interfaceMetricSource);
  const preferred = primarySource === 'ipsae' || secondarySource !== 'ipsae' ? primary : secondary;
  const fallback = preferred === primary ? secondary : primary;
  const preferredValue = toFiniteNumber(preferred.interfaceMetricValue);
  const fallbackValue = toFiniteNumber(fallback.interfaceMetricValue);
  const preferredSource = normalizePredictionInterfaceSource(preferred.interfaceMetricSource);
  const fallbackSource = normalizePredictionInterfaceSource(fallback.interfaceMetricSource);
  const mergedSource = preferredSource !== 'none' ? preferredSource : fallbackSource;
  const mergedLabel = mergedSource === 'iptm' ? 'ipTM' : 'IPSAE';
  return {
    interfaceMetricValue: preferredValue ?? fallbackValue,
    interfaceMetricLabel: mergedLabel,
    interfaceMetricSource: mergedSource
  };
}

export function hasPredictionRecordStructure(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  return readText(record.structureText).trim().length > 0;
}

export function hasExactPredictionRenderContract(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  const renderSmiles = readText(record.ligandRenderSmiles).trim();
  return renderSmiles.length > 0 && Array.isArray(record.ligandRenderAtomPlddts) && record.ligandRenderAtomPlddts.length > 0;
}

export function pickPredictionRenderContract(
  primary: LeadOptPredictionRecord | null | undefined,
  secondary: LeadOptPredictionRecord | null | undefined
): { ligandRenderSmiles: string; ligandRenderAtomPlddts: number[] } {
  if (hasExactPredictionRenderContract(primary)) {
    return {
      ligandRenderSmiles: readText(primary?.ligandRenderSmiles).trim(),
      ligandRenderAtomPlddts: Array.isArray(primary?.ligandRenderAtomPlddts) ? primary!.ligandRenderAtomPlddts : []
    };
  }
  if (hasExactPredictionRenderContract(secondary)) {
    return {
      ligandRenderSmiles: readText(secondary?.ligandRenderSmiles).trim(),
      ligandRenderAtomPlddts: Array.isArray(secondary?.ligandRenderAtomPlddts) ? secondary!.ligandRenderAtomPlddts : []
    };
  }
  return {
    ligandRenderSmiles: '',
    ligandRenderAtomPlddts: []
  };
}

export function choosePreferredPredictionRecord(
  left: LeadOptPredictionRecord,
  right: LeadOptPredictionRecord
): LeadOptPredictionRecord {
  const leftPriority = predictionStatePriority(left.state);
  const rightPriority = predictionStatePriority(right.state);
  if (leftPriority !== rightPriority) {
    return rightPriority > leftPriority ? right : left;
  }
  const leftMetrics = hasPredictionRecordMetrics(left) ? 1 : 0;
  const rightMetrics = hasPredictionRecordMetrics(right) ? 1 : 0;
  if (leftMetrics !== rightMetrics) {
    return rightMetrics > leftMetrics ? right : left;
  }
  const leftStructure = hasPredictionRecordStructure(left) ? 1 : 0;
  const rightStructure = hasPredictionRecordStructure(right) ? 1 : 0;
  if (leftStructure !== rightStructure) {
    return rightStructure > leftStructure ? right : left;
  }
  const leftTs = Number.isFinite(Number(left.updatedAt)) ? Number(left.updatedAt) : 0;
  const rightTs = Number.isFinite(Number(right.updatedAt)) ? Number(right.updatedAt) : 0;
  if (leftTs !== rightTs) {
    return rightTs > leftTs ? right : left;
  }
  return readText(right.error).trim() ? right : left;
}

export function mergePredictionRecordPair(
  fromConfidence: LeadOptPredictionRecord | null | undefined,
  fromProperties: LeadOptPredictionRecord | null | undefined
): LeadOptPredictionRecord | null {
  if (!fromConfidence && !fromProperties) return null;
  if (!fromConfidence) return fromProperties || null;
  if (!fromProperties) return fromConfidence;
  const primary = choosePreferredPredictionRecord(fromConfidence, fromProperties);
  const secondary = primary === fromConfidence ? fromProperties : fromConfidence;
  const renderContract = pickPredictionRenderContract(primary, secondary);
  const mergedInterfaceMetric = mergePredictionInterfaceMetric(primary, secondary);
  return {
    ...secondary,
    ...primary,
    taskId: readText(primary.taskId || secondary.taskId).trim(),
    state: normalizePredictionState(primary.state),
    backend: readText(primary.backend || secondary.backend).trim().toLowerCase(),
    pairIptm: toFiniteNumber(primary.pairIptm) ?? toFiniteNumber(secondary.pairIptm),
    interfaceMetricValue: mergedInterfaceMetric.interfaceMetricValue,
    interfaceMetricLabel: mergedInterfaceMetric.interfaceMetricLabel,
    interfaceMetricSource: mergedInterfaceMetric.interfaceMetricSource,
    pairPae: toFiniteNumber(primary.pairPae) ?? toFiniteNumber(secondary.pairPae),
    pairIptmResolved:
      primary.pairIptmResolved === true ||
      secondary.pairIptmResolved === true ||
      hasPredictionRecordMetrics(primary) ||
      hasPredictionRecordMetrics(secondary),
    ligandPlddt: toFiniteNumber(primary.ligandPlddt) ?? toFiniteNumber(secondary.ligandPlddt),
    ligandAtomPlddts: Array.isArray(primary.ligandAtomPlddts)
      ? primary.ligandAtomPlddts
      : Array.isArray(secondary.ligandAtomPlddts)
        ? secondary.ligandAtomPlddts
        : [],
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts,
    resultBundleHydrated: primary.resultBundleHydrated === true || secondary.resultBundleHydrated === true,
    updatedAt: Math.max(
      Number.isFinite(Number(primary.updatedAt)) ? Number(primary.updatedAt) : 0,
      Number.isFinite(Number(secondary.updatedAt)) ? Number(secondary.updatedAt) : 0
    )
  };
}

export function mergePredictionRecordMaps(
  confidenceInput: unknown,
  propertiesInput: unknown,
  preferredBackendInput?: unknown
): Record<string, LeadOptPredictionRecord> {
  const confidence = compactLeadOptPredictionMap(asPredictionRecordMap(confidenceInput), preferredBackendInput);
  const properties = compactLeadOptPredictionMap(asPredictionRecordMap(propertiesInput), preferredBackendInput);
  const merged: Record<string, LeadOptPredictionRecord> = {};
  const keys = new Set<string>([...Object.keys(confidence), ...Object.keys(properties)]);
  for (const key of keys) {
    const normalizedKey = readText(key).trim();
    if (!normalizedKey) continue;
    const mergedRecord = mergePredictionRecordPair(confidence[normalizedKey], properties[normalizedKey]);
    if (!mergedRecord) continue;
    merged[normalizedKey] = mergedRecord;
  }
  return merged;
}

export function summarizeLeadOptPredictions(records: Record<string, LeadOptPredictionRecord>) {
  let queued = 0;
  let running = 0;
  let success = 0;
  let failure = 0;
  for (const record of Object.values(records)) {
    const token = String(record.state || '').toUpperCase();
    if (token === 'QUEUED') queued += 1;
    else if (token === 'RUNNING') running += 1;
    else if (token === 'SUCCESS') success += 1;
    else if (token === 'FAILURE') failure += 1;
  }
  return {
    total: Object.keys(records).length,
    queued,
    running,
    success,
    failure
  };
}

export function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Array<Record<string, unknown>>;
}

export function toFiniteNumber(value: unknown): number | null {
  const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(numeric)) return null;
  return numeric;
}

export function normalizePlddtMetric(value: unknown): number | null {
  const numeric = toFiniteNumber(value);
  if (numeric === null) return null;
  const scaled = numeric >= 0 && numeric <= 1 ? numeric * 100 : numeric;
  if (!Number.isFinite(scaled)) return null;
  return Math.max(0, Math.min(100, scaled));
}

export function compactLigandAtomPlddts(values: unknown): number[] {
  if (!Array.isArray(values)) return [];
  const out: number[] = [];
  for (const item of values) {
    const normalized = normalizePlddtMetric(item);
    if (normalized === null) continue;
    out.push(Math.round(normalized * 100) / 100);
    if (out.length >= 256) break;
  }
  return out;
}

export function readBooleanToken(value: unknown): boolean | null {
  if (value === true) return true;
  if (value === false) return false;
  const token = readText(value).trim().toLowerCase();
  if (!token) return null;
  if (token === '1' || token === 'true' || token === 'yes' || token === 'on') return true;
  if (token === '0' || token === 'false' || token === 'no' || token === 'off') return false;
  return null;
}

export function normalizePredictionBackendStrict(value: unknown): string {
  const token = readText(value).trim().toLowerCase();
  if (token === 'boltz2') return 'boltz';
  if (token === 'nesso1' || token === 'nesso-1') return 'nesso';
  if (token === 'boltz' || token === 'alphafold3' || token === 'protenix' || token === 'nesso' || token === 'pocketxmol' || token === 'boltz2dock' || token === 'protenix2dock') return token;
  return '';
}

export function compactLeadOptPredictionRecord(value: LeadOptPredictionRecord): LeadOptPredictionRecord {
  const backend = normalizePredictionBackendStrict(value.backend);
  const renderContract = pickPredictionRenderContract(value, null);
  return {
    taskId: readText(value.taskId).trim(),
    state: value.state,
    backend,
    pairIptm: toFiniteNumber(value.pairIptm),
    interfaceMetricValue: toFiniteNumber(value.interfaceMetricValue),
    interfaceMetricLabel: value.interfaceMetricLabel === 'ipTM' ? 'ipTM' : 'IPSAE',
    interfaceMetricSource:
      value.interfaceMetricSource === 'ipsae' ? 'ipsae' : value.interfaceMetricSource === 'iptm' ? 'iptm' : 'none',
    pairPae: toFiniteNumber(value.pairPae),
    pairIptmResolved: value.pairIptmResolved === true,
    ligandPlddt: normalizePlddtMetric(value.ligandPlddt),
    ligandAtomPlddts: compactLigandAtomPlddts(value.ligandAtomPlddts),
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: compactLigandAtomPlddts(renderContract.ligandRenderAtomPlddts),
    structureText: '',
    structureFormat: readText(value.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: readText(value.structureName).trim(),
    resultBundleHydrated: value.resultBundleHydrated === true,
    error: readText(value.error),
    updatedAt: Number.isFinite(Number(value.updatedAt)) ? Number(value.updatedAt) : 0
  };
}

export function compactLeadOptPredictionMap(
  value: Record<string, LeadOptPredictionRecord>,
  _preferredBackendInput?: unknown
): Record<string, LeadOptPredictionRecord> {
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [rawKey, record] of Object.entries(value)) {
    const normalizedRawKey = readText(rawKey).trim();
    const parsedKey = parseLeadOptPredictionRecordKey(rawKey);
    const backendFromKey = normalizePredictionBackendStrict(parsedKey.backend);
    const backendFromRawKey = normalizePredictionBackendStrict(normalizedRawKey);
    const parsedSmiles = readText(parsedKey.smiles).trim();

    // Reference prediction map uses backend-only keys.
    if (!backendFromKey && backendFromRawKey && parsedSmiles.toLowerCase() === backendFromRawKey) {
      const compactRecord = compactLeadOptPredictionRecord({
        ...record,
        backend: backendFromRawKey
      });
      const merged = mergePredictionRecordPair(out[backendFromRawKey], compactRecord);
      if (!merged) continue;
      out[backendFromRawKey] = merged;
      continue;
    }

    if (!parsedSmiles) continue;
    // Candidate prediction map uses `backend::smiles` keys only.
    const backend = backendFromKey;
    if (!backend) continue;
    const key = buildLeadOptPredictionRecordKey(backend, parsedSmiles);
    if (!key) continue;
    const compactRecord = compactLeadOptPredictionRecord({
      ...record,
      backend
    });
    const merged = mergePredictionRecordPair(out[key], compactRecord);
    if (!merged) continue;
    out[key] = merged;
  }
  return out;
}

export function compactLeadOptEnumeratedCandidateRow(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const smiles = readText(row.smiles || row.candidate_smiles || row.predicted_smiles).trim();
  if (!smiles) return null;
  const nPairs = toFiniteNumber(row.n_pairs);
  const medianDelta = toFiniteNumber(row.median_delta);
  const propertiesRaw = asRecord(row.properties);
  const propertyDeltasRaw = asRecord(row.property_deltas);
  const properties: Record<string, unknown> = {};
  const propertyDeltas: Record<string, unknown> = {};
  const mw = toFiniteNumber(propertiesRaw.molecular_weight);
  const logp = toFiniteNumber(propertiesRaw.logp);
  const tpsa = toFiniteNumber(propertiesRaw.tpsa);
  const deltaMw = toFiniteNumber(propertyDeltasRaw.mw);
  const deltaLogp = toFiniteNumber(propertyDeltasRaw.logp);
  const deltaTpsa = toFiniteNumber(propertyDeltasRaw.tpsa);
  if (mw !== null) properties.molecular_weight = mw;
  if (logp !== null) properties.logp = logp;
  if (tpsa !== null) properties.tpsa = tpsa;
  if (deltaMw !== null) propertyDeltas.mw = deltaMw;
  if (deltaLogp !== null) propertyDeltas.logp = deltaLogp;
  if (deltaTpsa !== null) propertyDeltas.tpsa = deltaTpsa;
  const highlightAtomIndices = Array.isArray(row.final_highlight_atom_indices)
    ? Array.from(
        new Set(
          row.final_highlight_atom_indices
            .map((item) => Number(item))
            .filter((item) => Number.isFinite(item) && item >= 0)
            .map((item) => Math.floor(item))
        )
      )
    : [];
  const constantSmiles = readText(row.constant_smiles).trim();
  return {
    smiles,
    ...(nPairs === null ? {} : { n_pairs: nPairs }),
    ...(medianDelta === null ? {} : { median_delta: medianDelta }),
    properties,
    property_deltas: propertyDeltas,
    final_highlight_atom_indices: highlightAtomIndices,
    ...(constantSmiles ? { constant_smiles: constantSmiles } : {})
  };
}

export function readLeadOptPersistRecordUpdatedAt(value: unknown): number {
  const record = asRecord(value);
  const raw = record.updatedAt ?? record.updated_at;
  const numeric = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN;
  return Number.isFinite(numeric) ? numeric : 0;
}

export function mergeLeadOptPersistRecordMap(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const merged: Record<string, unknown> = { ...prev };
  for (const [key, nextRecord] of Object.entries(next)) {
    const prevRecord = merged[key];
    if (!prevRecord) {
      merged[key] = nextRecord;
      continue;
    }
    const nextUpdatedAt = readLeadOptPersistRecordUpdatedAt(nextRecord);
    const prevUpdatedAt = readLeadOptPersistRecordUpdatedAt(prevRecord);
    merged[key] = nextUpdatedAt >= prevUpdatedAt ? nextRecord : prevRecord;
  }
  return merged;
}

export function resolveLeadOptSnapshotFromTask(taskInput: unknown): Record<string, unknown> {
  const task = asRecord(taskInput);
  const properties = asRecord(task.properties);
  const fromProperties = asRecord(properties.lead_opt_list);
  const fromPropertiesState = asRecord(properties.lead_opt_state);
  const confidence = asRecord(task.confidence);
  const fromConfidence = asRecord(confidence.lead_opt_mmp);
  if (
    Object.keys(fromProperties).length === 0 &&
    Object.keys(fromPropertiesState).length === 0 &&
    Object.keys(fromConfidence).length === 0
  ) {
    return {};
  }

  const propertiesQueryResult = asRecord(fromProperties.query_result);
  const confidenceQueryResult = asRecord(fromConfidence.query_result);
  const propertiesSelection = asRecord(fromProperties.selection);
  const confidenceSelection = asRecord(fromConfidence.selection);
  const stateSelectedBackend = normalizePredictionBackendStrict(
    fromPropertiesState.selected_backend ?? fromConfidence.selected_backend
  );
  const preferredPredictionBackend = stateSelectedBackend;
  const mergedPredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(fromConfidence.prediction_by_smiles, fromProperties.prediction_by_smiles, preferredPredictionBackend),
    fromPropertiesState.prediction_by_smiles,
    preferredPredictionBackend
  );
  const mergedReferencePredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(
      fromConfidence.reference_prediction_by_backend,
      fromProperties.reference_prediction_by_backend,
      preferredPredictionBackend
    ),
    fromPropertiesState.reference_prediction_by_backend,
    preferredPredictionBackend
  );

  return {
    ...fromConfidence,
    ...fromProperties,
    query_result:
      Object.keys(propertiesQueryResult).length > 0
        ? propertiesQueryResult
        : confidenceQueryResult,
    enumerated_candidates:
      Array.isArray(fromProperties.enumerated_candidates) && fromProperties.enumerated_candidates.length > 0
        ? fromProperties.enumerated_candidates
        : Array.isArray(fromConfidence.enumerated_candidates)
          ? fromConfidence.enumerated_candidates
          : [],
    prediction_by_smiles: mergedPredictions,
    reference_prediction_by_backend: mergedReferencePredictions,
    ...(stateSelectedBackend ? { selected_backend: stateSelectedBackend } : {}),
    stage: readText(fromPropertiesState.stage).trim() || readText(fromProperties.stage).trim() || readText(fromConfidence.stage).trim(),
    prediction_stage:
      readText(fromPropertiesState.prediction_stage).trim() ||
      readText(fromProperties.prediction_stage).trim() ||
      readText(fromConfidence.prediction_stage).trim(),
    prediction_summary:
      Object.keys(asRecord(fromPropertiesState.prediction_summary)).length > 0
        ? asRecord(fromPropertiesState.prediction_summary)
        : Object.keys(asRecord(fromProperties.prediction_summary)).length > 0
          ? asRecord(fromProperties.prediction_summary)
          : asRecord(fromConfidence.prediction_summary),
    selection:
      Object.keys(propertiesSelection).length > 0
        ? propertiesSelection
        : confidenceSelection,
    ui_state: {},
    query_id:
      readText(fromProperties.query_id || propertiesQueryResult.query_id).trim() ||
      readText(fromPropertiesState.query_id).trim() ||
      readText(fromConfidence.query_id || confidenceQueryResult.query_id).trim(),
    task_id:
      readText(fromProperties.task_id || propertiesQueryResult.task_id).trim() ||
      readText(fromPropertiesState.task_id).trim() ||
      readText(fromConfidence.task_id || confidenceQueryResult.task_id).trim(),
    target_chain: readText(fromPropertiesState.target_chain || fromProperties.target_chain || fromConfidence.target_chain).trim(),
    ligand_chain: readText(fromPropertiesState.ligand_chain || fromProperties.ligand_chain || fromConfidence.ligand_chain).trim(),
    prediction_task_id:
      readText(fromPropertiesState.prediction_task_id).trim() ||
      readText(fromProperties.prediction_task_id).trim() ||
      readText(fromConfidence.prediction_task_id).trim(),
    prediction_candidate_smiles:
      readText(fromPropertiesState.prediction_candidate_smiles).trim() ||
      readText(fromProperties.prediction_candidate_smiles).trim() ||
      readText(fromConfidence.prediction_candidate_smiles).trim()
  };
}

export function readLeadOptTaskRowTimestamp(taskInput: unknown): number {
  const task = asRecord(taskInput);
  return new Date(
    readText(task.updated_at || task.completed_at || task.submitted_at || task.created_at).trim()
  ).getTime() || 0;
}

export function readLeadOptSnapshotPriority(taskInput: unknown): number {
  const task = taskInput as any;
  const snapshot = resolveLeadOptSnapshotFromTask(task);
  if (Object.keys(snapshot).length === 0) return -1;
  const summary = readLeadOptTaskSummary(task);
  if (!summary) return 0;
  const queued = Math.max(0, Number(summary.predictionQueued || 0));
  const running = Math.max(0, Number(summary.predictionRunning || 0));
  const success = Math.max(0, Number(summary.predictionSuccess || 0));
  const failure = Math.max(0, Number(summary.predictionFailure || 0));
  const stage = readText(summary.stage).trim().toLowerCase();
  const hasMaterializedQuery = Boolean(
    summary.transformCount !== null ||
    summary.candidateCount !== null ||
    summary.databaseId ||
    summary.databaseLabel ||
    summary.databaseSchema
  );
  if (success > 0 || failure > 0 || hasMaterializedQuery || stage === 'prediction_completed' || stage === 'completed') return 4;
  if (running > 0 || stage === 'prediction_running' || stage === 'running') return 3;
  if (queued > 0 || stage === 'prediction_queued' || stage === 'queued') return 2;
  return 1;
}

export function readLeadOptListPriority(taskInput: unknown): number {
  const task = taskInput as any;
  const snapshot = resolveLeadOptSnapshotFromTask(taskInput);
  if (Object.keys(snapshot).length === 0) return -1;
  const summary = readLeadOptTaskSummary(task);
  const queryResult = asRecord(snapshot.query_result);
  const queryId = readText(snapshot.query_id || queryResult.query_id).trim();
  const candidateCount = Math.max(
    Number(summary?.candidateCount ?? 0) || 0,
    Number(snapshot.candidate_count || 0) || 0,
    Array.isArray(snapshot.enumerated_candidates) ? snapshot.enumerated_candidates.length : 0
  );
  const transformCount = Math.max(
    Number(summary?.transformCount ?? 0) || 0,
    Number(snapshot.transform_count || 0) || 0,
    Array.isArray(queryResult.transforms) ? queryResult.transforms.length : 0
  );
  const bucketCount = Math.max(
    Number(summary?.bucketCount ?? 0) || 0,
    Number(snapshot.bucket_count || 0) || 0,
    Array.isArray(queryResult.clusters) ? queryResult.clusters.length : 0
  );
  if (queryId && candidateCount > 0) return 4;
  if (queryId && (transformCount > 0 || bucketCount > 0)) return 3;
  if (queryId) return 2;
  return 1;
}

export function hasLeadOptHaloSnapshot(taskInput: unknown): boolean {
  const halo = asRecord(asRecord(asRecord(taskInput).confidence).lead_opt_halo);
  return Array.isArray(halo.candidates) && halo.candidates.length > 0;
}

export function pickPreferredLeadOptTask(projectTasks: ProjectTask[]): ProjectTask | null {
  let preferred: ProjectTask | null = null;
  for (const row of projectTasks) {
    // HALO rows carry their results directly on confidence.lead_opt_halo and
    // never populate the legacy mmp snapshot fields.
    const halo = hasLeadOptHaloSnapshot(row);
    const snapshot = resolveLeadOptSnapshotFromTask(row);
    if (!halo && Object.keys(snapshot).length === 0) continue;
    if (!preferred) {
      preferred = row;
      continue;
    }
    if (halo && !hasLeadOptHaloSnapshot(preferred)) {
      preferred = row;
      continue;
    }
    const preferredPriority = readLeadOptListPriority(preferred);
    const candidatePriority = readLeadOptListPriority(row);
    if (candidatePriority > preferredPriority) {
      preferred = row;
      continue;
    }
    if (candidatePriority < preferredPriority) continue;
    if (readLeadOptTaskRowTimestamp(row) > readLeadOptTaskRowTimestamp(preferred)) {
      preferred = row;
    }
  }
  return preferred;
}

export function readLeadOptQueryIdFromSnapshot(snapshotInput: unknown): string {
  const snapshot = asRecord(snapshotInput);
  const queryResult = asRecord(snapshot.query_result);
  return readText(snapshot.query_id || queryResult.query_id).trim();
}
