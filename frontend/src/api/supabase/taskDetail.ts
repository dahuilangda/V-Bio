import { ProjectTask } from '../../types/models';
import { asObjectRecord, compactObjectRecord, parseBooleanToken, request } from './rest';



// 单任务详情读取与上传元数据清洗。

export async function getProjectTaskById(
  taskRowId: string,
  options?: {
    includeComponents?: boolean;
    includeConstraints?: boolean;
    includeProperties?: boolean;
    includeLeadOptSummary?: boolean;
    includeLeadOptCandidates?: boolean;
    includeConfidence?: boolean;
    includeAffinity?: boolean;
    includeProteinSequence?: boolean;
  }
): Promise<ProjectTask | null> {
  const normalizedTaskRowId = String(taskRowId || '').trim();
  if (!normalizedTaskRowId) return null;
  const includeComponents = options?.includeComponents !== false;
  const includeConstraints = options?.includeConstraints !== false;
  const includeProperties = options?.includeProperties !== false;
  const includeLeadOptSummary = options?.includeLeadOptSummary === true;
  const includeLeadOptCandidates = options?.includeLeadOptCandidates === true;
  const includeConfidence = options?.includeConfidence !== false;
  const includeAffinity = options?.includeAffinity !== false;
  const includeProteinSequence = options?.includeProteinSequence !== false;
  const selectFields = [
    'id',
    'project_id',
    'name',
    'summary',
    'task_id',
    'task_state',
    'status_text',
    'error_text',
    ...(includeProteinSequence ? ['protein_sequence'] : []),
    'backend',
    'seed',
    'ligand_smiles',
    ...(includeAffinity ? ['affinity'] : []),
    ...(includeConfidence ? ['confidence'] : []),
    ...(includeComponents ? ['components'] : []),
    ...(includeConstraints ? ['constraints'] : []),
    ...(includeProperties ? ['properties'] : []),
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
          'lead_opt_state_prediction_by_smiles:properties->lead_opt_state->prediction_by_smiles',
          'lead_opt_state_reference_prediction_by_backend:properties->lead_opt_state->reference_prediction_by_backend',
          'lead_opt_state_prediction_task_id:properties->lead_opt_state->>prediction_task_id',
          'lead_opt_state_prediction_candidate_smiles:properties->lead_opt_state->>prediction_candidate_smiles',
        ]
      : []),
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks', undefined, {
    select: selectFields,
    id: `eq.${normalizedTaskRowId}`,
    limit: '1'
  });
  const row = rows[0];
  if (!row) return null;
  const rowRecord = row as unknown as Record<string, unknown>;
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

  const normalizedProperties = (() => {
    if (includeProperties) {
      if (row.properties && typeof row.properties === 'object' && !Array.isArray(row.properties)) {
        return row.properties as ProjectTask['properties'];
      }
      return {
        affinity: false,
        target: null,
        ligand: null,
        binder: null
      } as ProjectTask['properties'];
    }
    if (!includeLeadOptSummary) {
      return {} as ProjectTask['properties'];
    }
    const listStage = readText(rowRecord.lead_opt_list_stage);
    const listPredictionStage = readText(rowRecord.lead_opt_list_prediction_stage);
    const listQueryId = readText(rowRecord.lead_opt_list_query_id);
    const listTransformCount = readFiniteNumber(rowRecord.lead_opt_list_transform_count);
    const listCandidateCount = readFiniteNumber(rowRecord.lead_opt_list_candidate_count);
    const listBucketCount = readFiniteNumber(rowRecord.lead_opt_list_bucket_count);
    const listDatabaseId = readText(rowRecord.lead_opt_list_database_id);
    const listDatabaseLabel = readText(rowRecord.lead_opt_list_database_label);
    const listDatabaseSchema = readText(rowRecord.lead_opt_list_database_schema);
    const listSelectedFragmentIds = readStringArray(rowRecord.lead_opt_list_selected_fragment_ids);
    const listSelectedAtomIndices = readIntegerArray(rowRecord.lead_opt_list_selected_atom_indices);
    const listSelectedFragmentQuery = readText(rowRecord.lead_opt_list_selected_fragment_query);
    const listPredictionTotal = readFiniteNumber(rowRecord.lead_opt_list_prediction_total);
    const listPredictionQueued = readFiniteNumber(rowRecord.lead_opt_list_prediction_queued);
    const listPredictionRunning = readFiniteNumber(rowRecord.lead_opt_list_prediction_running);
    const listPredictionSuccess = readFiniteNumber(rowRecord.lead_opt_list_prediction_success);
    const listPredictionFailure = readFiniteNumber(rowRecord.lead_opt_list_prediction_failure);
    const listQueryResultGroupedByEnvironment = parseBooleanToken(rowRecord.lead_opt_list_query_result_grouped_by_environment);
    const listQueryResult = compactObjectRecord({
      query_id: readText(rowRecord.lead_opt_list_query_result_query_id) || listQueryId,
      query_mode: readText(rowRecord.lead_opt_list_query_result_query_mode),
      aggregation_type: readText(rowRecord.lead_opt_list_query_result_aggregation_type),
      property_targets: asObjectRecord(rowRecord.lead_opt_list_query_result_property_targets),
      rule_env_radius: readFiniteNumber(rowRecord.lead_opt_list_query_result_rule_env_radius),
      grouped_by_environment:
        listQueryResultGroupedByEnvironment === null ? undefined : listQueryResultGroupedByEnvironment,
      cluster_group_by: readText(rowRecord.lead_opt_list_query_result_cluster_group_by),
      min_pairs: readFiniteNumber(rowRecord.lead_opt_list_query_result_min_pairs),
      count: readFiniteNumber(rowRecord.lead_opt_list_query_result_count),
      global_count: readFiniteNumber(rowRecord.lead_opt_list_query_result_global_count),
      stats: asObjectRecord(rowRecord.lead_opt_list_query_result_stats),
      mmp_database_id:
        readText(rowRecord.lead_opt_list_query_result_database_id) || listDatabaseId,
      mmp_database_label:
        readText(rowRecord.lead_opt_list_query_result_database_label) || listDatabaseLabel,
      mmp_database_schema:
        readText(rowRecord.lead_opt_list_query_result_database_schema) || listDatabaseSchema
    });

    const stateStage = readText(rowRecord.lead_opt_state_stage);
    const statePredictionStage = readText(rowRecord.lead_opt_state_prediction_stage);
    const stateQueryId = readText(rowRecord.lead_opt_state_query_id);
    const statePredictionTotal = readFiniteNumber(rowRecord.lead_opt_state_prediction_total);
    const statePredictionQueued = readFiniteNumber(rowRecord.lead_opt_state_prediction_queued);
    const statePredictionRunning = readFiniteNumber(rowRecord.lead_opt_state_prediction_running);
    const statePredictionSuccess = readFiniteNumber(rowRecord.lead_opt_state_prediction_success);
    const statePredictionFailure = readFiniteNumber(rowRecord.lead_opt_state_prediction_failure);
    const statePredictionTaskId = readText(rowRecord.lead_opt_state_prediction_task_id);
    const statePredictionCandidateSmiles = readText(rowRecord.lead_opt_state_prediction_candidate_smiles);

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
        statePredictionTaskId ||
        statePredictionCandidateSmiles ||
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
        Object.keys(listQueryResult).length > 0
    );
    if (!hasAnyLeadOptMeta) return {} as ProjectTask['properties'];

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
          rowRecord.lead_opt_list_selection && typeof rowRecord.lead_opt_list_selection === 'object' && !Array.isArray(rowRecord.lead_opt_list_selection)
            ? rowRecord.lead_opt_list_selection
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
        enumerated_candidates:
          includeLeadOptCandidates && Array.isArray(rowRecord.lead_opt_list_enumerated_candidates)
            ? rowRecord.lead_opt_list_enumerated_candidates
            : []
      },
      lead_opt_state: {
        stage: stateStage || listStage,
        prediction_stage: statePredictionStage || listPredictionStage,
        query_id: stateQueryId || listQueryId,
        prediction_summary: normalizedPredictionSummary,
        prediction_task_id: statePredictionTaskId,
        prediction_candidate_smiles: statePredictionCandidateSmiles,
        prediction_by_smiles:
          rowRecord.lead_opt_state_prediction_by_smiles &&
          typeof rowRecord.lead_opt_state_prediction_by_smiles === 'object' &&
          !Array.isArray(rowRecord.lead_opt_state_prediction_by_smiles)
            ? rowRecord.lead_opt_state_prediction_by_smiles
            : {},
        reference_prediction_by_backend:
          rowRecord.lead_opt_state_reference_prediction_by_backend &&
          typeof rowRecord.lead_opt_state_reference_prediction_by_backend === 'object' &&
          !Array.isArray(rowRecord.lead_opt_state_reference_prediction_by_backend)
            ? rowRecord.lead_opt_state_reference_prediction_by_backend
            : {}
      }
    } as unknown as ProjectTask['properties'];
  })();

  return {
    name: '',
    summary: '',
    protein_sequence: '',
    confidence: {},
    affinity: {},
    components: [],
    constraints: [],
    ...row,
    properties: normalizedProperties
  } as ProjectTask;
}

