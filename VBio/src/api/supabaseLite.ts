import type { ApiToken, ApiTokenUsage, ApiTokenUsageDaily, AppUser, Project, ProjectTask, TaskState } from '../types/models';
import { ENV } from '../utils/env';

const configuredBaseUrl = ENV.supabaseRestUrl.replace(/\/$/, '');
const SUPABASE_TIMEOUT_MS = 15000;

async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = SUPABASE_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...init,
      signal: controller.signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error(
        `Supabase-lite request timeout after ${timeoutMs}ms for ${url}. Check that PostgREST is reachable (VBio/supabase-lite, default port 54321).`
      );
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function unique<T>(items: T[]): T[] {
  return Array.from(new Set(items));
}

function buildQueryString(query?: Record<string, string | undefined>) {
  const params = new URLSearchParams();
  if (query) {
    Object.entries(query).forEach(([key, value]) => {
      if (value !== undefined && value !== '') {
        params.set(key, value);
      }
    });
  }
  return params.toString();
}

function buildSupabaseUrlCandidates(path: string, query?: Record<string, string | undefined>): string[] {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  const queryString = buildQueryString(query);
  const suffix = `${normalizedPath}${queryString ? `?${queryString}` : ''}`;
  const candidates = [`${configuredBaseUrl}${suffix}`];

  if (typeof window !== 'undefined') {
    // Vite proxy path
    candidates.push(`${window.location.origin}/supabase${suffix}`);
    // Remote-access fallback: hit PostgREST directly on the current host.
    candidates.push(`${window.location.protocol}//${window.location.hostname}:54321${suffix}`);
  }

  return unique(candidates.filter(Boolean));
}

async function request<T>(
  path: string,
  options?: RequestInit,
  query?: Record<string, string | undefined>
): Promise<T> {
  const candidates = buildSupabaseUrlCandidates(path, query);
  let lastError: Error | null = null;

  for (let i = 0; i < candidates.length; i += 1) {
    const url = candidates[i];
    let res: Response;
    try {
      res = await fetchWithTimeout(url, {
        ...options,
        headers: {
          'Content-Type': 'application/json',
          ...(options?.headers || {})
        }
      });
    } catch (error) {
      lastError = error instanceof Error ? error : new Error('Unknown network error');
      continue;
    }

    if ((res.status === 404 || res.status === 405) && i < candidates.length - 1) {
      lastError = new Error(`Supabase candidate returned ${res.status}: ${url}`);
      continue;
    }

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`PostgREST ${res.status}: ${text}`);
    }

    if (res.status === 204) {
      return [] as T;
    }

    return (await res.json()) as T;
  }

  const detail = lastError ? ` Last error: ${lastError.message}` : '';
  throw new Error(`Supabase-lite request failed. Tried: ${candidates.join(', ')}.${detail}`);
}

export async function listUsers(): Promise<AppUser[]> {
  return request<AppUser[]>('/app_users', undefined, {
    select: '*',
    deleted_at: 'is.null',
    order: 'created_at.asc'
  });
}

export async function findUserByUsername(username: string): Promise<AppUser | null> {
  const rows = await request<AppUser[]>('/app_users', undefined, {
    select: '*',
    username: `eq.${username.toLowerCase()}`,
    deleted_at: 'is.null',
    limit: '1'
  });
  return rows[0] || null;
}

export async function findUserByEmail(email: string): Promise<AppUser | null> {
  const rows = await request<AppUser[]>('/app_users', undefined, {
    select: '*',
    email: `eq.${email.toLowerCase()}`,
    deleted_at: 'is.null',
    limit: '1'
  });
  return rows[0] || null;
}

export async function findUserByIdentifier(identifier: string): Promise<AppUser | null> {
  const value = identifier.trim().toLowerCase();
  const rows = await request<AppUser[]>('/app_users', undefined, {
    select: '*',
    or: `(username.eq.${value},email.eq.${value})`,
    deleted_at: 'is.null',
    limit: '1'
  });
  return rows[0] || null;
}

