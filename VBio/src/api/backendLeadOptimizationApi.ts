import { API_HEADERS, requestBackend, requestManagement } from './backendClient';

export interface LeadOptFragmentPreviewResponse {
  smiles: string;
  fragments: Array<{
    fragment_id: string;
    smiles: string;
    atom_indices: number[];
    heavy_atoms: number;
    recommended_action: string;
    color: string;
    rule_coverage: number;
    quality_score: number;
    num_frags?: number;
  }>;
  recommended_variable_fragment_ids: string[];
  auto_generated_rules: {
    variable_smarts: string;
    variable_const_smarts: string;
  };
}

export interface LeadOptReferencePreviewResponse {
  target_chain_ids: string[];
  target_chain_sequences?: Record<string, string>;
  ligand_chain_id: string;
  ligand_smiles: string;
  supports_activity: boolean;
  complex_structure_text?: string;
  complex_structure_format?: 'cif' | 'pdb';
  structure_text: string;
  structure_format: 'cif' | 'pdb';
  overlay_structure_text: string;
  overlay_structure_format: 'cif' | 'pdb';
  pocket_residues: Array<{
    chain_id: string;
    residue_name: string;
    residue_number: number;
    min_distance: number;
    interaction_types: string[];
  }>;
  ligand_atom_contacts: Array<{
    atom_index: number;
    chain_id?: string;
    residue_name?: string;
    residue_number?: number;
    atom_name?: string;
    residues: Array<{
      chain_id: string;
      residue_name: string;
      residue_number: number;
      min_distance: number;
    }>;
  }>;
  ligand_atom_map?: Array<{
    atom_index: number;
    chain_id: string;
    residue_name?: string;
    residue_number?: number;
    atom_name?: string;
  }>;
}

export interface LeadOptPocketOverlayResponse {
  overlay_structure_text: string;
  overlay_structure_format: 'cif' | 'pdb';
  residue_count: number;
}

export interface LeadOptMmpQueryResponse {
  task_id?: string;
  state?: string;
  query_id: string;
  query_mode: string;
  mmp_database_id?: string;
  mmp_database_label?: string;
  mmp_database_schema?: string;
  transforms: Array<Record<string, unknown>>;
  global_transforms?: Array<Record<string, unknown>>;
  clusters: Array<Record<string, unknown>>;
  count: number;
  global_count?: number;
  min_pairs?: number;
  stats?: Record<string, unknown>;
}

export interface LeadOptMmpDatabaseProperty {
  name: string;
  label?: string;
  display_name?: string;
  base?: string;
  unit?: string;
  display_base?: string;
  display_unit?: string;
  change_displayed?: string;
}

export interface LeadOptMmpDatabaseItem {
  id: string;
  label: string;
  description?: string;
  backend: 'postgres' | string;
  schema?: string;
  visible?: boolean;
  is_default?: boolean;
  source?: string;
  properties: LeadOptMmpDatabaseProperty[];
}

export interface LeadOptMmpDatabaseCatalogResponse {
  default_database_id?: string;
  databases: LeadOptMmpDatabaseItem[];
  total?: number;
  total_visible?: number;
  total_all?: number;
}

interface LeadOptMmpQueryStatusResponse {
  task_id?: string;
  state?: string;
  progress?: Record<string, unknown>;
  result?: LeadOptMmpQueryResponse;
  error?: string;
}

export interface LeadOptMmpEvidenceResponse {
  transform_id: string;
  transform: Record<string, unknown>;
  pairs: Array<Record<string, unknown>>;
  n_pairs: number;
}