function stripTemplateContentFromTaskComponents(components: unknown): unknown {
  if (!Array.isArray(components)) return components;
  return components.map((component) => {
    if (!component || typeof component !== 'object' || Array.isArray(component)) return component;
    const next = { ...(component as Record<string, unknown>) };
    const camelUpload = next.templateUpload;
    if (camelUpload && typeof camelUpload === 'object' && !Array.isArray(camelUpload)) {
      const compactUpload = { ...(camelUpload as Record<string, unknown>) };
      delete compactUpload.content;
      next.templateUpload = compactUpload;
    }
    const snakeUpload = next.template_upload;
    if (snakeUpload && typeof snakeUpload === 'object' && !Array.isArray(snakeUpload)) {
      const compactUpload = { ...(snakeUpload as Record<string, unknown>) };
      delete compactUpload.content;
      next.template_upload = compactUpload;
    }
    return next;
  });
}

const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';

function affinityUploadRoleFromTaskComponent(component: Record<string, unknown>): 'target' | 'ligand' | null {
  const uploadMeta =
    component.affinityUpload && typeof component.affinityUpload === 'object' && !Array.isArray(component.affinityUpload)
      ? (component.affinityUpload as Record<string, unknown>)
      : component.affinity_upload && typeof component.affinity_upload === 'object' && !Array.isArray(component.affinity_upload)
        ? (component.affinity_upload as Record<string, unknown>)
        : null;
  const roleFromMeta = uploadMeta?.role;
  if (roleFromMeta === 'target' || roleFromMeta === 'ligand') return roleFromMeta;
  const componentId = typeof component.id === 'string' ? component.id.trim() : '';
  if (componentId === AFFINITY_TARGET_UPLOAD_COMPONENT_ID) return 'target';
  if (componentId === AFFINITY_LIGAND_UPLOAD_COMPONENT_ID) return 'ligand';
  return null;
}