export async function insertUser(input: Partial<AppUser>): Promise<AppUser> {
  const rows = await request<AppUser[]>(
    '/app_users',
    {
      method: 'POST',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(input)
    },
    {
      select: '*'
    }
  );
  return rows[0];
}

export async function updateUser(userId: string, patch: Partial<AppUser>): Promise<AppUser> {
  const rows = await request<AppUser[]>(
    '/app_users',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(patch)
    },
    {
      id: `eq.${userId}`,
      select: '*'
    }
  );
  return rows[0];
}

interface ListProjectsOptions {
  userId?: string;
  includeDeleted?: boolean;
  search?: string;
  lightweight?: boolean;
}

export async function listProjects(options: ListProjectsOptions = {}): Promise<Project[]> {
  const projectSelect = options.lightweight === false
    ? '*'
    : [
        'id',
        'user_id',
        'name',
        'summary',
        'backend',
        'use_msa',
        'color_mode',
        'task_type',
        'task_id',
        'task_state',
        'status_text',
        'error_text',
        'submitted_at',
        'completed_at',
        'duration_seconds',
        'structure_name',
        'created_at',
        'updated_at',
        'deleted_at'
      ].join(',');
  const query: Record<string, string | undefined> = {
    select: projectSelect,
    order: 'updated_at.desc'
  };

  if (!options.includeDeleted) {
    query.deleted_at = 'is.null';
  }
  if (options.userId) {
    query.user_id = `eq.${options.userId}`;
  }
  if (options.search?.trim()) {
    query.name = `ilike.*${options.search.trim()}*`;
  }

  const rows = await request<Array<Partial<Project>>>('/projects', undefined, query);
  return rows.map((row) => ({
    protein_sequence: '',
    ligand_smiles: '',
    confidence: {},
    affinity: {},
    ...row
  })) as Project[];
}

interface GetProjectByIdOptions {
  lightweight?: boolean;
}

export async function getProjectById(projectId: string, options: GetProjectByIdOptions = {}): Promise<Project | null> {
  const selectFields = options.lightweight
    ? [
        'id',
        'user_id',
        'name',
        'summary',
        'backend',
        'use_msa',
        'color_mode',
        'task_type',
        'task_id',
        'task_state',
        'status_text',
        'error_text',
        'submitted_at',
        'completed_at',
        'duration_seconds',
        'structure_name',
        'created_at',
        'updated_at',
        'deleted_at'
      ].join(',')
    : '*';
  const rows = await request<Project[]>('/projects', undefined, {
    select: selectFields,
    id: `eq.${projectId}`,
    limit: '1'
  });
  const row = rows[0];
  if (!row) return null;
  const normalized = row as Partial<Project>;
  return {
    ...normalized,
    protein_sequence: normalized.protein_sequence || '',
    ligand_smiles: normalized.ligand_smiles || '',
    confidence:
      normalized.confidence && typeof normalized.confidence === 'object' && !Array.isArray(normalized.confidence)
        ? normalized.confidence
        : {},
    affinity:
      normalized.affinity && typeof normalized.affinity === 'object' && !Array.isArray(normalized.affinity)
        ? normalized.affinity
        : {}
  } as Project;
}

export async function insertProject(input: Partial<Project>): Promise<Project> {
  const rows = await request<Project[]>(
    '/projects',
    {
      method: 'POST',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(input)
    },
    {
      select: '*'
    }
  );
  return rows[0];
}

export async function updateProject(projectId: string, patch: Partial<Project>): Promise<Project> {
  const rows = await request<Project[]>(
    '/projects',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(patch)
    },
    {
      id: `eq.${projectId}`,
      select: '*'
    }
  );
  return rows[0];
}

