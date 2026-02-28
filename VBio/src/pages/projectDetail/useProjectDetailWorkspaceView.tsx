import { useCallback, useRef, useState, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type { PredictionConstraint } from '../../types/models';
import { downloadResultFile } from '../../api/backendApi';
import { createInputComponent } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';
import { ProjectDetailLayout } from './ProjectDetailLayout';
import {
  computeUseMsaFlag,
  filterConstraintsByBackend,
} from './projectDraftUtils';
import { useProjectResultDisplay } from './useProjectResultDisplay';
import { useProjectRunHandlers } from './useProjectRunHandlers';
import {
  constraintLabel,
  formatConstraintCombo as formatConstraintComboForWorkspace,
  formatConstraintDetail as formatConstraintDetailForWorkspace
} from './constraintWorkspaceUtils';
import { useConstraintWorkspaceActions } from './useConstraintWorkspaceActions';
import { scrollToEditorBlock } from './editorActions';
import { useProjectEditorHandlers } from './useProjectEditorHandlers';
import { useProjectSidebarActions } from './useProjectSidebarActions';
import { useProjectWorkflowSectionProps } from './useProjectWorkflowSectionProps';
import { useProjectRunState } from './useProjectRunState';
import { usePredictionWorkspaceProps } from './usePredictionWorkspaceProps';
import { useProjectDetailRuntimeContext } from './useProjectDetailRuntimeContext';
import { buildLeadOptUploadSnapshotComponents, type LeadOptPersistedUploads } from './projectTaskSnapshot';
import { buildLeadOptCandidatesUiStateSignature, type LeadOptCandidatesUiState } from '../../components/project/leadopt/LeadOptCandidatesPanel';
import type { LeadOptPredictionRecord } from '../../components/project/leadopt/hooks/useLeadOptMmpQueryMachine';

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

function asPredictionRecordMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, LeadOptPredictionRecord>;
}

function normalizePredictionState(value: unknown): 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' {
  const token = readText(value).trim().toUpperCase();
  if (token === 'RUNNING' || token === 'SUCCESS' || token === 'FAILURE') return token;
  return 'QUEUED';
}

function predictionStatePriority(state: unknown): number {
  const normalized = normalizePredictionState(state);
  if (normalized === 'QUEUED') return 1;
  if (normalized === 'RUNNING') return 2;
  return 3;
}

function hasPredictionRecordMetrics(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  if (toFiniteNumber(record.pairIptm) !== null) return true;
  if (toFiniteNumber(record.pairPae) !== null) return true;
  if (toFiniteNumber(record.ligandPlddt) !== null) return true;
  return Array.isArray(record.ligandAtomPlddts) && record.ligandAtomPlddts.length > 0;
}

function hasPredictionRecordStructure(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  return readText(record.structureText).trim().length > 0;
}

function choosePreferredPredictionRecord(
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

function mergePredictionRecordPair(
  fromConfidence: LeadOptPredictionRecord | null | undefined,
  fromProperties: LeadOptPredictionRecord | null | undefined
): LeadOptPredictionRecord | null {
  if (!fromConfidence && !fromProperties) return null;
  if (!fromConfidence) return fromProperties || null;
  if (!fromProperties) return fromConfidence;
  const primary = choosePreferredPredictionRecord(fromConfidence, fromProperties);
  const secondary = primary === fromConfidence ? fromProperties : fromConfidence;
  return {
    ...secondary,
    ...primary,
    taskId: readText(primary.taskId || secondary.taskId).trim(),
    state: normalizePredictionState(primary.state),
    backend: readText(primary.backend || secondary.backend).trim().toLowerCase() || 'boltz',
    pairIptm: toFiniteNumber(primary.pairIptm) ?? toFiniteNumber(secondary.pairIptm),
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
    updatedAt: Math.max(
      Number.isFinite(Number(primary.updatedAt)) ? Number(primary.updatedAt) : 0,
      Number.isFinite(Number(secondary.updatedAt)) ? Number(secondary.updatedAt) : 0,
      Date.now()
    )
  };
}

function mergePredictionRecordMaps(
  confidenceInput: unknown,
  propertiesInput: unknown
): Record<string, LeadOptPredictionRecord> {
  const confidence = asPredictionRecordMap(confidenceInput);
  const properties = asPredictionRecordMap(propertiesInput);
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

function summarizeLeadOptPredictions(records: Record<string, LeadOptPredictionRecord>) {
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

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Array<Record<string, unknown>>;
}

function toFiniteNumber(value: unknown): number | null {
  const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(numeric)) return null;
  return numeric;
}

function normalizePlddtMetric(value: unknown): number | null {
  const numeric = toFiniteNumber(value);
  if (numeric === null) return null;
  const scaled = numeric >= 0 && numeric <= 1 ? numeric * 100 : numeric;
  if (!Number.isFinite(scaled)) return null;
  return Math.max(0, Math.min(100, scaled));
}

function compactLigandAtomPlddts(values: unknown): number[] {
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

function readBooleanToken(value: unknown): boolean | null {
  if (value === true) return true;
  if (value === false) return false;
  const token = readText(value).trim().toLowerCase();
  if (!token) return null;
  if (token === '1' || token === 'true' || token === 'yes' || token === 'on') return true;
  if (token === '0' || token === 'false' || token === 'no' || token === 'off') return false;
  return null;
}

function compactLeadOptPredictionRecord(value: LeadOptPredictionRecord): LeadOptPredictionRecord {
  return {
    taskId: readText(value.taskId).trim(),
    state: value.state,
    backend: readText(value.backend).trim().toLowerCase() || 'boltz',
    pairIptm: toFiniteNumber(value.pairIptm),
    pairPae: toFiniteNumber(value.pairPae),
    pairIptmResolved: value.pairIptmResolved === true,
    ligandPlddt: normalizePlddtMetric(value.ligandPlddt),
    ligandAtomPlddts: compactLigandAtomPlddts(value.ligandAtomPlddts),
    structureText: '',
    structureFormat: readText(value.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: readText(value.structureName).trim(),
    error: readText(value.error),
    updatedAt: Number.isFinite(Number(value.updatedAt)) ? Number(value.updatedAt) : Date.now()
  };
}

function compactLeadOptPredictionMap(value: Record<string, LeadOptPredictionRecord>): Record<string, LeadOptPredictionRecord> {
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [smiles, record] of Object.entries(value)) {
    const key = readText(smiles).trim();
    if (!key) continue;
    out[key] = compactLeadOptPredictionRecord(record);
  }
  return out;
}

function compactLeadOptEnumeratedCandidateRow(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const smiles = readText(row.smiles).trim();
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
  return {
    smiles,
    ...(nPairs === null ? {} : { n_pairs: nPairs }),
    ...(medianDelta === null ? {} : { median_delta: medianDelta }),
    properties,
    property_deltas: propertyDeltas,
    final_highlight_atom_indices: highlightAtomIndices
  };
}

function compactLeadOptEnumeratedCandidates(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  const rows: Array<Record<string, unknown>> = [];
  for (const item of value) {
    const compact = compactLeadOptEnumeratedCandidateRow(item);
    if (!compact) continue;
    rows.push(compact);
  }
  return rows;
}

function readLeadOptStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map((item) => readText(item).trim())
        .filter(Boolean)
    )
  );
}

function readLeadOptIntegerArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map((item) => Number(item))
        .filter((item) => Number.isFinite(item) && item >= 0)
        .map((item) => Math.floor(item))
    )
  );
}