function normalizeAffinityUploadMeta(
  raw: unknown,
  role: 'target' | 'ligand'
): Record<string, unknown> | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const next = { ...(raw as Record<string, unknown>) };
  next.role = role;

  if (typeof next.fileName === 'string') {
    const trimmed = next.fileName.trim();
    if (trimmed) {
      next.fileName = trimmed;
    } else {
      delete next.fileName;
    }
  } else {
    delete next.fileName;
  }

  if (typeof next.content !== 'string') {
    delete next.content;
  }

  return next;
}

function stripAffinityUploadContentFromTaskComponents(components: unknown): unknown {
  if (!Array.isArray(components)) return components;
  return components.map((component) => {
    if (!component || typeof component !== 'object' || Array.isArray(component)) return component;
    const role = affinityUploadRoleFromTaskComponent(component as Record<string, unknown>);
    if (!role) return component;
    const next = { ...(component as Record<string, unknown>) };
    const normalizedCamel = normalizeAffinityUploadMeta(next.affinityUpload, role);
    const normalizedSnake = normalizeAffinityUploadMeta(next.affinity_upload, role);
    if (normalizedCamel) {
      next.affinityUpload = normalizedCamel;
    } else {
      delete next.affinityUpload;
    }
    if (normalizedSnake) {
      next.affinity_upload = normalizedSnake;
    } else {
      delete next.affinity_upload;
    }
    return next;
  });
}

export function sanitizeProjectTaskWritePayload(payload: Partial<ProjectTask>): Partial<ProjectTask> {
  if (!Object.prototype.hasOwnProperty.call(payload, 'components')) return payload;
  const compactComponents = stripTemplateContentFromTaskComponents((payload as { components?: unknown }).components);
  return {
    ...payload,
    components: stripAffinityUploadContentFromTaskComponents(compactComponents) as ProjectTask['components']
  };
}