export async function listProjectTasks(projectId: string): Promise<ProjectTask[]> {
  return request<ProjectTask[]>('/project_tasks', undefined, {
    select: '*',
    project_id: `eq.${projectId}`,
    order: 'created_at.desc'
  });
}

export async function listProjectTasksCompact(projectId: string): Promise<ProjectTask[]> {
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
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
    select: selectFields,
    project_id: `eq.${projectId}`,
    order: 'created_at.desc'
  });

  return rows.map((row) => ({
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
  })) as ProjectTask[];
}

export async function listProjectTasksForList(
  projectId: string,
  options?: {
    includeComponents?: boolean;
    includeConfidence?: boolean;
    includeProperties?: boolean;
    includeLeadOptSummary?: boolean;
  }
): Promise<ProjectTask[]> {
  const includeComponents = options?.includeComponents !== false;
  const includeConfidence = options?.includeConfidence !== false;
  const includeProperties = options?.includeProperties !== false;
  const includeLeadOptSummary = options?.includeLeadOptSummary === true;
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
    ...(includeProperties ? ['properties'] : []),
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
          'lead_opt_state_stage:properties->lead_opt_state->>stage',
          'lead_opt_state_prediction_stage:properties->lead_opt_state->>prediction_stage',
          'lead_opt_state_query_id:properties->lead_opt_state->>query_id',
          'lead_opt_state_prediction_total:properties->lead_opt_state->prediction_summary->>total',
          'lead_opt_state_prediction_queued:properties->lead_opt_state->prediction_summary->>queued',
          'lead_opt_state_prediction_running:properties->lead_opt_state->prediction_summary->>running',
          'lead_opt_state_prediction_success:properties->lead_opt_state->prediction_summary->>success',
          'lead_opt_state_prediction_failure:properties->lead_opt_state->prediction_summary->>failure',
        ]
      : []),
    'structure_name',
    'submitted_at',
    'completed_at',
    'duration_seconds',
    'created_at',
    'updated_at'
  ].join(',');
  const rows = await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
    select: selectFields,
    project_id: `eq.${projectId}`,
    order: 'created_at.desc'
  });
  const detailRows = includeComponents
    ? await request<Array<Partial<ProjectTask>>>('/project_tasks_list', undefined, {
        select: 'id,components',
        project_id: `eq.${projectId}`,
        order: 'created_at.desc'
      })
    : [];
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

    const stateStage = readText(row.lead_opt_state_stage);
    const statePredictionStage = readText(row.lead_opt_state_prediction_stage);
    const stateQueryId = readText(row.lead_opt_state_query_id);
    const statePredictionTotal = readFiniteNumber(row.lead_opt_state_prediction_total);
    const statePredictionQueued = readFiniteNumber(row.lead_opt_state_prediction_queued);
    const statePredictionRunning = readFiniteNumber(row.lead_opt_state_prediction_running);
    const statePredictionSuccess = readFiniteNumber(row.lead_opt_state_prediction_success);
    const statePredictionFailure = readFiniteNumber(row.lead_opt_state_prediction_failure);

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
        statePredictionFailure !== null
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
        selection: {
          selected_fragment_ids: listSelectedFragmentIds,
          selected_fragment_atom_indices: listSelectedAtomIndices,
          variable_queries: listSelectedFragmentQuery ? [listSelectedFragmentQuery] : []
        },
        selected_fragment_ids: listSelectedFragmentIds,
        selected_fragment_atom_indices: listSelectedAtomIndices,
        selected_fragment_query: listSelectedFragmentQuery,
        prediction_summary: normalizedPredictionSummary
      },
      lead_opt_state: {
        stage: stateStage || listStage,
        prediction_stage: statePredictionStage || listPredictionStage,
        query_id: stateQueryId || listQueryId,
        prediction_summary: normalizedPredictionSummary
      }
    };
  };

  const mergedRows = rows.map((row) => {
    const detail = detailById.get(String(row.id || '').trim()) || {};
    const rowRecord = row as unknown as Record<string, unknown>;
    const leadOptSummaryProperties = includeLeadOptSummary ? buildLeadOptSummaryProperties(rowRecord) : null;
    return {
      ...row,
      components: Array.isArray((detail as any).components) ? (detail as any).components : [],
      properties:
        includeProperties
          ? row.properties
          : leadOptSummaryProperties || {}
    };
  });

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
    return {
      name: '',
      summary: '',
      protein_sequence: '',
      confidence: {},
      affinity: {},
      constraints: [],
      ...row,
      properties: normalizedProperties
    };
  }) as ProjectTask[];
}

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
          'lead_opt_list_ui_state:properties->lead_opt_list->ui_state',
          'lead_opt_list_query_result:properties->lead_opt_list->query_result',
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
          ...(includeLeadOptCandidates
            ? [
                'lead_opt_state_prediction_by_smiles:properties->lead_opt_state->prediction_by_smiles',
                'lead_opt_state_reference_prediction_by_backend:properties->lead_opt_state->reference_prediction_by_backend'
              ]
            : []),
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
        statePredictionFailure !== null
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
        query_result:
          rowRecord.lead_opt_list_query_result && typeof rowRecord.lead_opt_list_query_result === 'object' && !Array.isArray(rowRecord.lead_opt_list_query_result)
            ? rowRecord.lead_opt_list_query_result
            : {},
        ui_state:
          rowRecord.lead_opt_list_ui_state && typeof rowRecord.lead_opt_list_ui_state === 'object' && !Array.isArray(rowRecord.lead_opt_list_ui_state)
            ? rowRecord.lead_opt_list_ui_state
            : {},
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
          includeLeadOptCandidates &&
          rowRecord.lead_opt_state_prediction_by_smiles &&
          typeof rowRecord.lead_opt_state_prediction_by_smiles === 'object' &&
          !Array.isArray(rowRecord.lead_opt_state_prediction_by_smiles)
            ? rowRecord.lead_opt_state_prediction_by_smiles
            : {},
        reference_prediction_by_backend:
          includeLeadOptCandidates &&
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

