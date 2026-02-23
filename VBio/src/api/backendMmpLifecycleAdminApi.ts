import { API_HEADERS, requestBackend } from './backendClient';

export interface MmpLifecycleDatabaseItem {
  id: string;
  label: string;
  schema?: string;
  backend?: string;
  visible?: boolean;
  is_default?: boolean;
  properties?: Array<Record<string, unknown>>;
  stats?: Record<string, unknown>;
}

export interface MmpLifecycleBatchFileMeta {
  stored_name?: string;
  original_name?: string;
  size?: number;
  uploaded_at?: string;
  path?: string;
  column_config?: Record<string, unknown>;
}

export interface MmpLifecycleBatch {
  id: string;
  name: string;
  description?: string;
  notes?: string;
  status?: string;
  selected_database_id?: string;
  created_at?: string;
  updated_at?: string;
  files?: {
    compounds?: MmpLifecycleBatchFileMeta | null;
    experiments?: MmpLifecycleBatchFileMeta | null;
    generated_property_import?: MmpLifecycleBatchFileMeta | null;
  };
  last_check?: Record<string, unknown> | null;
  review?: Record<string, unknown> | null;
  approval?: Record<string, unknown> | null;
  rejection?: Record<string, unknown> | null;
  apply_history?: Array<Record<string, unknown>>;
  rollback_history?: Array<Record<string, unknown>>;
  status_history?: Array<Record<string, unknown>>;
}

export interface MmpLifecycleMethod {
  id: string;
  key: string;
  name: string;
  output_property?: string;
  input_unit?: string;
  output_unit?: string;
  display_unit?: string;
  import_transform?: string;
  display_transform?: string;
  category?: string;
  description?: string;
  reference?: string;
  created_at?: string;
  updated_at?: string;
}

export interface MmpLifecycleMethodUsage {
  method_id: string;
  database_id?: string;
  database_label?: string;
  database_schema?: string;
  mapping_count?: number;
  source_property_count?: number;
  source_properties?: string[];
}

export interface MmpLifecyclePropertyMapping {
  id: string;
  database_id: string;
  source_property: string;
  mmp_property: string;
  method_id?: string;
  value_transform?: string;
  notes?: string;
  updated_at?: string;
}

export interface MmpLifecyclePendingDatabaseSync {
  id: string;
  database_id: string;
  operation: string;
  payload?: Record<string, unknown>;
  dedupe_key?: string;
  status?: string;
  created_at?: string;
  updated_at?: string;
  applied_at?: string;
  result?: Record<string, unknown>;
  error?: string;
}

export interface MmpLifecycleCompoundsPreview {
  batch_id: string;
  original_name?: string;
  stored_name?: string;
  headers: string[];
  rows: Array<Record<string, string>>;
  total_rows: number;
  preview_truncated: boolean;
  column_non_empty_counts: Record<string, number>;
  column_numeric_counts: Record<string, number>;
  column_positive_numeric_counts: Record<string, number>;
}

export interface MmpLifecycleOverviewResponse {
  databases: MmpLifecycleDatabaseItem[];
  default_database_id?: string;
  methods: MmpLifecycleMethod[];
  batches: MmpLifecycleBatch[];
  pending_database_sync?: MmpLifecyclePendingDatabaseSync[];
  pending_sync_by_database?: Record<string, number>;
  updated_at?: string;
}

interface JsonObject {
  [key: string]: unknown;
}