function buildLeadOptListMeta(leadOptMmpInput: unknown): Record<string, unknown> {
  const leadOptMmp = asRecord(leadOptMmpInput);
  const queryResult = asRecord(leadOptMmp.query_result);
  const selection = asRecord(leadOptMmp.selection);
  const predictionSummary = asRecord(leadOptMmp.prediction_summary);
  const predictionMap = asRecord(leadOptMmp.prediction_by_smiles);
  const compactCandidates = compactLeadOptEnumeratedCandidates(
    leadOptMmp.enumerated_candidates ?? asRecord(leadOptMmp.result_snapshot).enumerated_candidates
  );
  const compactQueryResult = {
    query_id: readText(leadOptMmp.query_id || queryResult.query_id).trim(),
    query_mode: readText(queryResult.query_mode).trim() || 'one-to-many',
    aggregation_type: readText(queryResult.aggregation_type).trim(),
    property_targets: asRecord(queryResult.property_targets),
    rule_env_radius: toFiniteNumber(queryResult.rule_env_radius),
    grouped_by_environment: readBooleanToken(queryResult.grouped_by_environment),
    mmp_database_id: readText(leadOptMmp.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOptMmp.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema).trim(),
    cluster_group_by: readText(queryResult.cluster_group_by).trim(),
    min_pairs: toFiniteNumber(queryResult.min_pairs),
    stats: asRecord(queryResult.stats)
  } as Record<string, unknown>;
  const predictionTotal = toFiniteNumber(predictionSummary.total);
  const selectedFragmentIds = readLeadOptStringArray(
    selection.selected_fragment_ids ?? leadOptMmp.selected_fragment_ids
  );
  const selectedFragmentAtomIndices = readLeadOptIntegerArray(
    selection.selected_fragment_atom_indices ?? leadOptMmp.selected_fragment_atom_indices
  );
  const selectedFragmentQuery =
    readLeadOptStringArray(selection.variable_queries ?? leadOptMmp.variable_queries)[0] ||
    readText(leadOptMmp.selected_fragment_query).trim();
  return {
    stage: readText(leadOptMmp.stage).trim(),
    prediction_stage: readText(leadOptMmp.prediction_stage).trim(),
    query_id: readText(leadOptMmp.query_id || queryResult.query_id).trim(),
    transform_count: toFiniteNumber(leadOptMmp.transform_count),
    candidate_count: toFiniteNumber(leadOptMmp.candidate_count),
    bucket_count:
      toFiniteNumber(leadOptMmp.bucket_count) ??
      predictionTotal ??
      Object.keys(predictionMap).length,
    mmp_database_id: readText(leadOptMmp.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOptMmp.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema).trim(),
    selection: {
      selected_fragment_ids: selectedFragmentIds,
      selected_fragment_atom_indices: selectedFragmentAtomIndices,
      variable_queries: selectedFragmentQuery ? [selectedFragmentQuery] : []
    },
    selected_fragment_ids: selectedFragmentIds,
    selected_fragment_atom_indices: selectedFragmentAtomIndices,
    selected_fragment_query: selectedFragmentQuery,
    prediction_summary: {
      total: predictionTotal,
      queued: toFiniteNumber(predictionSummary.queued),
      running: toFiniteNumber(predictionSummary.running),
      success: toFiniteNumber(predictionSummary.success),
      failure: toFiniteNumber(predictionSummary.failure)
    },
    query_result: compactQueryResult,
    enumerated_candidates: compactCandidates,
    ui_state: asRecord(leadOptMmp.ui_state),
    target_chain: readText(leadOptMmp.target_chain).trim(),
    ligand_chain: readText(leadOptMmp.ligand_chain).trim()
  };
}

function mergeLeadOptListMetaIntoProperties(
  propertiesInput: unknown,
  leadOptMmpInput: unknown
): Record<string, unknown> {
  const properties = asRecord(propertiesInput);
  return {
    ...properties,
    lead_opt_list: buildLeadOptListMeta(leadOptMmpInput)
  };
}

function buildLeadOptStateMeta(leadOptInput: unknown): Record<string, unknown> {
  const leadOpt = asRecord(leadOptInput);
  const predictionSummary = asRecord(leadOpt.prediction_summary);
  return {
    stage: readText(leadOpt.stage).trim(),
    prediction_stage: readText(leadOpt.prediction_stage).trim(),
    query_id: readText(leadOpt.query_id || asRecord(leadOpt.query_result).query_id).trim(),
    prediction_task_id: readText(leadOpt.prediction_task_id).trim(),
    prediction_candidate_smiles: readText(leadOpt.prediction_candidate_smiles).trim(),
    prediction_summary: {
      total: toFiniteNumber(predictionSummary.total) ?? 0,
      queued: toFiniteNumber(predictionSummary.queued) ?? 0,
      running: toFiniteNumber(predictionSummary.running) ?? 0,
      success: toFiniteNumber(predictionSummary.success) ?? 0,
      failure: toFiniteNumber(predictionSummary.failure) ?? 0,
      latest_task_id: readText(predictionSummary.latest_task_id).trim()
    },
    prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.prediction_by_smiles)),
    reference_prediction_by_backend: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.reference_prediction_by_backend)),
    target_chain: readText(leadOpt.target_chain).trim(),
    ligand_chain: readText(leadOpt.ligand_chain).trim()
  };
}

function mergeLeadOptStateMetaIntoProperties(
  propertiesInput: unknown,
  leadOptInput: unknown
): Record<string, unknown> {
  const properties = asRecord(propertiesInput);
  return {
    ...properties,
    lead_opt_state: buildLeadOptStateMeta(leadOptInput)
  };
}

function mergeLeadOptMetaIntoProperties(
  propertiesInput: unknown,
  leadOptInput: unknown
): Record<string, unknown> {
  const properties = asRecord(propertiesInput);
  return {
    ...properties,
    lead_opt_list: buildLeadOptListMeta(leadOptInput),
    lead_opt_state: buildLeadOptStateMeta(leadOptInput)
  };
}

function compactLeadOptForConfidenceWrite(leadOptInput: unknown): Record<string, unknown> {
  const leadOpt = asRecord(leadOptInput);
  const predictionSummary = asRecord(leadOpt.prediction_summary);
  const compactPredictionSummary = {
    total: toFiniteNumber(predictionSummary.total) ?? 0,
    queued: toFiniteNumber(predictionSummary.queued) ?? 0,
    running: toFiniteNumber(predictionSummary.running) ?? 0,
    success: toFiniteNumber(predictionSummary.success) ?? 0,
    failure: toFiniteNumber(predictionSummary.failure) ?? 0,
    latest_task_id: readText(predictionSummary.latest_task_id).trim()
  };
  return {
    stage: readText(leadOpt.stage).trim(),
    prediction_stage: readText(leadOpt.prediction_stage).trim(),
    query_id: readText(leadOpt.query_id || asRecord(leadOpt.query_result).query_id).trim(),
    mmp_database_id: readText(leadOpt.mmp_database_id || asRecord(leadOpt.query_result).mmp_database_id).trim(),
    target_chain: readText(leadOpt.target_chain).trim(),
    ligand_chain: readText(leadOpt.ligand_chain).trim(),
    prediction_summary: compactPredictionSummary,
    prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.prediction_by_smiles)),
    reference_prediction_by_backend: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.reference_prediction_by_backend))
  };
}

function buildLeadOptPredictionPersistSignature(records: Record<string, LeadOptPredictionRecord>): string {
  return Object.entries(records)
    .map(([key, record]) => {
      const normalizedKey = readText(key).trim();
      const taskId = readText(record.taskId).trim();
      const state = readText(record.state).trim().toUpperCase();
      const backend = readText(record.backend).trim().toLowerCase() || 'boltz';
      const pairIptm = toFiniteNumber(record.pairIptm);
      const pairPae = toFiniteNumber(record.pairPae);
      const ligandPlddt = normalizePlddtMetric(record.ligandPlddt);
      const atomPlddts = compactLigandAtomPlddts(record.ligandAtomPlddts);
      const atomPlddtSignature = atomPlddts.length > 0
        ? `${atomPlddts.length}:${atomPlddts[0]?.toFixed(2) || ''}:${atomPlddts[atomPlddts.length - 1]?.toFixed(2) || ''}`
        : '';
      const error = readText(record.error).trim();
      return [
        normalizedKey,
        taskId,
        state,
        backend,
        pairIptm === null ? '' : pairIptm.toFixed(4),
        pairPae === null ? '' : pairPae.toFixed(3),
        record.pairIptmResolved === true ? '1' : '0',
        ligandPlddt === null ? '' : ligandPlddt.toFixed(3),
        atomPlddtSignature,
        error
      ].join('~');
    })
    .sort((a, b) => a.localeCompare(b))
    .join('||');
}