function sanitizeProjectTaskWritePayload(payload: Partial<ProjectTask>): Partial<ProjectTask> {
  if (!Object.prototype.hasOwnProperty.call(payload, 'components')) return payload;
  const compactComponents = stripTemplateContentFromTaskComponents((payload as { components?: unknown }).components);
  return {
    ...payload,
    components: stripAffinityUploadContentFromTaskComponents(compactComponents) as ProjectTask['components']
  };
}

interface ProjectTaskStateRow {
  project_id: string;
  task_id: string;
  task_state: TaskState;
}

export async function listProjectTaskStatesByProjects(projectIds: string[]): Promise<ProjectTaskStateRow[]> {
  const normalizedIds = Array.from(new Set(projectIds.map((id) => id.trim()).filter(Boolean)));
  if (normalizedIds.length === 0) return [];

  // Keep query strings bounded; very large `in (...)` filters can time out or exceed URL limits.
  const chunkSize = 150;
  const chunks: string[][] = [];
  for (let i = 0; i < normalizedIds.length; i += chunkSize) {
    chunks.push(normalizedIds.slice(i, i + chunkSize));
  }

  const results = await Promise.all(
    chunks.map((chunk) =>
      request<ProjectTaskStateRow[]>('/project_tasks', undefined, {
        select: 'project_id,task_id,task_state',
        project_id: `in.(${chunk.join(',')})`
      })
    )
  );
  return results.flat();
}

export async function insertProjectTask(input: Partial<ProjectTask>): Promise<ProjectTask> {
  const sanitized = sanitizeProjectTaskWritePayload(input);
  const rows = await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'POST',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(sanitized)
    },
    {
      select: '*'
    }
  );
  return rows[0];
}

