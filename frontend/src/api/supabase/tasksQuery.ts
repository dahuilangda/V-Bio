import { applyTaskAccess, buildEditableTaskIdSet } from './shared';
import { EffectiveAccessLevel, ProjectAccessScope, ProjectTask } from '../../types/models';
import { PEPTIDE_TASK_PREVIEW_KEY } from '../../utils/peptideTaskPreview';
import { asObjectRecord, buildInFilter, buildSupabaseUrlCandidates, compactObjectRecord, fetchWithTimeout, parseBooleanToken, request } from './rest';



// 任务列表批量查询（分页与行合并）。


interface ProjectTaskAccessOptions {
  taskRowIds?: string[];
  accessScope?: ProjectAccessScope;
  accessLevel?: EffectiveAccessLevel;
  editableTaskIds?: string[];
}

// Exact task-row count in one request (Prefer: count=exact + limit=1 → Content-Range);
// throws on failure rather than letting callers substitute an estimate.
export async function countProjectTasks(
  projectId: string,
  options?: ProjectTaskAccessOptions
): Promise<number> {
  const taskIdFilter = buildInFilter(options?.taskRowIds || []);
  const candidates = buildSupabaseUrlCandidates('/project_tasks', {
    select: 'id',
    project_id: `eq.${projectId}`,
    ...(taskIdFilter ? { id: taskIdFilter } : {}),
    limit: '1'
  });
  let lastFailure: Error | null = null;
  for (const url of candidates) {
    try {
      const res = await fetchWithTimeout(url, {
        method: 'GET',
        headers: { 'Prefer': 'count=exact' }
      });
      if (!res.ok) {
        lastFailure = new Error(`Task count request failed (${res.status}).`);
        continue;
      }
      const range = res.headers.get('Content-Range') || '';
      const total = Number.parseInt(range.split('/')[1] || '', 10);
      if (Number.isFinite(total)) return total;
      lastFailure = new Error('Task count response did not include a usable Content-Range header.');
    } catch (error) {
      lastFailure = error instanceof Error ? error : new Error('Unknown network error');
    }
  }
  throw lastFailure || new Error('Task count request failed.');
}

export async function listProjectTasksCompact(
  projectId: string,
  options?: ProjectTaskAccessOptions
): Promise<ProjectTask[]> {
  const selectFields = [
    'id',
    'project_id',
    'name',
    'summary',
    'task_id',
    'task_state',
    'status_text',
    'error_text',
    'backend',
    'seed',
    'ligand_smiles',
    'properties',
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const taskIdFilter = buildInFilter(options?.taskRowIds || []);
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
    select: selectFields,
    project_id: `eq.${projectId}`,
    ...(taskIdFilter ? { id: taskIdFilter } : {}),
    order: 'created_at.desc'
  });

  const scope = options?.accessScope || 'owner';
  const accessLevel = options?.accessLevel || (scope === 'owner' ? 'owner' : 'viewer');
  const editableTaskIdSet = buildEditableTaskIdSet(options?.editableTaskIds || []);
  return rows.map((row) => applyTaskAccess({
    name: '',
    summary: '',
    protein_sequence: '',
    affinity: {},
    confidence: {},
    components: [],
    constraints: [],
    properties: {
      affinity: false,
      target: null,
      ligand: null,
      binder: null
    },
    ...row
  } as ProjectTask, scope, accessLevel, editableTaskIdSet));
}