function compactLeadOptCandidatesUiState(value: LeadOptCandidatesUiState): LeadOptCandidatesUiState {
  return {
    selectedBackend: readText(value.selectedBackend).trim().toLowerCase() || 'pocketxmol',
    stateFilter: value.stateFilter,
    showAdvanced: value.showAdvanced === true,
    mwMin: readText(value.mwMin).trim(),
    mwMax: readText(value.mwMax).trim(),
    logpMin: readText(value.logpMin).trim(),
    logpMax: readText(value.logpMax).trim(),
    tpsaMin: readText(value.tpsaMin).trim(),
    tpsaMax: readText(value.tpsaMax).trim(),
    plddtMin: readText(value.plddtMin).trim(),
    plddtMax: readText(value.plddtMax).trim(),
    iptmMin: readText(value.iptmMin).trim(),
    iptmMax: readText(value.iptmMax).trim(),
    paeMin: readText(value.paeMin).trim(),
    paeMax: readText(value.paeMax).trim(),
    structureSearchMode: value.structureSearchMode,
    structureSearchQuery: readText(value.structureSearchQuery).trim(),
    previewRenderMode: value.previewRenderMode
  };
}

function resolveLeadOptSnapshotFromTask(taskInput: unknown): Record<string, unknown> {
  const task = asRecord(taskInput);
  const fromConfidence = asRecord(asRecord(task.confidence).lead_opt_mmp);
  const properties = asRecord(task.properties);
  const fromProperties = asRecord(properties.lead_opt_list);
  const fromPropertiesState = asRecord(properties.lead_opt_state);
  if (Object.keys(fromConfidence).length === 0 && Object.keys(fromPropertiesState).length === 0) return fromProperties;
  if (Object.keys(fromProperties).length === 0 && Object.keys(fromPropertiesState).length === 0) return fromConfidence;

  const confidenceQueryResult = asRecord(fromConfidence.query_result);
  const propertiesQueryResult = asRecord(fromProperties.query_result);
  const mergedPredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(fromConfidence.prediction_by_smiles, fromProperties.prediction_by_smiles),
    fromPropertiesState.prediction_by_smiles
  );
  const mergedReferencePredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(fromConfidence.reference_prediction_by_backend, fromProperties.reference_prediction_by_backend),
    fromPropertiesState.reference_prediction_by_backend
  );
  const confidenceSelection = asRecord(fromConfidence.selection);
  const propertiesSelection = asRecord(fromProperties.selection);
  const confidenceUiState = asRecord(fromConfidence.ui_state);
  const propertiesUiState = asRecord(fromProperties.ui_state);

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
    stage: readText(fromPropertiesState.stage).trim() || readText(fromProperties.stage || fromConfidence.stage).trim(),
    prediction_stage:
      readText(fromPropertiesState.prediction_stage).trim() ||
      readText(fromProperties.prediction_stage || fromConfidence.prediction_stage).trim(),
    prediction_summary:
      Object.keys(asRecord(fromPropertiesState.prediction_summary)).length > 0
        ? asRecord(fromPropertiesState.prediction_summary)
        : asRecord(fromProperties.prediction_summary || fromConfidence.prediction_summary),
    selection:
      Object.keys(propertiesSelection).length > 0
        ? propertiesSelection
        : confidenceSelection,
    ui_state:
      Object.keys(propertiesUiState).length > 0
        ? propertiesUiState
        : confidenceUiState,
    query_id:
      readText(fromProperties.query_id || propertiesQueryResult.query_id).trim() ||
      readText(fromConfidence.query_id || confidenceQueryResult.query_id).trim(),
    target_chain: readText(fromProperties.target_chain).trim() || readText(fromConfidence.target_chain).trim(),
    ligand_chain: readText(fromProperties.ligand_chain).trim() || readText(fromConfidence.ligand_chain).trim(),
    prediction_task_id:
      readText(fromPropertiesState.prediction_task_id).trim() ||
      readText(fromProperties.prediction_task_id || fromConfidence.prediction_task_id).trim(),
    prediction_candidate_smiles:
      readText(fromPropertiesState.prediction_candidate_smiles).trim() ||
      readText(fromProperties.prediction_candidate_smiles || fromConfidence.prediction_candidate_smiles).trim()
  };
}

function buildLeadOptSelectionFromPayload(payload: Record<string, unknown>, context: {
  querySmiles: string;
  targetChain: string;
  ligandChain: string;
}) {
  const variableSpec = asRecord(payload.variable_spec);
  const variableItems = asRecordArray(variableSpec.items).map((item) => {
    const atomIndices = Array.isArray(item.atom_indices)
      ? item.atom_indices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
      : [];
    return {
      query: readText(item.query).trim(),
      mode: readText(item.mode).trim() || 'substructure',
      fragment_id: readText(item.fragment_id).trim(),
      atom_indices: atomIndices
    };
  });
  const selectedFragmentIdsFromPayload = Array.isArray(payload.selected_fragment_ids)
    ? payload.selected_fragment_ids
        .map((value) => readText(value).trim())
        .filter(Boolean)
    : [];
  const selectedFragmentAtomIndicesFromPayload = Array.isArray(payload.selected_fragment_atom_indices)
    ? payload.selected_fragment_atom_indices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
    : [];
  const selectedFragmentIds = Array.from(
    new Set(
      selectedFragmentIdsFromPayload.length > 0
        ? selectedFragmentIdsFromPayload
        : variableItems.map((item) => readText(item.fragment_id).trim()).filter(Boolean)
    )
  );
  const selectedFragmentAtomIndices = Array.from(
    new Set(
      selectedFragmentAtomIndicesFromPayload.length > 0
        ? selectedFragmentAtomIndicesFromPayload
        : variableItems.flatMap((item) => item.atom_indices || [])
    )
  );
  const variableQueries = Array.from(
    new Set(variableItems.map((item) => readText(item.query).trim()).filter(Boolean))
  );
  const groupedByEnvironmentValue = readBooleanToken(payload.grouped_by_environment);
  const groupedByEnvironmentMode =
    groupedByEnvironmentValue === true ? 'on' : groupedByEnvironmentValue === false ? 'off' : 'auto';
  const propertyTargets = asRecord(payload.property_targets);
  const queryProperty = readText(propertyTargets.property).trim();
  const directionToken = readText(propertyTargets.direction).trim().toLowerCase();
  const direction = directionToken === 'increase' || directionToken === 'decrease' ? directionToken : '';
  const minPairsRaw = Number(payload.min_pairs);
  const minPairs = Number.isFinite(minPairsRaw) ? Math.max(1, Math.floor(minPairsRaw)) : 1;
  const envRadiusRaw = Number(payload.rule_env_radius);
  const envRadius = Number.isFinite(envRadiusRaw) ? Math.max(0, Math.floor(envRadiusRaw)) : 1;
  return {
    query_smiles: readText(context.querySmiles).trim(),
    target_chain: readText(context.targetChain).trim(),
    ligand_chain: readText(context.ligandChain).trim(),
    selected_fragment_ids: selectedFragmentIds,
    selected_fragment_atom_indices: selectedFragmentAtomIndices,
    variable_queries: variableQueries,
    variable_items: variableItems,
    grouped_by_environment_mode: groupedByEnvironmentMode,
    query_property: queryProperty,
    direction,
    min_pairs: minPairs,
    env_radius: envRadius
  };
}

type WorkspaceRuntime = ReturnType<typeof useProjectDetailRuntimeContext>;
type WorkspaceRuntimeReady = WorkspaceRuntime & {
  project: NonNullable<WorkspaceRuntime['project']>;
  draft: NonNullable<WorkspaceRuntime['draft']>;
};

export function useProjectDetailWorkspaceView() {
  const runtime = useProjectDetailRuntimeContext();
  const { locationSearch, entryRoutingResolved, loading, error, project, draft } = runtime;

  if (!entryRoutingResolved || loading) {
    const query = new URLSearchParams(locationSearch);
    const requestedTaskRowId = String(query.get('task_row_id') || '').trim();
    const loadingLabel =
      !entryRoutingResolved
        ? 'Loading project...'
        : requestedTaskRowId || query.get('tab') === 'results'
          ? 'Loading current task...'
          : 'Loading project...';
    return <div className="centered-page">{loadingLabel}</div>;
  }

  if (error && !project) {
    return (
      <div className="page-grid">
        <div className="alert error">{error}</div>
        <Link className="btn btn-ghost" to="/projects">
          Back to projects
        </Link>
      </div>
    );
  }

  if (!project || !draft) {
    return null;
  }

  return <ProjectDetailWorkspaceLoaded runtime={runtime as WorkspaceRuntimeReady} />;
}