async function parseJsonOrError(res: Response, scope: string): Promise<JsonObject> {
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${scope} (${res.status}): ${text}`);
  }
  return (await res.json()) as JsonObject;
}

export async function fetchMmpLifecycleOverview(): Promise<MmpLifecycleOverviewResponse> {
  const res = await requestBackend('/api/admin/lead_optimization/mmp_lifecycle/overview', {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  return (await parseJsonOrError(res, 'Failed to fetch lifecycle overview')) as unknown as MmpLifecycleOverviewResponse;
}

export async function createMmpLifecycleBatch(payload: {
  id?: string;
  name: string;
  description?: string;
  notes?: string;
  selected_database_id?: string;
}): Promise<MmpLifecycleBatch> {
  const res = await requestBackend('/api/admin/lead_optimization/mmp_lifecycle/batches', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload)
  });
  const data = await parseJsonOrError(res, 'Failed to create lifecycle batch');
  return (data.batch || {}) as MmpLifecycleBatch;
}

export async function patchMmpLifecycleBatch(batchId: string, payload: Record<string, unknown>): Promise<MmpLifecycleBatch> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}`, {
    method: 'PATCH',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  const data = await parseJsonOrError(res, 'Failed to update lifecycle batch');
  return (data.batch || {}) as MmpLifecycleBatch;
}

export async function transitionMmpLifecycleBatchStatus(
  batchId: string,
  payload: {
    action: 'review' | 'approve' | 'reject' | 'reopen';
    note?: string;
    database_id?: string;
  }
): Promise<Record<string, unknown>> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/status`, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  return (await parseJsonOrError(res, 'Failed to transition lifecycle batch status')) as Record<string, unknown>;
}

export async function deleteMmpLifecycleBatch(batchId: string): Promise<void> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}`, {
    method: 'DELETE',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  await parseJsonOrError(res, 'Failed to delete lifecycle batch');
}

export async function uploadMmpLifecycleCompounds(
  batchId: string,
  options: {
    file: File;
    smiles_column?: string;
    id_column?: string;
  }
): Promise<MmpLifecycleBatch> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const form = new FormData();
  form.append('file', options.file);
  if (String(options.smiles_column || '').trim()) form.append('smiles_column', String(options.smiles_column || '').trim());
  if (String(options.id_column || '').trim()) form.append('id_column', String(options.id_column || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/upload_compounds`, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    },
    body: form
  });
  const data = await parseJsonOrError(res, 'Failed to upload compound file');
  return (data.batch || {}) as MmpLifecycleBatch;
}

export async function uploadMmpLifecycleExperiments(
  batchId: string,
  options: {
    file: File;
    smiles_column?: string;
    property_column?: string;
    value_column?: string;
    method_column?: string;
    notes_column?: string;
    activity_columns?: string[];
    activity_method_map?: Record<string, string>;
    activity_transform_map?: Record<string, string>;
    activity_output_property_map?: Record<string, string>;
    assay_method_id?: string;
    assay_method_key?: string;
    source_notes_column?: string;
  }
): Promise<MmpLifecycleBatch> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const form = new FormData();
  form.append('file', options.file);
  if (String(options.smiles_column || '').trim()) form.append('smiles_column', String(options.smiles_column || '').trim());
  if (String(options.property_column || '').trim()) form.append('property_column', String(options.property_column || '').trim());
  if (String(options.value_column || '').trim()) form.append('value_column', String(options.value_column || '').trim());
  if (String(options.method_column || '').trim()) form.append('method_column', String(options.method_column || '').trim());
  if (String(options.notes_column || '').trim()) form.append('notes_column', String(options.notes_column || '').trim());
  if (Array.isArray(options.activity_columns) && options.activity_columns.length > 0) {
    form.append('activity_columns', JSON.stringify(options.activity_columns));
  }
  if (options.activity_method_map && typeof options.activity_method_map === 'object') {
    form.append('activity_method_map', JSON.stringify(options.activity_method_map));
  }
  if (options.activity_transform_map && typeof options.activity_transform_map === 'object') {
    form.append('activity_transform_map', JSON.stringify(options.activity_transform_map));
  }
  if (options.activity_output_property_map && typeof options.activity_output_property_map === 'object') {
    form.append('activity_output_property_map', JSON.stringify(options.activity_output_property_map));
  }
  if (String(options.assay_method_id || '').trim()) form.append('assay_method_id', String(options.assay_method_id || '').trim());
  if (String(options.assay_method_key || '').trim()) form.append('assay_method_key', String(options.assay_method_key || '').trim());
  if (String(options.source_notes_column || '').trim()) form.append('source_notes_column', String(options.source_notes_column || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/upload_experiments`, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    },
    body: form
  });
  const data = await parseJsonOrError(res, 'Failed to upload experiment file');
  return (data.batch || {}) as MmpLifecycleBatch;
}