export async function updateProjectTask(
  taskRowId: string,
  patch: Partial<ProjectTask>,
  options?: { minimalReturn?: boolean; select?: string }
): Promise<ProjectTask> {
  const sanitized = sanitizeProjectTaskWritePayload(patch);
  const preferHeader = options?.minimalReturn ? 'return=minimal' : 'return=representation';
  const rows = await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'PATCH',
      headers: {
        Prefer: preferHeader
      },
      body: JSON.stringify(sanitized)
    },
    {
      id: `eq.${taskRowId}`,
      ...(options?.minimalReturn ? {} : { select: options?.select || '*' })
    }
  );
  if (options?.minimalReturn) {
    return {
      id: taskRowId,
      ...sanitized,
      updated_at: new Date().toISOString()
    } as ProjectTask;
  }
  return rows[0];
}

export async function findProjectTaskByTaskId(taskId: string, projectId?: string): Promise<ProjectTask | null> {
  const normalizedTaskId = taskId.trim();
  if (!normalizedTaskId) return null;
  const query: Record<string, string | undefined> = {
    select: '*',
    task_id: `eq.${normalizedTaskId}`,
    order: 'created_at.desc',
    limit: '1'
  };
  if (projectId?.trim()) {
    query.project_id = `eq.${projectId.trim()}`;
  }
  const rows = await request<ProjectTask[]>('/project_tasks', undefined, query);
  return rows[0] || null;
}

export async function updateProjectTaskByTaskId(
  taskId: string,
  patch: Partial<ProjectTask>,
  projectId?: string
): Promise<ProjectTask | null> {
  const current = await findProjectTaskByTaskId(taskId, projectId);
  if (!current) return null;
  return updateProjectTask(current.id, patch);
}

export async function deleteProjectTask(taskRowId: string): Promise<void> {
  await request<ProjectTask[]>(
    '/project_tasks',
    {
      method: 'DELETE',
      headers: {
        Prefer: 'return=minimal'
      }
    },
    {
      id: `eq.${taskRowId}`
    }
  );
}

export async function listApiTokens(userId: string): Promise<ApiToken[]> {
  const normalizedUserId = String(userId || '').trim();
  if (!normalizedUserId) return [];
  return request<ApiToken[]>('/api_tokens', undefined, {
    select: '*',
    user_id: `eq.${normalizedUserId}`,
    order: 'created_at.desc'
  });
}

export async function findApiTokenByHash(tokenHash: string): Promise<ApiToken | null> {
  const normalizedHash = String(tokenHash || '').trim();
  if (!normalizedHash) return null;
  const rows = await request<ApiToken[]>('/api_tokens', undefined, {
    select: '*',
    token_hash: `eq.${normalizedHash}`,
    limit: '1'
  });
  return rows[0] || null;
}

export async function insertApiToken(input: Partial<ApiToken>): Promise<ApiToken> {
  const rows = await request<ApiToken[]>(
    '/api_tokens',
    {
      method: 'POST',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(input)
    },
    {
      select: '*'
    }
  );
  return rows[0];
}

export async function updateApiToken(tokenId: string, patch: Partial<ApiToken>): Promise<ApiToken> {
  const rows = await request<ApiToken[]>(
    '/api_tokens',
    {
      method: 'PATCH',
      headers: {
        Prefer: 'return=representation'
      },
      body: JSON.stringify(patch)
    },
    {
      id: `eq.${tokenId}`,
      select: '*'
    }
  );
  return rows[0];
}

export async function revokeApiToken(tokenId: string): Promise<ApiToken> {
  return updateApiToken(tokenId, {
    is_active: false,
    revoked_at: new Date().toISOString()
  });
}

export async function deleteApiToken(tokenId: string): Promise<void> {
  await request<ApiToken[]>(
    '/api_tokens',
    {
      method: 'DELETE',
      headers: {
        Prefer: 'return=minimal'
      }
    },
    {
      id: `eq.${tokenId}`
    }
  );
}