function ProjectDetailWorkspaceLoaded({ runtime }: { runtime: WorkspaceRuntimeReady }) {
  const [leadOptHeaderRunAction, setLeadOptHeaderRunAction] = useState<(() => void | Promise<void>) | null>(null);
  const [leadOptHeaderRunPending, setLeadOptHeaderRunPending] = useState(false);
  const handleRegisterLeadOptHeaderRunAction = useCallback((action: (() => void | Promise<void>) | null) => {
    setLeadOptHeaderRunAction(() => action);
  }, []);
  const {
    loading,
    error,
    setError,
    project,
    draft,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    hasIncompleteComponents,
    componentCompletion,
    submitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFile,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError,
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    chainInfoById,
    componentTypeBuckets,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    activeConstraintId,
    selectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintPickSlotRef,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    structureText,
    structureFormat,
    structureTaskId,
    confidenceBackend,
    projectBackend,
    activeResultTask,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIptmTone,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreview,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity,
    snapshotConfidence,
    statusInfo,
    statusContextTaskRow,
    requestedStatusTaskRow,
    projectTasks,
    snapshotLigandAtomPlddts,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponent,
    snapshotLigandResiduePlddts,
    setProteinTemplates,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions,
    pickedResidue,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    allowedConstraintTypes,
    sidebarTypeOpen,
    activeComponentId,
    sidebarConstraintsOpen,
    selectedContactConstraintIdSet,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    affinityEnableDisabledReason,
    isResultsResizing,
    resultsGridRef,
    handleResultsResizerPointerDown,
    handleResultsResizerKeyDown,
    resultsGridStyle,
    resultChainIds,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityUseMsaChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    leadOptPrimary,
    leadOptChainContext,
    leadOptPersistedUploads,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    handleComponentsResizerPointerDown,
    handleComponentsResizerKeyDown,
    proteinTemplates,
    displayTaskState,
    isActiveRuntime,
    progressPercent,
    waitingSeconds,
    totalRuntimeSeconds,
    hasUnsavedChanges,
    runMenuOpen,
    runSuccessNotice,
    resultError,
    resultChainConsistencyWarning,
    runActionRef,
    topRunButtonRef,
    patchTask,
    pullResultForViewer,
    persistDraftTaskSnapshot,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saveDraft,
    setRunRedirectTaskId,
    navigate,
    affinityLigandFile
  } = runtime;
  const leadOptMmpTaskRowMapRef = useRef<Record<string, string>>({});
  const leadOptUploadPersistKeyRef = useRef('');
  const leadOptActiveTaskRowIdRef = useRef('');
  const leadOptPredictionPersistKeyRef = useRef('');
  const leadOptPredictionPersistQueueRef = useRef<Promise<void>>(Promise.resolve());
  const leadOptUiStatePersistKeyRef = useRef('');
  const leadOptMmpContextByTaskIdRef = useRef<Record<string, Record<string, unknown>>>({});

  const resolveLeadOptTaskRowId = useCallback((): string => {
    const requestedRowId = readText(requestedStatusTaskRow?.id).trim();
    if (requestedRowId) return requestedRowId;

    const contextRowId = readText(statusContextTaskRow?.id).trim();
    if (contextRowId) return contextRowId;

    const activeResultRowId = readText(activeResultTask?.id).trim();
    if (activeResultRowId) return activeResultRowId;

    const rememberedRowId = readText(leadOptActiveTaskRowIdRef.current).trim();
    if (rememberedRowId) return rememberedRowId;

    const leadOptSnapshotTask = projectTasks.find((row) => {
      const properties = asRecord(row?.properties);
      const leadOptList = asRecord(properties.lead_opt_list);
      const leadOptState = asRecord(properties.lead_opt_state);
      const leadOptConfidence = asRecord(asRecord(row?.confidence).lead_opt_mmp);
      return (
        Object.keys(leadOptList).length > 0 ||
        Object.keys(leadOptState).length > 0 ||
        Object.keys(leadOptConfidence).length > 0
      );
    });
    const leadOptSnapshotTaskRowId = readText(leadOptSnapshotTask?.id).trim();
    if (leadOptSnapshotTaskRowId) return leadOptSnapshotTaskRowId;

    const latestRuntimeTaskRow = projectTasks.find((row) => readText(row?.task_id).trim().length > 0);
    const latestRuntimeTaskRowId = readText(latestRuntimeTaskRow?.id).trim();
    if (latestRuntimeTaskRowId) return latestRuntimeTaskRowId;

    const firstTaskRowId = readText(projectTasks[0]?.id).trim();
    if (firstTaskRowId) return firstTaskRowId;

    return '';
  }, [activeResultTask, projectTasks, requestedStatusTaskRow, statusContextTaskRow]);

  const resolveLeadOptSourceTask = useCallback(
    (taskRowId: string) => {
      const id = readText(taskRowId).trim();
      if (!id) return null;
      if (requestedStatusTaskRow && String(requestedStatusTaskRow.id) === id) return requestedStatusTaskRow;
      if (statusContextTaskRow && String(statusContextTaskRow.id) === id) return statusContextTaskRow;
      if (activeResultTask && String(activeResultTask.id) === id) return activeResultTask;
      return null;
    },
    [activeResultTask, requestedStatusTaskRow, statusContextTaskRow]
  );

  const handleLeadOptMmpTaskQueued = async (payload: {
    taskId: string;
    requestPayload: Record<string, unknown>;
    querySmiles: string;
    referenceUploads: LeadOptPersistedUploads;
  }) => {
    if (!project || !draft) return;
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const effectiveLeadOptLigandSmiles =
      readText(payload.querySmiles).trim() || readText(leadOptPrimary.ligandSmiles).trim();
    const snapshotComponents = buildLeadOptUploadSnapshotComponents(
      draft.inputConfig.components,
      payload.referenceUploads,
      effectiveLeadOptLigandSmiles
    );
    const queuedAt = new Date().toISOString();
    const selection = buildLeadOptSelectionFromPayload(payload.requestPayload || {}, {
      querySmiles: payload.querySmiles || leadOptPrimary.ligandSmiles,
      targetChain: leadOptChainContext.targetChain,
      ligandChain: leadOptChainContext.ligandChain
    });
    const mmpContext = {
      query_payload: payload.requestPayload || {},
      selection,
      target_chain: readText(leadOptChainContext.targetChain).trim(),
      ligand_chain: readText(leadOptChainContext.ligandChain).trim()
    } as Record<string, unknown>;
    const draftTaskRow = await persistDraftTaskSnapshot(draft.inputConfig, {
      statusText: 'Lead optimization MMP query queued',
      reuseTaskRowId: null,
      snapshotComponents,
      proteinSequenceOverride: leadOptPrimary.proteinSequence,
      ligandSmilesOverride: effectiveLeadOptLigandSmiles
    });
    leadOptMmpTaskRowMapRef.current[taskId] = draftTaskRow.id;
    leadOptActiveTaskRowIdRef.current = draftTaskRow.id;
    leadOptMmpContextByTaskIdRef.current[taskId] = mmpContext;
    const leadOptPayload = {
      stage: 'queued',
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: {},
      ...mmpContext
    } as Record<string, unknown>;
    await patchTask(draftTaskRow.id, {
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'MMP query queued',
      error_text: '',
      submitted_at: queuedAt,
      completed_at: null,
      duration_seconds: null,
      components: snapshotComponents,
      properties: mergeLeadOptMetaIntoProperties(draft.inputConfig.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
      }
    });
    setRunRedirectTaskId(taskId);
  };

  const handleLeadOptMmpTaskCompleted = async (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => {
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const taskRowId = leadOptMmpTaskRowMapRef.current[taskId];
    if (!taskRowId) return;
    leadOptActiveTaskRowIdRef.current = taskRowId;
    const completedAt = new Date().toISOString();
    const mmpContext = asRecord(leadOptMmpContextByTaskIdRef.current[taskId]);
    const snapshot = asRecord(payload.resultSnapshot);
    const queryResult = asRecord(snapshot.query_result);
    const enumeratedCandidates = compactLeadOptEnumeratedCandidates(snapshot.enumerated_candidates);
    const uiState = asRecord(snapshot.ui_state);
    const compactQueryResult: Record<string, unknown> = {
      query_id: readText(payload.queryId).trim(),
      query_mode: readText(queryResult.query_mode).trim() || 'one-to-many',
      aggregation_type: readText(queryResult.aggregation_type).trim(),
      mmp_database_id: readText(queryResult.mmp_database_id).trim(),
      mmp_database_label: readText(queryResult.mmp_database_label).trim(),
      mmp_database_schema: readText(queryResult.mmp_database_schema).trim(),
      property_targets: asRecord(queryResult.property_targets),
      rule_env_radius: Number.isFinite(Number(queryResult.rule_env_radius)) ? Number(queryResult.rule_env_radius) : 1,
      grouped_by_environment:
        readBooleanToken(queryResult.grouped_by_environment) === null
          ? undefined
          : readBooleanToken(queryResult.grouped_by_environment),
      count: Number.isFinite(Number(queryResult.count)) ? Number(queryResult.count) : payload.transformCount,
      global_count: Number.isFinite(Number(queryResult.global_count)) ? Number(queryResult.global_count) : payload.transformCount,
      min_pairs: Number.isFinite(Number(queryResult.min_pairs)) ? Number(queryResult.min_pairs) : 1,
      cluster_group_by: readText(queryResult.cluster_group_by).trim(),
      stats: asRecord(queryResult.stats)
    };
    const leadOptPayload = {
      stage: 'completed',
      query_id: payload.queryId,
      transform_count: payload.transformCount,
      candidate_count: payload.candidateCount,
      query_result: compactQueryResult,
      result_storage: 'server_query_cache',
      enumerated_candidates: enumeratedCandidates,
      ui_state: uiState,
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: {},
      ...mmpContext
    } as Record<string, unknown>;
    const sourceTask = resolveLeadOptSourceTask(taskRowId);
    await patchTask(taskRowId, {
      task_state: 'SUCCESS',
      status_text: `MMP complete (${payload.transformCount} transforms, ${payload.candidateCount} rows). Scoring not started.`,
      error_text: '',
      completed_at: completedAt,
      duration_seconds: Number.isFinite(payload.elapsedSeconds) ? payload.elapsedSeconds : null,
      properties: mergeLeadOptMetaIntoProperties(sourceTask?.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
      }
    });
    delete leadOptMmpTaskRowMapRef.current[taskId];
    delete leadOptMmpContextByTaskIdRef.current[taskId];
  };

  const handleLeadOptMmpTaskFailed = async (payload: { taskId: string; error: string }) => {
    const taskId = String(payload.taskId || '').trim();
    if (!taskId) return;
    const taskRowId = leadOptMmpTaskRowMapRef.current[taskId];
    if (!taskRowId) return;
    leadOptActiveTaskRowIdRef.current = taskRowId;
    const completedAt = new Date().toISOString();
    const mmpContext = asRecord(leadOptMmpContextByTaskIdRef.current[taskId]);
    const leadOptPayload = {
      stage: 'failed',
      prediction_stage: 'idle',
      prediction_summary: {
        total: 0,
        queued: 0,
        running: 0,
        success: 0,
        failure: 0
      },
      prediction_by_smiles: {},
      reference_prediction_by_backend: {},
      ...mmpContext
    } as Record<string, unknown>;
    const sourceTask = resolveLeadOptSourceTask(taskRowId);
    await patchTask(taskRowId, {
      task_state: 'FAILURE',
      status_text: 'MMP query failed',
      error_text: payload.error || 'MMP query failed.',
      completed_at: completedAt,
      properties: mergeLeadOptMetaIntoProperties(sourceTask?.properties, leadOptPayload) as any,
      confidence: {
        lead_opt_mmp: compactLeadOptForConfidenceWrite(leadOptPayload)
      }
    });
    delete leadOptMmpTaskRowMapRef.current[taskId];
    delete leadOptMmpContextByTaskIdRef.current[taskId];
  };

  const handleLeadOptPredictionQueued = useCallback(
    async (payload: { taskId: string; backend: string; candidateSmiles: string }) => {
      const taskId = readText(payload.taskId).trim();
      if (!taskId) return;
      const taskRowId = resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;
      // Prediction queue status is persisted by the throttled state-change handler.
      // Avoid per-candidate writes here to prevent burst traffic on batch submit.
    },
    [resolveLeadOptTaskRowId]
  );

  const handleLeadOptPredictionStateChange = useCallback(
    async (payload: {
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
    }) => {
      const taskRowId = resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;

      const records = compactLeadOptPredictionMap(asPredictionRecordMap(payload.records));
      const referenceRecords = compactLeadOptPredictionMap(asPredictionRecordMap(payload.referenceRecords));
      const summary = summarizeLeadOptPredictions(records);
      const unresolved = summary.queued + summary.running;
      const unresolvedState = summary.running > 0 ? 'RUNNING' : summary.queued > 0 ? 'QUEUED' : null;
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const nextTaskState: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' =
        unresolved > 0
          ? unresolvedState === 'RUNNING'
            ? 'RUNNING'
            : 'QUEUED'
          : summary.total > 0 && summary.success === 0 && summary.failure > 0
            ? 'FAILURE'
            : 'SUCCESS';
      const sourceTaskState = readText(sourceTask?.task_state).trim().toUpperCase();
      const persistedTaskState: 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' =
        sourceTaskState === 'SUCCESS' && (nextTaskState === 'QUEUED' || nextTaskState === 'RUNNING')
          ? 'SUCCESS'
          : nextTaskState;
      const statusText =
        unresolved > 0
          ? unresolvedState === 'RUNNING'
            ? `Scoring ${unresolved} running (${summary.success}/${Math.max(1, summary.total)} done)`
            : `Scoring ${unresolved} queued (${summary.success}/${Math.max(1, summary.total)} done)`
          : summary.total > 0
            ? `Scoring complete (${summary.success}/${Math.max(1, summary.total)})`
            : 'MMP complete';
      const errorText = summary.total > 0 && summary.success === 0 && summary.failure > 0 ? 'All candidate scoring jobs failed.' : '';

      const sourceLeadOpt = resolveLeadOptSnapshotFromTask(sourceTask);
      const nextLeadOpt = {
        ...sourceLeadOpt,
        stage:
          unresolved > 0
            ? unresolvedState === 'RUNNING'
              ? 'prediction_running'
              : 'prediction_queued'
            : summary.failure > 0 && summary.success === 0
              ? 'prediction_failed'
              : 'prediction_completed',
        prediction_stage: unresolved > 0 ? (unresolvedState === 'RUNNING' ? 'running' : 'queued') : 'completed',
        prediction_summary: {
          ...summary,
          latest_task_id: readText(payload.summary?.latestTaskId).trim()
        },
        bucket_count: summary.total,
        prediction_by_smiles: records,
        reference_prediction_by_backend: referenceRecords
      } as Record<string, unknown>;
      const persistKey = [
        taskRowId,
        statusText,
        errorText,
        summary.total,
        summary.queued,
        summary.running,
        summary.success,
        summary.failure,
        buildLeadOptPredictionPersistSignature(records),
        buildLeadOptPredictionPersistSignature(referenceRecords)
      ].join('|');
      if (leadOptPredictionPersistKeyRef.current === persistKey) return;
      leadOptPredictionPersistKeyRef.current = persistKey;

      const patchPayload = {
        task_state: persistedTaskState,
        status_text: statusText,
        error_text: errorText,
        properties: mergeLeadOptStateMetaIntoProperties(sourceTask?.properties, nextLeadOpt) as any,
        confidence: {
          lead_opt_mmp: compactLeadOptForConfidenceWrite(nextLeadOpt)
        }
      };
      const nextPersist = leadOptPredictionPersistQueueRef.current
        .catch(() => undefined)
        .then(async () => {
          await patchTask(taskRowId, patchPayload);
        });
      leadOptPredictionPersistQueueRef.current = nextPersist;
      await nextPersist;
    },
    [patchTask, resolveLeadOptSourceTask, resolveLeadOptTaskRowId]
  );

  const handleLeadOptUiStateChange = useCallback(
    async (payload: { uiState: LeadOptCandidatesUiState }) => {
      if (!project) return;
      const taskRowId = resolveLeadOptTaskRowId();
      if (!taskRowId) return;
      leadOptActiveTaskRowIdRef.current = taskRowId;
      const sourceTask = resolveLeadOptSourceTask(taskRowId);
      const sourceLeadOpt = resolveLeadOptSnapshotFromTask(sourceTask);
      if (Object.keys(sourceLeadOpt).length === 0) return;
      const compactUiState = compactLeadOptCandidatesUiState(payload.uiState);
      const persistKey = [
        taskRowId,
        buildLeadOptCandidatesUiStateSignature(compactUiState),
        readText(sourceLeadOpt.query_id || asRecord(sourceLeadOpt.query_result).query_id).trim()
      ].join('|');
      if (leadOptUiStatePersistKeyRef.current === persistKey) return;
      leadOptUiStatePersistKeyRef.current = persistKey;
      const nextLeadOpt = {
        ...sourceLeadOpt,
        ui_state: compactUiState
      };
      await patchTask(taskRowId, {
        properties: mergeLeadOptListMetaIntoProperties(sourceTask?.properties, nextLeadOpt) as any
      });
    },
    [patchTask, project, resolveLeadOptSourceTask, resolveLeadOptTaskRowId]
  );

  const handleLeadOptReferenceUploadsChange = useCallback(
    async (uploads: LeadOptPersistedUploads) => {
      if (!project || !draft || !canEdit) return;
      if (workspaceTab !== 'components') return;
      const targetName = readText(uploads.target?.fileName).trim();
      const targetSize = readText(uploads.target?.content).length;
      const ligandName = readText(uploads.ligand?.fileName).trim();
      const ligandSize = readText(uploads.ligand?.content).length;
      const contextDraftRowId =
        String(statusContextTaskRow?.task_state || '').toUpperCase() === 'DRAFT'
          ? readText(statusContextTaskRow?.id).trim()
          : '';
      const editableDraftRowId = contextDraftRowId;
      const effectiveLeadOptLigandSmiles = readText(leadOptPrimary.ligandSmiles).trim();
      const dedupeKey = `${project.id}|${editableDraftRowId}|${targetName}:${targetSize}|${ligandName}:${ligandSize}|${effectiveLeadOptLigandSmiles}`;
      if (leadOptUploadPersistKeyRef.current === dedupeKey) return;
      leadOptUploadPersistKeyRef.current = dedupeKey;

      const snapshotComponents = buildLeadOptUploadSnapshotComponents(
        draft.inputConfig.components,
        uploads,
        effectiveLeadOptLigandSmiles
      );

      if (editableDraftRowId) {
        await patchTask(editableDraftRowId, {
          components: snapshotComponents,
          protein_sequence: leadOptPrimary.proteinSequence,
          ligand_smiles: effectiveLeadOptLigandSmiles
        });
      }
    },
    [
      canEdit,
      draft,
      leadOptPrimary.ligandSmiles,
      leadOptPrimary.proteinSequence,
      patchTask,
      project,
      statusContextTaskRow,
      workspaceTab
    ]
  );

  const workflow = getWorkflowDefinition(project.task_type);
  const affinityUseMsa = computeUseMsaFlag(draft.inputConfig.components, draft.use_msa);
  const runSubmitting = submitting || (isLeadOptimizationWorkflow && leadOptHeaderRunPending);
  const leadOptInitialMmpSnapshot = (() => {
    const preferredTaskRowId = resolveLeadOptTaskRowId();
    const sourceTask = resolveLeadOptSourceTask(preferredTaskRowId);
    const leadOptMmp = resolveLeadOptSnapshotFromTask(sourceTask);
    if (!leadOptMmp || Object.keys(leadOptMmp).length === 0) return null;
    const queryResult = asRecord(leadOptMmp.query_result);
    const queryId = readText(leadOptMmp.query_id || queryResult.query_id).trim();
    if (!queryId) return null;
    return {
      query_result: {
        query_id: queryId,
        query_mode: readText(queryResult.query_mode || 'one-to-many') || 'one-to-many',
        aggregation_type: readText(queryResult.aggregation_type).trim(),
        property_targets: asRecord(queryResult.property_targets),
        rule_env_radius: Number.isFinite(Number(queryResult.rule_env_radius)) ? Number(queryResult.rule_env_radius) : 1,
        grouped_by_environment:
          readBooleanToken(queryResult.grouped_by_environment) === null
            ? undefined
            : readBooleanToken(queryResult.grouped_by_environment),
        mmp_database_id: readText(queryResult.mmp_database_id).trim(),
        mmp_database_label: readText(queryResult.mmp_database_label).trim(),
        mmp_database_schema: readText(queryResult.mmp_database_schema).trim(),
        cluster_group_by: readText(queryResult.cluster_group_by).trim(),
        transforms: Array.isArray(queryResult.transforms) ? queryResult.transforms : [],
        global_transforms: Array.isArray(queryResult.global_transforms) ? queryResult.global_transforms : [],
        clusters: Array.isArray(queryResult.clusters) ? queryResult.clusters : [],
        stats: asRecord(queryResult.stats),
        count: Number(queryResult.count || 0),
        global_count: Number(queryResult.global_count || 0)
      },
      enumerated_candidates: compactLeadOptEnumeratedCandidates(leadOptMmp.enumerated_candidates),
      prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOptMmp.prediction_by_smiles)),
      reference_prediction_by_backend: compactLeadOptPredictionMap(
        asPredictionRecordMap(leadOptMmp.reference_prediction_by_backend)
      ),
      ui_state: asRecord(leadOptMmp.ui_state),
      selection: asRecord(leadOptMmp.selection),
      query_payload: asRecord(leadOptMmp.query_payload),
      target_chain: readText(leadOptMmp.target_chain).trim(),
      ligand_chain: readText(leadOptMmp.ligand_chain).trim()
    } as Record<string, unknown>;
  })();
  const leadOptSnapshotContext = asRecord(leadOptInitialMmpSnapshot || null);
  const leadOptSnapshotSelection = asRecord(leadOptSnapshotContext.selection);
  const leadOptSnapshotQueryPayload = asRecord(leadOptSnapshotContext.query_payload);
  const leadOptSnapshotTargetChain =
    readText(leadOptSnapshotContext.target_chain).trim() ||
    readText(leadOptSnapshotSelection.target_chain).trim() ||
    readText(leadOptSnapshotQueryPayload.target_chain).trim();
  const leadOptSnapshotLigandChain =
    readText(leadOptSnapshotContext.ligand_chain).trim() ||
    readText(leadOptSnapshotSelection.ligand_chain).trim() ||
    readText(leadOptSnapshotQueryPayload.ligand_chain).trim();
  const leadOptWorkspaceTargetChain = leadOptSnapshotTargetChain || readText(leadOptChainContext.targetChain).trim();
  const leadOptWorkspaceLigandChain = leadOptSnapshotLigandChain || readText(leadOptChainContext.ligandChain).trim();
  const {
    componentStepLabel,
    isRunRedirecting,
    showQuickRunFab,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    runBlockedReason,
    runDisabled,
    canOpenRunMenu,
    sidebarTypeOrder
  } = useProjectRunState({
    workspaceTab,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    hasIncompleteComponents,
    componentCompletion,
    submitting: runSubmitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFilePresent: Boolean(affinityTargetFile),
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError: String(affinityPreviewError || ''),
    affinityTargetChainCount: affinityTargetChainIds.length,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    draftBackend: draft.backend
  });
  const formatConstraintCombo = (constraint: PredictionConstraint) =>
    formatConstraintComboForWorkspace(constraint, chainInfoById, componentTypeBuckets);
  const formatConstraintDetail = (constraint: PredictionConstraint) =>
    formatConstraintDetailForWorkspace(constraint);
  const {
    addComponentToDraft,
    addConstraintFromSidebar,
    setAffinityEnabledFromWorkspace,
    setAffinityComponentFromWorkspace,
    jumpToComponent
  } = useProjectSidebarActions({
    draft,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    createInputComponent
  });
  const {
    clearConstraintSelection,
    selectConstraint,
    jumpToConstraint,
    navigateConstraint,
    applyPickToSelectedConstraint
  } = useConstraintWorkspaceActions({
    draft,
    setDraft,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintSelectionAnchorRef,
    setWorkspaceTab,
    setSidebarConstraintsOpen,
    scrollToEditorBlock,
    constraintPickSlotRef,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    ligandChainOptions,
    isBondOnlyBackend
  });
  const {
    displayStructureText,
    displayStructureFormat,
    displayStructureName,
    displayStructureColorMode,
    constraintStructureText,
    constraintStructureFormat,
    hasConstraintStructure,
    snapshotCards,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    affinityResultLigandSmiles,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    hasAffinityDisplayStructure,
  } = useProjectResultDisplay({
    structureText,
    structureFormat,
    confidenceBackend,
    projectBackend,
    activeResultTaskStructureName: activeResultTask?.structure_name || '',
    projectStructureName: project.structure_name || '',
    draftColorMode: draft.color_mode,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedTemplatePreviewContent: selectedTemplatePreview?.content || '',
    selectedTemplatePreviewFormat: selectedTemplatePreview?.format || 'pdb',
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIptmTone,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity: snapshotAffinity || null,
    snapshotConfidence: snapshotConfidence || null,
    statusContextLigandSmiles: String(statusContextTaskRow?.ligand_smiles || ''),
    activeResultLigandSmiles: String(activeResultTask?.ligand_smiles || ''),
    snapshotLigandAtomPlddts: snapshotLigandAtomPlddts || [],
    affinityLigandSmiles,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponentType: selectedResultLigandComponent?.type || null,
    snapshotLigandResiduePlddts,
  });
  const {
    handlePredictionComponentsChange,
    handlePredictionProteinTemplateChange,
    handlePredictionTemplateResiduePick,
    handleRuntimeBackendChange,
    handleRuntimeSeedChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideMutationRateChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleTaskNameChange,
    handleTaskSummaryChange
  } = useProjectEditorHandlers({
    setDraft,
    setPickedResidue,
    setProteinTemplates,
    filterConstraintsByBackend
  });
  const { predictionConstraintsWorkspaceProps, predictionComponentsSidebarProps } = usePredictionWorkspaceProps({
    draft,
    setDraft,
    filterConstraintsByBackend,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions: constraintTemplateOptions || [],
    selectedTemplatePreview,
    setSelectedConstraintTemplateComponentId,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    canEdit,
    setWorkspaceTab,
    navigateConstraint,
    pickedResidue,
    hasConstraintStructure,
    constraintStructureText,
    constraintStructureFormat,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    applyPickToSelectedConstraint,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    clearConstraintSelection,
    activeConstraintId,
    selectedContactConstraintIds,
    selectConstraint,
    allowedConstraintTypes,
    isBondOnlyBackend,
    hasIncompleteComponents,
    componentCompletion,
    sidebarTypeOrder,
    componentTypeBuckets,
    sidebarTypeOpen,
    setSidebarTypeOpen,
    addComponentToDraft,
    activeComponentId,
    jumpToComponent,
    sidebarConstraintsOpen,
    setSidebarConstraintsOpen,
    addConstraintFromSidebar,
    hasActiveChains: activeChainInfos.length > 0,
    selectedContactConstraintIdSet,
    jumpToConstraint,
    constraintLabel,
    formatConstraintCombo,
    formatConstraintDetail,
    canEnableAffinityFromWorkspace,
    setAffinityEnabledFromWorkspace,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    setAffinityComponentFromWorkspace,
    affinityEnableDisabledReason,
    showAffinityComputeToggle: !isPeptideDesignWorkflow
  });
  const {
    projectResultsSectionProps,
    affinityWorkflowSectionProps,
    leadOptimizationWorkflowSectionProps,
    predictionWorkflowSectionProps,
    workflowRuntimeSettingsSectionProps
  } = useProjectWorkflowSectionProps({
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isAffinityWorkflow,
    workflowTitle: workflow.title,
    workflowShortTitle: workflow.shortTitle,
    projectTaskState: displayTaskState || project.task_state || '',
    projectTaskId:
      String(statusContextTaskRow?.task_id || '').trim() ||
      String(activeResultTask?.task_id || '').trim() ||
      String(project.task_id || '').trim(),
    statusInfo: statusInfo || null,
    progressPercent,
    onPeptideRequestStructure: async () => {
      const contextTask = statusContextTaskRow || activeResultTask;
      const taskId = String(contextTask?.task_id || project.task_id || '').trim();
      if (!taskId) return;
      await pullResultForViewer(taskId, {
        taskRowId: contextTask?.id || undefined,
        persistProject: String(project.task_id || '').trim() === taskId,
        resultMode: 'view'
      });
    },
    resultsGridRef,
    isResultsResizing,
    resultsGridStyle,
    onResultsResizerPointerDown: handleResultsResizerPointerDown,
    onResultsResizerKeyDown: handleResultsResizerKeyDown,
    snapshotCards,
    snapshotConfidence: snapshotConfidence || null,
    resultChainIds,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    displayStructureText,
    displayStructureFormat,
    displayStructureColorMode,
    displayStructureName,
    confidenceBackend,
    projectBackend,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    hasAffinityDisplayStructure,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    affinityResultLigandSmiles,
    affinityTargetChainIds,
    affinityLigandChainId,
    snapshotLigandAtomPlddts,
    snapshotPlddt,
    snapshotIptm,
    snapshotSelectedPairIptm,
    selectedResultLigandSequence,
    canEdit,
    submitting,
    affinityTargetFileName: affinityTargetFile?.name || '',
    affinityLigandFileName: affinityLigandFile?.name || '',
    affinityLigandSmiles,
    affinityPreviewLigandSmiles: String(affinityPreview?.ligandSmiles || ''),
    affinityUseMsa,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    affinityHasLigand,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityUseMsaChange,
    onAffinityConfidenceOnlyChange,
    setAffinityLigandSmiles,
    leadOptProteinSequence: leadOptPrimary.proteinSequence,
    leadOptLigandSmiles: leadOptPrimary.ligandSmiles,
    leadOptTargetChain: leadOptWorkspaceTargetChain,
    leadOptLigandChain: leadOptWorkspaceLigandChain,
    leadOptReferenceScopeKey: `${project.id}:leadopt`,
    leadOptPersistedReferenceUploads: leadOptPersistedUploads,
    onLeadOptReferenceUploadsChange: handleLeadOptReferenceUploadsChange,
    onLeadOptMmpTaskQueued: handleLeadOptMmpTaskQueued,
    onLeadOptMmpTaskCompleted: handleLeadOptMmpTaskCompleted,
    onLeadOptMmpTaskFailed: handleLeadOptMmpTaskFailed,
    onLeadOptUiStateChange: handleLeadOptUiStateChange,
    onLeadOptPredictionQueued: handleLeadOptPredictionQueued,
    onLeadOptPredictionStateChange: handleLeadOptPredictionStateChange,
    onLeadOptNavigateToResults: () => {},
    leadOptInitialMmpSnapshot,
    setDraft,
    setWorkspaceTab,
    onRegisterLeadOptHeaderRunAction: handleRegisterLeadOptHeaderRunAction,
    workspaceTab,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    onComponentsResizerPointerDown: handleComponentsResizerPointerDown,
    onComponentsResizerKeyDown: handleComponentsResizerKeyDown,
    components: draft.inputConfig.components,
    onComponentsChange: handlePredictionComponentsChange,
    proteinTemplates,
    onProteinTemplateChange: handlePredictionProteinTemplateChange,
    activeComponentId,
    setActiveComponentId,
    onProteinTemplateResiduePick: handlePredictionTemplateResiduePick,
    predictionConstraintsWorkspaceProps,
    predictionComponentsSidebarProps,
    backend: draft.backend,
    seed: draft.inputConfig.options.seed ?? null,
    peptideDesignMode: draft.inputConfig.options.peptideDesignMode ?? 'cyclic',
    peptideBinderLength: draft.inputConfig.options.peptideBinderLength ?? 20,
    peptideUseInitialSequence: draft.inputConfig.options.peptideUseInitialSequence ?? false,
    peptideInitialSequence: draft.inputConfig.options.peptideInitialSequence ?? '',
    peptideSequenceMask:
      draft.inputConfig.options.peptideSequenceMask ??
      'X'.repeat(Math.max(1, draft.inputConfig.options.peptideBinderLength ?? 20)),
    peptideIterations: draft.inputConfig.options.peptideIterations ?? 12,
    peptidePopulationSize: draft.inputConfig.options.peptidePopulationSize ?? 16,
    peptideEliteSize: draft.inputConfig.options.peptideEliteSize ?? 5,
    peptideMutationRate: draft.inputConfig.options.peptideMutationRate ?? 0.25,
    peptideBicyclicLinkerCcd: draft.inputConfig.options.peptideBicyclicLinkerCcd ?? 'SEZ',
    peptideBicyclicCysPositionMode: draft.inputConfig.options.peptideBicyclicCysPositionMode ?? 'auto',
    peptideBicyclicFixTerminalCys: draft.inputConfig.options.peptideBicyclicFixTerminalCys ?? true,
    peptideBicyclicIncludeExtraCys: draft.inputConfig.options.peptideBicyclicIncludeExtraCys ?? false,
    peptideBicyclicCys1Pos: draft.inputConfig.options.peptideBicyclicCys1Pos ?? 3,
    peptideBicyclicCys2Pos: draft.inputConfig.options.peptideBicyclicCys2Pos ?? 8,
    peptideBicyclicCys3Pos:
      draft.inputConfig.options.peptideBicyclicCys3Pos ??
      (draft.inputConfig.options.peptideBinderLength ?? 20),
    onBackendChange: handleRuntimeBackendChange,
    onSeedChange: handleRuntimeSeedChange,
    onPeptideDesignModeChange: handleRuntimePeptideDesignModeChange,
    onPeptideBinderLengthChange: handleRuntimePeptideBinderLengthChange,
    onPeptideUseInitialSequenceChange: handleRuntimePeptideUseInitialSequenceChange,
    onPeptideInitialSequenceChange: handleRuntimePeptideInitialSequenceChange,
    onPeptideSequenceMaskChange: handleRuntimePeptideSequenceMaskChange,
    onPeptideIterationsChange: handleRuntimePeptideIterationsChange,
    onPeptidePopulationSizeChange: handleRuntimePeptidePopulationSizeChange,
    onPeptideEliteSizeChange: handleRuntimePeptideEliteSizeChange,
    onPeptideMutationRateChange: handleRuntimePeptideMutationRateChange,
    onPeptideBicyclicLinkerCcdChange: handleRuntimePeptideBicyclicLinkerCcdChange,
    onPeptideBicyclicCysPositionModeChange: handleRuntimePeptideBicyclicCysPositionModeChange,
    onPeptideBicyclicFixTerminalCysChange: handleRuntimePeptideBicyclicFixTerminalCysChange,
    onPeptideBicyclicIncludeExtraCysChange: handleRuntimePeptideBicyclicIncludeExtraCysChange,
    onPeptideBicyclicCys1PosChange: handleRuntimePeptideBicyclicCys1PosChange,
    onPeptideBicyclicCys2PosChange: handleRuntimePeptideBicyclicCys2PosChange,
    onPeptideBicyclicCys3PosChange: handleRuntimePeptideBicyclicCys3PosChange
  });
  const taskHistoryPath = `/projects/${project.id}/tasks`;
  const {
    handleRunAction,
    handleRunCurrentDraft,
    handleRestoreSavedDraft,
    handleResetFromHeader,
    handleWorkspaceFormSubmit,
    handleOpenTaskHistory,
  } = useProjectRunHandlers({
    runDisabled,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saving,
    submitting,
    loading,
    hasUnsavedChanges,
    saveDraft,
    taskHistoryPath,
    setRunRedirectTaskId,
    navigate,
  });

  const leadOptHeaderActionMissing = isLeadOptimizationWorkflow && !leadOptHeaderRunAction;
  const effectiveRunDisabled = runDisabled || leadOptHeaderActionMissing;
  const effectiveRunBlockedReason = leadOptHeaderActionMissing
    ? workspaceTab === 'components'
      ? 'Select at least one fragment to run.'
      : 'Run action is only available in Lead Optimization Components view.'
    : runBlockedReason;
  const handleHeaderRunAction = () => {
    if (isLeadOptimizationWorkflow) {
      if (!leadOptHeaderRunAction || leadOptHeaderRunPending) return;
      setLeadOptHeaderRunPending(true);
      void Promise.resolve(leadOptHeaderRunAction())
        .catch(() => {
          // Lead opt workspace already surfaces query errors.
        })
        .finally(() => {
          setLeadOptHeaderRunPending(false);
        });
      return;
    }
    handleRunAction();
  };

  return (
    <ProjectDetailLayout
      projectName={project.name}
      canDownloadResult={Boolean(
        readText(structureTaskId).trim() ||
          (readText(activeResultTask?.structure_name).trim() && readText(activeResultTask?.task_id).trim()) ||
          (!isLeadOptimizationWorkflow && readText(project.task_id).trim())
      )}
      workflow={{
        shortTitle: workflow.shortTitle,
        runLabel: workflow.runLabel,
        description: workflow.description
      }}
      workspaceTab={workspaceTab}
      componentStepLabel={componentStepLabel}
      taskName={draft.taskName}
      taskSummary={draft.taskSummary}
      isPredictionWorkflow={isPredictionWorkflow}
      isAffinityWorkflow={isAffinityWorkflow}
      isLeadOptimizationWorkflow={isLeadOptimizationWorkflow}
      displayTaskState={displayTaskState}
      isActiveRuntime={isActiveRuntime}
      progressPercent={progressPercent}
      waitingSeconds={waitingSeconds}
      totalRuntimeSeconds={totalRuntimeSeconds}
      canEdit={canEdit}
      loading={loading}
      saving={saving}
      submitting={submitting}
      runSubmitting={runSubmitting}
      hasUnsavedChanges={hasUnsavedChanges}
      runMenuOpen={runMenuOpen}
      runDisabled={effectiveRunDisabled}
      runBlockedReason={effectiveRunBlockedReason}
      isRunRedirecting={isRunRedirecting}
      canOpenRunMenu={canOpenRunMenu}
      showHeaderRunAction
      showQuickRunFab={showQuickRunFab}
      taskHistoryPath={taskHistoryPath}
      runSuccessNotice={runSuccessNotice}
      error={error}
      resultError={resultError}
      affinityPreviewError={affinityPreviewError}
      resultChainConsistencyWarning={resultChainConsistencyWarning}
      projectResultsSectionProps={projectResultsSectionProps}
      affinitySectionProps={affinityWorkflowSectionProps}
      leadOptimizationSectionProps={leadOptimizationWorkflowSectionProps}
      predictionSectionProps={predictionWorkflowSectionProps}
      runtimeSettingsProps={workflowRuntimeSettingsSectionProps}
      runActionRef={runActionRef as RefObject<HTMLDivElement>}
      topRunButtonRef={topRunButtonRef as RefObject<HTMLButtonElement>}
      onOpenTaskHistory={handleOpenTaskHistory}
      onDownloadResult={() => {
        const viewerTaskId = readText(structureTaskId).trim();
        const activeTaskId = readText(activeResultTask?.task_id).trim();
        const activeStructureName = readText(activeResultTask?.structure_name).trim();
        const projectTaskId = readText(project.task_id).trim();
        const downloadTaskId =
          viewerTaskId ||
          (activeStructureName ? activeTaskId : '') ||
          (isLeadOptimizationWorkflow ? '' : projectTaskId);
        if (!downloadTaskId) return;
        setError(null);
        void downloadResultFile(downloadTaskId).catch((err) => {
          setError(err instanceof Error ? err.message : 'Failed to download result archive.');
        });
      }}
      onSaveDraft={() => {
        void saveDraft();
      }}
      onReset={handleResetFromHeader}
      onRunAction={handleHeaderRunAction}
      onRestoreSavedDraft={handleRestoreSavedDraft}
      onRunCurrentDraft={handleRunCurrentDraft}
      onWorkspaceTabChange={setWorkspaceTab}
      onTaskNameChange={handleTaskNameChange}
      onTaskSummaryChange={handleTaskSummaryChange}
      onWorkspaceFormSubmit={handleWorkspaceFormSubmit}
    />
  );
}