export async function clearMmpLifecycleExperiments(batchId: string): Promise<MmpLifecycleBatch> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/clear_experiments`, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  const data = await parseJsonOrError(res, 'Failed to clear experiment file');
  return (data.batch || {}) as MmpLifecycleBatch;
}

export async function fetchMmpLifecycleCompoundsPreview(
  batchId: string,
  options?: { max_rows?: number }
): Promise<MmpLifecycleCompoundsPreview> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const search = new URLSearchParams();
  const maxRows = Number(options?.max_rows || 0);
  if (Number.isFinite(maxRows) && maxRows > 0) search.set('max_rows', String(Math.trunc(maxRows)));
  const suffix = search.toString() ? `?${search.toString()}` : '';
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/preview_compounds${suffix}`, {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  const data = await parseJsonOrError(res, 'Failed to preview compounds file');
  return {
    batch_id: String(data.batch_id || ''),
    original_name: String(data.original_name || ''),
    stored_name: String(data.stored_name || ''),
    headers: Array.isArray(data.headers) ? (data.headers as string[]) : [],
    rows: Array.isArray(data.rows) ? (data.rows as Array<Record<string, string>>) : [],
    total_rows: Number(data.total_rows || 0),
    preview_truncated: Boolean(data.preview_truncated),
    column_non_empty_counts:
      data.column_non_empty_counts && typeof data.column_non_empty_counts === 'object'
        ? (data.column_non_empty_counts as Record<string, number>)
        : {},
    column_numeric_counts:
      data.column_numeric_counts && typeof data.column_numeric_counts === 'object'
        ? (data.column_numeric_counts as Record<string, number>)
        : {},
    column_positive_numeric_counts:
      data.column_positive_numeric_counts && typeof data.column_positive_numeric_counts === 'object'
        ? (data.column_positive_numeric_counts as Record<string, number>)
        : {},
  };
}