export async function previewLeadOptimizationFragments(smiles: string): Promise<LeadOptFragmentPreviewResponse> {
  const res = await requestBackend('/api/lead_optimization/fragment_preview', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify({ smiles })
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to preview fragments (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptFragmentPreviewResponse;
}

export async function previewLeadOptimizationReference(
  referenceTargetFile: File,
  referenceLigandFile: File
): Promise<LeadOptReferencePreviewResponse> {
  const form = new FormData();
  form.append('reference_target_file', referenceTargetFile);
  form.append('reference_ligand_file', referenceLigandFile);
  const res = await requestBackend('/api/lead_optimization/reference_preview', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    },
    body: form
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to preview reference complex (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptReferencePreviewResponse;
}

export async function previewLeadOptimizationPocketOverlay(payload: {
  complex_structure_text: string;
  complex_structure_format: 'cif' | 'pdb';
  ligand_chain_id: string;
  residues: Array<{ chain_id: string; residue_number: number }>;
}): Promise<LeadOptPocketOverlayResponse> {
  const res = await requestManagement('/vbio-api/api/lead_optimization/pocket_overlay', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to build pocket overlay (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptPocketOverlayResponse;
}

export async function queryLeadOptimizationMmp(
  payload: Record<string, unknown>,
  options?: { onEnqueued?: (taskId: string) => void | Promise<void> }
): Promise<LeadOptMmpQueryResponse> {
  const enqueue = await requestBackend('/api/lead_optimization/mmp_query', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify({
      ...(payload || {}),
      async: true
    })
  });
  if (!enqueue.ok) {
    const text = await enqueue.text();
    throw new Error(`Failed to query MMP (${enqueue.status}): ${text}`);
  }
  const enqueueData = (await enqueue.json()) as { task_id?: string; state?: string } & LeadOptMmpQueryResponse;
  if (!enqueueData.task_id) {
    if (Array.isArray(enqueueData.transforms)) {
      return enqueueData as LeadOptMmpQueryResponse;
    }
    throw new Error('MMP query enqueue response did not include task_id.');
  }

  const taskId = String(enqueueData.task_id || '').trim();
  if (taskId && typeof options?.onEnqueued === 'function') {
    await options.onEnqueued(taskId);
  }
  while (true) {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    const statusRes = await requestBackend(`/api/lead_optimization/mmp_query_status/${encodeURIComponent(taskId)}`, {
      method: 'GET',
      headers: {
        ...API_HEADERS,
        Accept: 'application/json'
      }
    });
    if (!statusRes.ok) {
      const text = await statusRes.text();
      throw new Error(`Failed to query MMP status (${statusRes.status}): ${text}`);
    }
    const statusData = (await statusRes.json()) as LeadOptMmpQueryStatusResponse;
    const state = String(statusData.state || '').toUpperCase();
    if (state === 'SUCCESS') {
      const result = statusData.result;
      if (!result) {
        throw new Error('MMP query finished but result payload is empty.');
      }
      return {
        ...result,
        task_id: taskId,
        state
      };
    }
    if (state === 'FAILURE') {
      throw new Error(statusData.error || 'MMP query task failed.');
    }
  }
}

export async function fetchLeadOptimizationMmpDatabases(options?: {
  includeHidden?: boolean;
}): Promise<LeadOptMmpDatabaseCatalogResponse> {
  const path = options?.includeHidden
    ? '/api/admin/lead_optimization/mmp_databases'
    : '/api/lead_optimization/mmp_databases';
  const res = await requestBackend(path, {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to list MMP databases (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptMmpDatabaseCatalogResponse;
}

export async function patchLeadOptimizationMmpDatabaseAdmin(
  databaseId: string,
  patch: {
    visible?: boolean;
    label?: string;
    description?: string;
    is_default?: boolean;
  }
): Promise<LeadOptMmpDatabaseCatalogResponse> {
  const safeId = encodeURIComponent(String(databaseId || '').trim());
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_databases/${safeId}`, {
    method: 'PATCH',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(patch || {})
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to patch MMP database (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptMmpDatabaseCatalogResponse;
}

export async function deleteLeadOptimizationMmpDatabaseAdmin(
  databaseId: string,
  options?: { dropData?: boolean }
): Promise<LeadOptMmpDatabaseCatalogResponse> {
  const safeId = encodeURIComponent(String(databaseId || '').trim());
  const dropData = options?.dropData !== false ? 'true' : 'false';
  const res = await requestBackend(`/api/admin/lead_optimization/mmp_databases/${safeId}?drop_data=${dropData}`, {
    method: 'DELETE',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to delete MMP database (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptMmpDatabaseCatalogResponse;
}

export async function queryLeadOptimizationMmpSync(payload: Record<string, unknown>): Promise<LeadOptMmpQueryResponse> {
  const res = await requestBackend('/api/lead_optimization/mmp_query', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify({
      ...(payload || {}),
      async: false
    })
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to query MMP (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptMmpQueryResponse;
}

export async function clusterLeadOptimizationMmp(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const res = await requestBackend('/api/lead_optimization/mmp_cluster', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to cluster MMP transforms (${res.status}): ${text}`);
  }
  return (await res.json()) as Record<string, unknown>;
}

export async function fetchLeadOptimizationMmpQueryResult(queryId: string): Promise<LeadOptMmpQueryResponse> {
  const safeQueryId = encodeURIComponent(String(queryId || '').trim());
  const res = await requestBackend(`/api/lead_optimization/mmp_query_result/${safeQueryId}`, {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch MMP query result (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptMmpQueryResponse;
}

export async function fetchLeadOptimizationMmpEvidence(transformId: string): Promise<LeadOptMmpEvidenceResponse> {
  const safeTransformId = encodeURIComponent(String(transformId || '').trim());
  const res = await requestBackend(`/api/lead_optimization/mmp_evidence/${safeTransformId}`, {
    method: 'GET',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch MMP evidence (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptMmpEvidenceResponse;
}

export async function enumerateLeadOptimizationMmp(payload: Record<string, unknown>): Promise<Record<string, unknown>> {
  const res = await requestBackend('/api/lead_optimization/mmp_enumerate', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify(payload || {})
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to enumerate MMP candidates (${res.status}): ${text}`);
  }
  return (await res.json()) as Record<string, unknown>;
}

export async function predictLeadOptimizationCandidate(payload: {
  candidateSmiles: string;
  proteinSequence?: string;
  backend: string;
  targetChain: string;
  ligandChain: string;
  pocketResidues: Array<Record<string, unknown>>;
  referenceTemplateStructureText?: string;
  referenceTemplateFormat?: 'cif' | 'pdb';
  useMsaServer?: boolean;
  seed?: number | null;
}): Promise<string> {
  const res = await requestBackend('/api/lead_optimization/predict_candidate', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      'Content-Type': 'application/json',
      Accept: 'application/json'
    },
    body: JSON.stringify({
      candidate_smiles: payload.candidateSmiles,
      protein_sequence: payload.proteinSequence || '',
      backend: payload.backend,
      target_chain: payload.targetChain,
      ligand_chain: payload.ligandChain,
      pocket_residues: payload.pocketResidues || [],
      reference_template_structure_text: payload.referenceTemplateStructureText || '',
      reference_template_structure_format: payload.referenceTemplateFormat || 'cif',
      use_msa_server: payload.useMsaServer ?? true,
      seed: payload.seed ?? null,
      priority: 'high'
    })
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to submit candidate prediction (${res.status}): ${text}`);
  }
  const data = (await res.json()) as { task_id?: string };
  if (!data.task_id) {
    throw new Error('Candidate prediction response did not include task_id.');
  }
  return data.task_id;
}