export async function listApiTokenUsage(tokenId: string, sinceIso?: string): Promise<ApiTokenUsage[]> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) return [];
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'created_at.desc'
  };
  if (sinceIso?.trim()) {
    query.created_at = `gte.${sinceIso.trim()}`;
  }
  return request<ApiTokenUsage[]>('/api_token_usage', undefined, query);
}

function parseTotalFromContentRange(value: string | null): number {
  if (!value) return 0;
  const slashIdx = value.lastIndexOf('/');
  if (slashIdx < 0) return 0;
  const raw = value.slice(slashIdx + 1).trim();
  const total = Number(raw);
  return Number.isFinite(total) && total >= 0 ? Math.floor(total) : 0;
}

export async function listApiTokenUsagePage(
  tokenId: string,
  options: {
    sinceIso?: string;
    limit: number;
    offset: number;
  }
): Promise<{ rows: ApiTokenUsage[]; total: number }> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) {
    return { rows: [], total: 0 };
  }
  const limit = Math.max(1, Math.floor(options.limit || 1));
  const offset = Math.max(0, Math.floor(options.offset || 0));
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'created_at.desc',
    limit: String(limit),
    offset: String(offset)
  };
  if (options.sinceIso?.trim()) {
    query.created_at = `gte.${options.sinceIso.trim()}`;
  }

  const candidates = buildSupabaseUrlCandidates('/api_token_usage', query);
  let lastError: Error | null = null;
  for (let i = 0; i < candidates.length; i += 1) {
    const url = candidates[i];
    let res: Response;
    try {
      res = await fetchWithTimeout(url, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          Prefer: 'count=exact'
        }
      });
    } catch (error) {
      lastError = error instanceof Error ? error : new Error('Unknown network error');
      continue;
    }

    if ((res.status === 404 || res.status === 405) && i < candidates.length - 1) {
      lastError = new Error(`Supabase candidate returned ${res.status}: ${url}`);
      continue;
    }

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`PostgREST ${res.status}: ${text}`);
    }

    const rows = (await res.json()) as ApiTokenUsage[];
    const total = parseTotalFromContentRange(res.headers.get('content-range'));
    return { rows, total };
  }

  const detail = lastError ? ` Last error: ${lastError.message}` : '';
  throw new Error(`Supabase-lite request failed. Tried: ${candidates.join(', ')}.${detail}`);
}

export async function listApiTokenUsageDaily(tokenId: string, sinceIso?: string): Promise<ApiTokenUsageDaily[]> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) return [];
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'usage_day.asc'
  };
  if (sinceIso?.trim()) {
    query.usage_day = `gte.${sinceIso.trim().slice(0, 10)}`;
  }
  return request<ApiTokenUsageDaily[]>('/api_token_usage_daily', undefined, query);
}

export async function listApiTokenUsageDailyByTokenIds(tokenIds: string[], sinceIso?: string): Promise<ApiTokenUsageDaily[]> {
  const ids = Array.from(new Set(tokenIds.map((id) => String(id || '').trim()).filter(Boolean)));
  if (ids.length === 0) return [];
  const chunkSize = 150;
  const rows = await Promise.all(
    Array.from({ length: Math.ceil(ids.length / chunkSize) }, (_, i) => ids.slice(i * chunkSize, (i + 1) * chunkSize)).map(
      (chunk) => {
        const query: Record<string, string | undefined> = {
          select: '*',
          token_id: `in.(${chunk.join(',')})`,
          order: 'usage_day.asc'
        };
        if (sinceIso?.trim()) {
          query.usage_day = `gte.${sinceIso.trim().slice(0, 10)}`;
        }
        return request<ApiTokenUsageDaily[]>('/api_token_usage_daily', undefined, query);
      }
    )
  );
  return rows.flat();
}