export async function fetchMmpLifecycleMethods(): Promise<MmpLifecycleMethod[]> {
  const res = await requestBackend('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods', {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  const data = await parseJsonOrError(res, 'Failed to fetch experiment methods');
  return Array.isArray(data.methods) ? (data.methods as MmpLifecycleMethod[]) : [];
}

export async function fetchMmpLifecycleMethodUsage(): Promise<MmpLifecycleMethodUsage[]> {
  const res = await requestBackend('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/usage', {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  const data = await parseJsonOrError(res, 'Failed to fetch experiment method usage');
  return Array.isArray(data.usage) ? (data.usage as MmpLifecycleMethodUsage[]) : [];
}

export async function createMmpLifecycleMethod(payload: {
  key: string;
  name: string;
  output_property: string;
  database_id?: string;
  input_unit?: string;
  output_unit?: string;
  display_unit?: string;
  import_transform?: string;
  display_transform?: string;
  category?: string;
  description?: string;
  reference?: string;
}): Promise<MmpLifecycleMethod> {
  const res = await requestBackend('/api/admin/lead_optimization/mmp_lifecycle/experiment_methods', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload)
  });
  const data = await parseJsonOrError(res, 'Failed to create experiment method');
  return (data.method || {}) as MmpLifecycleMethod;
}

export async function patchMmpLifecycleMethod(methodId: string, payload: Record<string, unknown>): Promise<MmpLifecycleMethod> {
  const safeId = encodeURIComponent(String(methodId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/${safeId}`, {
    method: 'PATCH',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  const data = await parseJsonOrError(res, 'Failed to update experiment method');
  return (data.method || {}) as MmpLifecycleMethod;
}

export async function deleteMmpLifecycleMethod(
  methodId: string,
  payload?: {
    database_id?: string;
    purge_database_data?: boolean;
    confirm_output_property?: string;
  }
): Promise<Record<string, unknown>> {
  const safeId = encodeURIComponent(String(methodId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/experiment_methods/${safeId}`, {
    method: 'DELETE',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  return (await parseJsonOrError(res, 'Failed to delete experiment method')) as Record<string, unknown>;
}

export async function fetchMmpLifecycleDatabaseSyncQueue(options?: {
  database_id?: string;
  include_applied?: boolean;
}): Promise<{
  rows: MmpLifecyclePendingDatabaseSync[];
  pending_by_database: Record<string, number>;
}> {
  const search = new URLSearchParams();
  if (String(options?.database_id || '').trim()) search.set('database_id', String(options?.database_id || '').trim());
  if (options?.include_applied) search.set('include_applied', '1');
  const suffix = search.toString() ? `?${search.toString()}` : '';
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/database_sync_queue${suffix}`, {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  const data = await parseJsonOrError(res, 'Failed to fetch pending database sync queue');
  return {
    rows: Array.isArray(data.rows) ? (data.rows as MmpLifecyclePendingDatabaseSync[]) : [],
    pending_by_database:
      data.pending_by_database && typeof data.pending_by_database === 'object'
        ? (data.pending_by_database as Record<string, number>)
        : {}
  };
}

export async function fetchMmpLifecyclePropertyMappings(databaseId: string): Promise<MmpLifecyclePropertyMapping[]> {
  const safeId = encodeURIComponent(String(databaseId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/property_mappings?database_id=${safeId}`, {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  const data = await parseJsonOrError(res, 'Failed to fetch property mappings');
  return Array.isArray(data.mappings) ? (data.mappings as MmpLifecyclePropertyMapping[]) : [];
}

export async function saveMmpLifecyclePropertyMappings(
  databaseId: string,
  mappings: Array<{
    id?: string;
    source_property: string;
    mmp_property: string;
    method_id?: string;
    value_transform?: string;
    notes?: string;
  }>
): Promise<MmpLifecyclePropertyMapping[]> {
  const safeId = encodeURIComponent(String(databaseId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/property_mappings/${safeId}`, {
    method: 'PUT',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify({ mappings })
  });
  const data = await parseJsonOrError(res, 'Failed to save property mappings');
  return Array.isArray(data.mappings) ? (data.mappings as MmpLifecyclePropertyMapping[]) : [];
}

export async function fetchMmpLifecycleDatabaseProperties(databaseId: string): Promise<Array<Record<string, unknown>>> {
  const safeId = encodeURIComponent(String(databaseId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/database_properties/${safeId}`, {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  const data = await parseJsonOrError(res, 'Failed to fetch database properties');
  return Array.isArray(data.properties) ? (data.properties as Array<Record<string, unknown>>) : [];
}

export async function checkMmpLifecycleBatch(
  batchId: string,
  payload: {
    database_id: string;
    row_limit?: number;
    check_policy?: Record<string, unknown>;
  }
): Promise<Record<string, unknown>> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/check`, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload)
  });
  return (await parseJsonOrError(res, 'Failed to check lifecycle batch')) as Record<string, unknown>;
}

export async function applyMmpLifecycleBatch(
  batchId: string,
  payload: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/apply`, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  return (await parseJsonOrError(res, 'Failed to apply lifecycle batch')) as Record<string, unknown>;
}

export async function rollbackMmpLifecycleBatch(
  batchId: string,
  payload: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const safeId = encodeURIComponent(String(batchId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_lifecycle/batches/${safeId}/rollback`, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  return (await parseJsonOrError(res, 'Failed to rollback lifecycle batch')) as Record<string, unknown>;
}

export async function fetchMmpLifecycleMetrics(databaseId: string, recentLimit = 10): Promise<Record<string, unknown>> {
  const safeId = encodeURIComponent(String(databaseId || '').trim());
  const safeLimit = Number.isFinite(recentLimit) ? Math.max(1, Math.min(50, Math.trunc(recentLimit))) : 10;
  const res = await requestBackend(
    `/api/admin/lead_optimization/mmp_lifecycle/metrics/${safeId}?recent_limit=${safeLimit}`,
    {
      method: 'GET',
      headers: {
        ...API_HEADERS,
        Accept: 'application/json'
      }
    }
  );
  return (await parseJsonOrError(res, 'Failed to fetch lifecycle metrics')) as Record<string, unknown>;
}