export async function listProjectTasksForList(
  projectId: string,
  options?: {
    includeComponents?: boolean;
    includeConfidence?: boolean;
    includeConfidenceSummary?: boolean;
    includeProperties?: boolean;
    includePropertiesSummary?: boolean;
    includeLeadOptSummary?: boolean;
    includeLeadOptCandidates?: boolean;
    taskRowIds?: string[];
    accessScope?: ProjectAccessScope;
    accessLevel?: EffectiveAccessLevel;
    editableTaskIds?: string[];
    limit?: number;
    offset?: number;
  }
): Promise<ProjectTask[]> {
  const includeComponents = options?.includeComponents !== false;
  const includeConfidence = options?.includeConfidence !== false;
  const includeConfidenceSummary = !includeConfidence && options?.includeConfidenceSummary === true;
  const includeResidueConfidenceSummary = includeConfidenceSummary && includeComponents;
  const includeProperties = options?.includeProperties !== false;
  const includePropertiesSummary = !includeProperties && options?.includePropertiesSummary === true;
  const includeLeadOptSummary = options?.includeLeadOptSummary === true;
  const includeLeadOptCandidates = options?.includeLeadOptCandidates === true;
  const taskIdFilter = buildInFilter(options?.taskRowIds || []);
  const limit = Number(options?.limit);
  const offset = Number(options?.offset);
  const paginationQuery = {
    ...(Number.isFinite(limit) && limit > 0 ? { limit: String(Math.floor(limit)) } : {}),
    ...(Number.isFinite(offset) && offset > 0 ? { offset: String(Math.floor(offset)) } : {})
  };
  const selectFields = [
    'id',
    'project_id',
    'name',
    'summary',
    'task_id',
    'task_state',
    'status_text',
    'error_text',
    'backend',
    'seed',
    'ligand_smiles',
    // small scalar jsonb; the Excel export reads it from loaded rows without opening archives
    'affinity',
    ...(includePropertiesSummary
      ? [
          'properties_target:properties->>target',
          'properties_ligand:properties->>ligand',
          'properties_binder:properties->>binder',
          'properties_affinity_mode:properties->__vbio_input_options_v1->>affinityMode',
          'properties_peptide_preview_design_mode:properties->peptide_preview->>design_mode',
          'properties_peptide_preview_binder_length:properties->peptide_preview->>binder_length',
          'properties_peptide_preview_iterations:properties->peptide_preview->>iterations',
          'properties_peptide_preview_population_size:properties->peptide_preview->>population_size',
          'properties_peptide_preview_elite_size:properties->peptide_preview->>elite_size',
          'properties_peptide_preview_mutation_rate:properties->peptide_preview->>mutation_rate',
          'properties_peptide_preview_current_generation:properties->peptide_preview->>current_generation',
          'properties_peptide_preview_total_generations:properties->peptide_preview->>total_generations',
          'properties_peptide_preview_best_score:properties->peptide_preview->>best_score',
          'properties_peptide_preview_candidate_count:properties->peptide_preview->>candidate_count',
          'properties_peptide_preview_completed_tasks:properties->peptide_preview->>completed_tasks',
          'properties_peptide_preview_pending_tasks:properties->peptide_preview->>pending_tasks',
          'properties_peptide_preview_total_tasks:properties->peptide_preview->>total_tasks',
          'properties_peptide_preview_current_status:properties->peptide_preview->>current_status',
          'properties_peptide_preview_status_message:properties->peptide_preview->>status_message',
          'properties_peptide_preview_best_candidate:properties->peptide_preview->best_candidate'
        ]
      : []),
    ...(includeProperties ? ['properties'] : []),
    ...(includeConfidenceSummary
      ? [
          'confidence_ligand_smiles:confidence->>ligand_smiles',
          'confidence_ligand_display_smiles:confidence->>ligand_display_smiles',
          'confidence_requested_target_chain_id:confidence->>requested_target_chain_id',
          'confidence_target_chain_id:confidence->>target_chain_id',
          'confidence_target_chain:confidence->>target_chain',
          'confidence_protein_chain_id:confidence->>protein_chain_id',
          'confidence_requested_ligand_chain_id:confidence->>requested_ligand_chain_id',
          'confidence_ligand_chain_id:confidence->>ligand_chain_id',
          'confidence_model_ligand_chain_id:confidence->>model_ligand_chain_id',
          'confidence_binder_chain_id:confidence->>binder_chain_id',
          'confidence_chain_ids:confidence->chain_ids',
          'confidence_chain_mean_plddt:confidence->chain_mean_plddt',
          'confidence_pair_chains_iptm:confidence->pair_chains_iptm',
          'confidence_chain_pair_iptm:confidence->chain_pair_iptm',
          'confidence_chain_pair_iptm_global:confidence->chain_pair_iptm_global',
          'confidence_ligand_display_atom_plddts_by_chain:confidence->ligand_display_atom_plddts_by_chain',
          'confidence_ligand_atom_plddts_by_chain:confidence->ligand_atom_plddts_by_chain',
          ...(includeResidueConfidenceSummary
            ? [
                'confidence_residue_plddt_by_chain:confidence->residue_plddt_by_chain',
                'confidence_residuePlddtByChain:confidence->residuePlddtByChain',
                'confidence_residue_plddts_by_chain:confidence->residue_plddts_by_chain',
                'confidence_chain_residue_plddt:confidence->chain_residue_plddt',
                'confidence_plddt_by_chain:confidence->plddt_by_chain'
              ]
            : []),
          'confidence_ligand_display_atom_plddts:confidence->ligand_display_atom_plddts',
          'confidence_ligand_atom_plddts:confidence->ligand_atom_plddts',
          'confidence_ligand_plddt:confidence->>ligand_plddt',
          'confidence_ligand_mean_plddt:confidence->>ligand_mean_plddt',
          'confidence_complex_iplddt:confidence->>complex_iplddt',
          'confidence_complex_plddt_protein:confidence->>complex_plddt_protein',
          'confidence_complex_plddt:confidence->>complex_plddt',
          'confidence_plddt:confidence->>plddt',
          'confidence_ipsae_dom:confidence->>ipsae_dom',
          'confidence_ligand_ipsae_max:confidence->>ligand_ipsae_max',
          'confidence_interface_metric:confidence->>interface_metric',
          'confidence_interface_metric_value:confidence->>interface_metric_value',
          'confidence_interface_metric_label:confidence->>interface_metric_label',
          'confidence_interface_metric_source:confidence->>interface_metric_source',
          'confidence_interfaceMetricValue:confidence->>interfaceMetricValue',
          'confidence_interfaceMetricLabel:confidence->>interfaceMetricLabel',
          'confidence_interfaceMetricSource:confidence->>interfaceMetricSource',
          'confidence_iptm:confidence->>iptm',
          'confidence_ligand_iptm:confidence->>ligand_iptm',
          'confidence_protein_iptm:confidence->>protein_iptm',
          'confidence_complex_pde:confidence->>complex_pde',
          'confidence_complex_pae:confidence->>complex_pae',
          'confidence_gpde:confidence->>gpde',
          'confidence_pae:confidence->>pae'
        ]
      : []),
    ...(includeConfidence ? ['confidence'] : []),
    ...(includeLeadOptSummary
      ? [
          'lead_opt_list_stage:properties->lead_opt_list->>stage',
          'lead_opt_list_prediction_stage:properties->lead_opt_list->>prediction_stage',
          'lead_opt_list_query_id:properties->lead_opt_list->>query_id',
          'lead_opt_list_transform_count:properties->lead_opt_list->>transform_count',
          'lead_opt_list_candidate_count:properties->lead_opt_list->>candidate_count',
          'lead_opt_list_bucket_count:properties->lead_opt_list->>bucket_count',
          'lead_opt_list_database_id:properties->lead_opt_list->>mmp_database_id',
          'lead_opt_list_database_label:properties->lead_opt_list->>mmp_database_label',
          'lead_opt_list_database_schema:properties->lead_opt_list->>mmp_database_schema',
          'lead_opt_list_selected_fragment_ids:properties->lead_opt_list->selection->selected_fragment_ids',
          'lead_opt_list_selected_atom_indices:properties->lead_opt_list->selection->selected_fragment_atom_indices',
          'lead_opt_list_selected_fragment_query:properties->lead_opt_list->>selected_fragment_query',
          'lead_opt_list_prediction_total:properties->lead_opt_list->prediction_summary->>total',
          'lead_opt_list_prediction_queued:properties->lead_opt_list->prediction_summary->>queued',
          'lead_opt_list_prediction_running:properties->lead_opt_list->prediction_summary->>running',
          'lead_opt_list_prediction_success:properties->lead_opt_list->prediction_summary->>success',
          'lead_opt_list_prediction_failure:properties->lead_opt_list->prediction_summary->>failure',
          'lead_opt_list_selection:properties->lead_opt_list->selection',
          'lead_opt_list_query_result_query_id:properties->lead_opt_list->query_result->>query_id',
          'lead_opt_list_query_result_query_mode:properties->lead_opt_list->query_result->>query_mode',
          'lead_opt_list_query_result_aggregation_type:properties->lead_opt_list->query_result->>aggregation_type',
          'lead_opt_list_query_result_property_targets:properties->lead_opt_list->query_result->property_targets',
          'lead_opt_list_query_result_rule_env_radius:properties->lead_opt_list->query_result->>rule_env_radius',
          'lead_opt_list_query_result_grouped_by_environment:properties->lead_opt_list->query_result->>grouped_by_environment',
          'lead_opt_list_query_result_cluster_group_by:properties->lead_opt_list->query_result->>cluster_group_by',
          'lead_opt_list_query_result_min_pairs:properties->lead_opt_list->query_result->>min_pairs',
          'lead_opt_list_query_result_count:properties->lead_opt_list->query_result->>count',
          'lead_opt_list_query_result_global_count:properties->lead_opt_list->query_result->>global_count',
          'lead_opt_list_query_result_stats:properties->lead_opt_list->query_result->stats',
          'lead_opt_list_query_result_database_id:properties->lead_opt_list->query_result->>mmp_database_id',
          'lead_opt_list_query_result_database_label:properties->lead_opt_list->query_result->>mmp_database_label',
          'lead_opt_list_query_result_database_schema:properties->lead_opt_list->query_result->>mmp_database_schema',
          ...(includeLeadOptCandidates
            ? ['lead_opt_list_enumerated_candidates:properties->lead_opt_list->enumerated_candidates']
            : []),
          'lead_opt_state_stage:properties->lead_opt_state->>stage',
          'lead_opt_state_prediction_stage:properties->lead_opt_state->>prediction_stage',
          'lead_opt_state_query_id:properties->lead_opt_state->>query_id',
          'lead_opt_state_prediction_total:properties->lead_opt_state->prediction_summary->>total',
          'lead_opt_state_prediction_queued:properties->lead_opt_state->prediction_summary->>queued',
          'lead_opt_state_prediction_running:properties->lead_opt_state->prediction_summary->>running',
          'lead_opt_state_prediction_success:properties->lead_opt_state->prediction_summary->>success',
          'lead_opt_state_prediction_failure:properties->lead_opt_state->prediction_summary->>failure',
          'lead_opt_state_prediction_task_id:properties->lead_opt_state->>prediction_task_id',
          'lead_opt_state_prediction_candidate_smiles:properties->lead_opt_state->>prediction_candidate_smiles',
          'lead_opt_state_prediction_by_smiles:properties->lead_opt_state->prediction_by_smiles',
          'lead_opt_state_reference_prediction_by_backend:properties->lead_opt_state->reference_prediction_by_backend',
        ]
      : []),
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const rowsQuery = {
    select: selectFields,
    project_id: `eq.${projectId}`,
    ...(taskIdFilter ? { id: taskIdFilter } : {}),
    order: 'created_at.desc',
    ...paginationQuery
  } as const;
  // 两个独立查询并行，降低列表首屏延迟。
  const [rows, detailRows] = await Promise.all([
    request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, rowsQuery),
    includeComponents
      ? request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
          select: 'id,components',
          project_id: `eq.${projectId}`,
          ...(taskIdFilter ? { id: taskIdFilter } : {}),
          order: 'created_at.desc',
          ...paginationQuery
        })
      : Promise.resolve([] as Array<Partial<ProjectTask>>)
  ]);
  const detailById = new Map(
    detailRows
      .map((row) => [String(row.id || '').trim(), row] as const)
      .filter(([id]) => Boolean(id))
  );

  const readText = (value: unknown): string => {
    if (value === null || value === undefined) return '';
    return String(value).trim();
  };

  const readFiniteNumber = (value: unknown): number | null => {
    const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : Number.NaN;
    if (!Number.isFinite(parsed)) return null;
    return parsed;
  };

  const readStringArray = (value: unknown): string[] => {
    if (!Array.isArray(value)) return [];
    return Array.from(
      new Set(
        value
          .map((item) => String(item || '').trim())
          .filter(Boolean)
      )
    );
  };

  const readIntegerArray = (value: unknown): number[] => {
    if (!Array.isArray(value)) return [];
    return Array.from(
      new Set(
        value
          .map((item) => Number(item))
          .filter((item) => Number.isFinite(item) && item >= 0)
          .map((item) => Math.floor(item))
      )
    );
  };

  const buildLeadOptSummaryProperties = (row: Record<string, unknown>): Record<string, unknown> | null => {
    const listStage = readText(row.lead_opt_list_stage);
    const listPredictionStage = readText(row.lead_opt_list_prediction_stage);
    const listQueryId = readText(row.lead_opt_list_query_id);
    const listTransformCount = readFiniteNumber(row.lead_opt_list_transform_count);
    const listCandidateCount = readFiniteNumber(row.lead_opt_list_candidate_count);
    const listBucketCount = readFiniteNumber(row.lead_opt_list_bucket_count);
    const listDatabaseId = readText(row.lead_opt_list_database_id);
    const listDatabaseLabel = readText(row.lead_opt_list_database_label);
    const listDatabaseSchema = readText(row.lead_opt_list_database_schema);
    const listSelectedFragmentIds = readStringArray(row.lead_opt_list_selected_fragment_ids);
    const listSelectedAtomIndices = readIntegerArray(row.lead_opt_list_selected_atom_indices);
    const listSelectedFragmentQuery = readText(row.lead_opt_list_selected_fragment_query);

    const listPredictionTotal = readFiniteNumber(row.lead_opt_list_prediction_total);
    const listPredictionQueued = readFiniteNumber(row.lead_opt_list_prediction_queued);
    const listPredictionRunning = readFiniteNumber(row.lead_opt_list_prediction_running);
    const listPredictionSuccess = readFiniteNumber(row.lead_opt_list_prediction_success);
    const listPredictionFailure = readFiniteNumber(row.lead_opt_list_prediction_failure);
    const listSelection =
      row.lead_opt_list_selection && typeof row.lead_opt_list_selection === 'object' && !Array.isArray(row.lead_opt_list_selection)
        ? (row.lead_opt_list_selection as Record<string, unknown>)
        : {};
    const listQueryResultGroupedByEnvironment = parseBooleanToken(row.lead_opt_list_query_result_grouped_by_environment);
    const listQueryResult = compactObjectRecord({
      query_id: readText(row.lead_opt_list_query_result_query_id) || listQueryId,
      query_mode: readText(row.lead_opt_list_query_result_query_mode),
      aggregation_type: readText(row.lead_opt_list_query_result_aggregation_type),
      property_targets: asObjectRecord(row.lead_opt_list_query_result_property_targets),
      rule_env_radius: readFiniteNumber(row.lead_opt_list_query_result_rule_env_radius),
      grouped_by_environment:
        listQueryResultGroupedByEnvironment === null ? undefined : listQueryResultGroupedByEnvironment,
      cluster_group_by: readText(row.lead_opt_list_query_result_cluster_group_by),
      min_pairs: readFiniteNumber(row.lead_opt_list_query_result_min_pairs),
      count: readFiniteNumber(row.lead_opt_list_query_result_count),
      global_count: readFiniteNumber(row.lead_opt_list_query_result_global_count),
      stats: asObjectRecord(row.lead_opt_list_query_result_stats),
      mmp_database_id:
        readText(row.lead_opt_list_query_result_database_id) || listDatabaseId,
      mmp_database_label:
        readText(row.lead_opt_list_query_result_database_label) || listDatabaseLabel,
      mmp_database_schema:
        readText(row.lead_opt_list_query_result_database_schema) || listDatabaseSchema
    });
    const listEnumeratedCandidates =
      includeLeadOptCandidates && Array.isArray(row.lead_opt_list_enumerated_candidates)
        ? row.lead_opt_list_enumerated_candidates
        : [];

    const stateStage = readText(row.lead_opt_state_stage);
    const statePredictionStage = readText(row.lead_opt_state_prediction_stage);
    const stateQueryId = readText(row.lead_opt_state_query_id);
    const statePredictionTotal = readFiniteNumber(row.lead_opt_state_prediction_total);
    const statePredictionQueued = readFiniteNumber(row.lead_opt_state_prediction_queued);
    const statePredictionRunning = readFiniteNumber(row.lead_opt_state_prediction_running);
    const statePredictionSuccess = readFiniteNumber(row.lead_opt_state_prediction_success);
    const statePredictionFailure = readFiniteNumber(row.lead_opt_state_prediction_failure);
    const statePredictionTaskId = readText(row.lead_opt_state_prediction_task_id);
    const statePredictionCandidateSmiles = readText(row.lead_opt_state_prediction_candidate_smiles);
    const statePredictionBySmiles =
      row.lead_opt_state_prediction_by_smiles &&
      typeof row.lead_opt_state_prediction_by_smiles === 'object' &&
      !Array.isArray(row.lead_opt_state_prediction_by_smiles)
        ? (row.lead_opt_state_prediction_by_smiles as Record<string, unknown>)
        : {};
    const stateReferencePredictionByBackend =
      row.lead_opt_state_reference_prediction_by_backend &&
      typeof row.lead_opt_state_reference_prediction_by_backend === 'object' &&
      !Array.isArray(row.lead_opt_state_reference_prediction_by_backend)
        ? (row.lead_opt_state_reference_prediction_by_backend as Record<string, unknown>)
        : {};

    const hasAnyLeadOptMeta = Boolean(
      listStage ||
        listPredictionStage ||
        listQueryId ||
        listDatabaseId ||
        listDatabaseLabel ||
        listDatabaseSchema ||
        stateStage ||
        statePredictionStage ||
        stateQueryId ||
        listTransformCount !== null ||
        listCandidateCount !== null ||
        listBucketCount !== null ||
        listPredictionTotal !== null ||
        listPredictionQueued !== null ||
        listPredictionRunning !== null ||
        listPredictionSuccess !== null ||
        listPredictionFailure !== null ||
        statePredictionTotal !== null ||
        statePredictionQueued !== null ||
        statePredictionRunning !== null ||
        statePredictionSuccess !== null ||
        statePredictionFailure !== null ||
        statePredictionTaskId ||
        statePredictionCandidateSmiles ||
        Object.keys(listSelection).length > 0 ||
        Object.keys(listQueryResult).length > 0 ||
        listEnumeratedCandidates.length > 0 ||
        Object.keys(statePredictionBySmiles).length > 0 ||
        Object.keys(stateReferencePredictionByBackend).length > 0
    );
    if (!hasAnyLeadOptMeta) return null;

    const normalizedPredictionSummary = {
      total: statePredictionTotal ?? listPredictionTotal ?? 0,
      queued: statePredictionQueued ?? listPredictionQueued ?? 0,
      running: statePredictionRunning ?? listPredictionRunning ?? 0,
      success: statePredictionSuccess ?? listPredictionSuccess ?? 0,
      failure: statePredictionFailure ?? listPredictionFailure ?? 0,
    };

    return {
      lead_opt_list: {
        stage: listStage,
        prediction_stage: listPredictionStage || statePredictionStage,
        query_id: listQueryId || stateQueryId,
        transform_count: listTransformCount,
        candidate_count: listCandidateCount,
        bucket_count: listBucketCount,
        mmp_database_id: listDatabaseId,
        mmp_database_label: listDatabaseLabel,
        mmp_database_schema: listDatabaseSchema,
        selection:
          Object.keys(listSelection).length > 0
            ? listSelection
            : {
                selected_fragment_ids: listSelectedFragmentIds,
                selected_fragment_atom_indices: listSelectedAtomIndices,
                variable_queries: listSelectedFragmentQuery ? [listSelectedFragmentQuery] : []
              },
        selected_fragment_ids: listSelectedFragmentIds,
        selected_fragment_atom_indices: listSelectedAtomIndices,
        selected_fragment_query: listSelectedFragmentQuery,
        prediction_summary: normalizedPredictionSummary,
        query_result: listQueryResult,
        ui_state: {},
        enumerated_candidates: listEnumeratedCandidates
      },
      lead_opt_state: {
        stage: stateStage || listStage,
        prediction_stage: statePredictionStage || listPredictionStage,
        query_id: stateQueryId || listQueryId,
        prediction_task_id: statePredictionTaskId,
        prediction_candidate_smiles: statePredictionCandidateSmiles,
        prediction_summary: normalizedPredictionSummary,
        prediction_by_smiles: statePredictionBySmiles,
        reference_prediction_by_backend: stateReferencePredictionByBackend
      }
    };
  };

  const mergedRows = rows.map((row) => {
    const detail = detailById.get(String(row.id || '').trim()) || {};
    const rowRecord = row as unknown as Record<string, unknown>;
    const leadOptSummaryProperties = includeLeadOptSummary ? buildLeadOptSummaryProperties(rowRecord) : null;
    const summaryProperties = includePropertiesSummary
      ? (() => {
          const peptidePreview = compactObjectRecord({
            design_mode: readText(rowRecord.properties_peptide_preview_design_mode),
            binder_length: readFiniteNumber(rowRecord.properties_peptide_preview_binder_length),
            iterations: readFiniteNumber(rowRecord.properties_peptide_preview_iterations),
            population_size: readFiniteNumber(rowRecord.properties_peptide_preview_population_size),
            elite_size: readFiniteNumber(rowRecord.properties_peptide_preview_elite_size),
            mutation_rate: readFiniteNumber(rowRecord.properties_peptide_preview_mutation_rate),
            current_generation: readFiniteNumber(rowRecord.properties_peptide_preview_current_generation),
            total_generations: readFiniteNumber(rowRecord.properties_peptide_preview_total_generations),
            best_score: readFiniteNumber(rowRecord.properties_peptide_preview_best_score),
            candidate_count: readFiniteNumber(rowRecord.properties_peptide_preview_candidate_count),
            completed_tasks: readFiniteNumber(rowRecord.properties_peptide_preview_completed_tasks),
            pending_tasks: readFiniteNumber(rowRecord.properties_peptide_preview_pending_tasks),
            total_tasks: readFiniteNumber(rowRecord.properties_peptide_preview_total_tasks),
            current_status: readText(rowRecord.properties_peptide_preview_current_status),
            status_message: readText(rowRecord.properties_peptide_preview_status_message),
            best_candidate:
              rowRecord.properties_peptide_preview_best_candidate &&
              typeof rowRecord.properties_peptide_preview_best_candidate === 'object' &&
              !Array.isArray(rowRecord.properties_peptide_preview_best_candidate)
                ? (rowRecord.properties_peptide_preview_best_candidate as Record<string, unknown>)
                : undefined
          });
          return compactObjectRecord({
            target: readText(rowRecord.properties_target),
            ligand: readText(rowRecord.properties_ligand),
            binder: readText(rowRecord.properties_binder),
            affinity_mode_summary: readText(rowRecord.properties_affinity_mode),
            ...(Object.keys(peptidePreview).length > 0 ? { [PEPTIDE_TASK_PREVIEW_KEY]: peptidePreview } : {})
          });
        })()
      : {};
    const summaryConfidence = (() => {
      if (!includeConfidenceSummary) return {};
      const next: Record<string, unknown> = {};
      const chainMeanPlddt = asObjectRecord(rowRecord.confidence_chain_mean_plddt);
      const pairChainsIptm = asObjectRecord(rowRecord.confidence_pair_chains_iptm);
      const ligandDisplayAtomPlddtsByChain = asObjectRecord(rowRecord.confidence_ligand_display_atom_plddts_by_chain);
      const ligandAtomPlddtsByChain = asObjectRecord(rowRecord.confidence_ligand_atom_plddts_by_chain);
      const firstObjectRecord = (...values: unknown[]): Record<string, unknown> => {
        for (const value of values) {
          const objectValue = asObjectRecord(value);
          if (Object.keys(objectValue).length > 0) return objectValue;
        }
        return {};
      };
      const residuePlddtByChain = firstObjectRecord(
        rowRecord.confidence_residue_plddt_by_chain,
        rowRecord.confidence_residuePlddtByChain,
        rowRecord.confidence_residue_plddts_by_chain,
        rowRecord.confidence_chain_residue_plddt,
        rowRecord.confidence_plddt_by_chain
      );
      const chainIdsFromSummary = Array.from(
        new Set(
          [
            ...(Array.isArray(rowRecord.confidence_chain_ids)
              ? rowRecord.confidence_chain_ids.map((value: unknown) => readText(value)).filter(Boolean)
              : []),
            ...Object.keys(chainMeanPlddt).map((value) => readText(value)).filter(Boolean),
            ...Object.keys(ligandDisplayAtomPlddtsByChain).map((value) => readText(value)).filter(Boolean),
            ...Object.keys(ligandAtomPlddtsByChain).map((value) => readText(value)).filter(Boolean),
            ...Object.keys(residuePlddtByChain).map((value) => readText(value)).filter(Boolean)
          ]
        )
      );
      const explicitLigandChainId =
        readText(rowRecord.confidence_ligand_chain_id) ||
        readText(rowRecord.confidence_model_ligand_chain_id) ||
        readText(rowRecord.confidence_binder_chain_id) ||
        readText(rowRecord.confidence_requested_ligand_chain_id);
      const derivedLigandChainId =
        explicitLigandChainId ||
        (Object.keys(ligandDisplayAtomPlddtsByChain).length === 1
          ? readText(Object.keys(ligandDisplayAtomPlddtsByChain)[0])
          : Object.keys(ligandAtomPlddtsByChain).length === 1
            ? readText(Object.keys(ligandAtomPlddtsByChain)[0])
            : '');
      const explicitTargetChainId =
        readText(rowRecord.confidence_target_chain) ||
        readText(rowRecord.confidence_target_chain_id) ||
        readText(rowRecord.confidence_requested_target_chain_id) ||
        readText(rowRecord.confidence_protein_chain_id);
      const derivedTargetChainId =
        explicitTargetChainId ||
        chainIdsFromSummary.find((chainId) => !derivedLigandChainId || chainId !== derivedLigandChainId) ||
        '';
      const assignText = (key: string, value: unknown) => {
        const text = readText(value);
        if (text) next[key] = text;
      };
      const assignNumber = (key: string, value: unknown) => {
        const numeric = readFiniteNumber(value);
        if (numeric !== null) next[key] = numeric;
      };
      const assignObject = (key: string, value: unknown) => {
        const objectValue = asObjectRecord(value);
        if (Object.keys(objectValue).length > 0) next[key] = objectValue;
      };
      const assignArray = (key: string, value: unknown) => {
        if (Array.isArray(value) && value.length > 0) next[key] = value;
      };

      assignText('ligand_smiles', rowRecord.confidence_ligand_smiles);
      assignText('ligand_display_smiles', rowRecord.confidence_ligand_display_smiles);
      assignText('requested_target_chain_id', rowRecord.confidence_requested_target_chain_id);
      assignText('target_chain_id', rowRecord.confidence_target_chain_id || derivedTargetChainId);
      assignText('target_chain', rowRecord.confidence_target_chain || derivedTargetChainId);
      assignText('protein_chain_id', rowRecord.confidence_protein_chain_id || derivedTargetChainId);
      assignText('requested_ligand_chain_id', rowRecord.confidence_requested_ligand_chain_id || derivedLigandChainId);
      assignText('ligand_chain_id', rowRecord.confidence_ligand_chain_id || derivedLigandChainId);
      assignText('model_ligand_chain_id', rowRecord.confidence_model_ligand_chain_id || derivedLigandChainId);
      assignText('binder_chain_id', rowRecord.confidence_binder_chain_id || derivedLigandChainId);
      assignArray('chain_ids', chainIdsFromSummary);
      assignObject('chain_mean_plddt', chainMeanPlddt);
      assignObject('pair_chains_iptm', pairChainsIptm);
      assignArray('chain_pair_iptm', rowRecord.confidence_chain_pair_iptm);
      assignArray('chain_pair_iptm_global', rowRecord.confidence_chain_pair_iptm_global);
      assignObject('ligand_display_atom_plddts_by_chain', ligandDisplayAtomPlddtsByChain);
      assignObject('ligand_atom_plddts_by_chain', ligandAtomPlddtsByChain);
      assignObject('residue_plddt_by_chain', residuePlddtByChain);
      assignNumber('ligand_plddt', rowRecord.confidence_ligand_plddt);
      assignNumber('ligand_mean_plddt', rowRecord.confidence_ligand_mean_plddt);
      assignNumber('complex_iplddt', rowRecord.confidence_complex_iplddt);
      assignNumber('complex_plddt_protein', rowRecord.confidence_complex_plddt_protein);
      assignNumber('complex_plddt', rowRecord.confidence_complex_plddt);
      assignNumber('plddt', rowRecord.confidence_plddt);
      assignNumber('ipsae_dom', rowRecord.confidence_ipsae_dom);
      assignNumber('ligand_ipsae_max', rowRecord.confidence_ligand_ipsae_max);
      assignNumber('interface_metric', rowRecord.confidence_interface_metric);
      assignNumber('interface_metric_value', rowRecord.confidence_interface_metric_value);
      assignText('interface_metric_label', rowRecord.confidence_interface_metric_label);
      assignText('interface_metric_source', rowRecord.confidence_interface_metric_source);
      assignNumber('interfaceMetricValue', rowRecord.confidence_interfaceMetricValue);
      assignText('interfaceMetricLabel', rowRecord.confidence_interfaceMetricLabel);
      assignText('interfaceMetricSource', rowRecord.confidence_interfaceMetricSource);
      assignNumber('iptm', rowRecord.confidence_iptm);
      assignNumber('ligand_iptm', rowRecord.confidence_ligand_iptm);
      assignNumber('protein_iptm', rowRecord.confidence_protein_iptm);
      assignNumber('complex_pde', rowRecord.confidence_complex_pde);
      assignNumber('complex_pae', rowRecord.confidence_complex_pae);
      assignNumber('gpde', rowRecord.confidence_gpde);
      assignNumber('pae', rowRecord.confidence_pae);

      const ligandDisplay: Record<string, unknown> = {};
      const displaySmiles = readText(rowRecord.confidence_ligand_display_smiles);
      if (displaySmiles) ligandDisplay.smiles = displaySmiles;
      if (Object.keys(ligandDisplayAtomPlddtsByChain).length > 0) ligandDisplay.atom_plddts_by_chain = ligandDisplayAtomPlddtsByChain;
      if (Array.isArray(rowRecord.confidence_ligand_display_atom_plddts) && rowRecord.confidence_ligand_display_atom_plddts.length > 0) {
        ligandDisplay.atom_plddts = rowRecord.confidence_ligand_display_atom_plddts;
      }
      if (Object.keys(ligandDisplay).length > 0) next.ligand_display = ligandDisplay;

      const ligand: Record<string, unknown> = {};
      const ligandSmiles = readText(rowRecord.confidence_ligand_smiles);
      if (ligandSmiles) ligand.smiles = ligandSmiles;
      if (Object.keys(ligandAtomPlddtsByChain).length > 0) ligand.atom_plddts_by_chain = ligandAtomPlddtsByChain;
      if (Array.isArray(rowRecord.confidence_ligand_atom_plddts) && rowRecord.confidence_ligand_atom_plddts.length > 0) {
        ligand.atom_plddts = rowRecord.confidence_ligand_atom_plddts;
      }
      if (Object.keys(ligand).length > 0) next.ligand = ligand;

      return next;
    })();
    return {
      ...row,
      components: Array.isArray((detail as any).components) ? (detail as any).components : [],
      properties:
        includeProperties
          ? row.properties
          : leadOptSummaryProperties || summaryProperties,
      confidence: includeConfidence ? row.confidence : summaryConfidence,
    };
  });

  const scope = options?.accessScope || 'owner';
  const accessLevel = options?.accessLevel || (scope === 'owner' ? 'owner' : 'viewer');
  const editableTaskIdSet = buildEditableTaskIdSet(options?.editableTaskIds || []);
  return mergedRows.map((row) => {
    const normalizedProperties = includeProperties
      ? (row.properties && typeof row.properties === 'object' && !Array.isArray(row.properties)
          ? (row.properties as ProjectTask['properties'])
          : {
              affinity: false,
              target: null,
              ligand: null,
              binder: null
            }) as ProjectTask['properties']
      : (row.properties && typeof row.properties === 'object' && !Array.isArray(row.properties)
          ? (row.properties as ProjectTask['properties'])
          : {}) as ProjectTask['properties'];
    return applyTaskAccess({
      name: '',
      summary: '',
      protein_sequence: '',
      affinity: {},
      constraints: [],
      ...row,
      confidence:
        row.confidence && typeof row.confidence === 'object' && !Array.isArray(row.confidence)
          ? (row.confidence as ProjectTask['confidence'])
          : ({} as ProjectTask['confidence']),
      properties: normalizedProperties
    } as ProjectTask, scope, accessLevel, editableTaskIdSet);
  }) as ProjectTask[];
}

