import { API_HEADERS, requestBackend } from './backendClient';

export interface LeadOptFragmentPreviewResponse {
  smiles: string;
  fragments: Array<{
    fragment_id: string;
    smiles: string;
    query_smiles?: string;
    display_smiles?: string;
    atom_indices: number[];
    heavy_atoms: number;
    attachment_count?: number;
    recommended_action: string;
    color: string;
    rule_coverage: number;
    quality_score: number;
    num_frags?: number;
  }>;
  atom_bonds?: Array<[number, number]>;
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
    chain_id?: string;
    residue_number?: number;
    residue_name?: string;
    atom_name?: string;
  }>;
}

export type LeadOptHaloMode = 'denovo' | 'fragment' | 'scaffold_hop';
export type LeadOptHaloBackend = 'protenix2dock' | 'boltz2dock' | 'alphafold3';

export interface LeadOptHaloBackendsResponse {
  backends: Array<{ id: string; label: string; default: boolean }>;
  default: string;
  modes: LeadOptHaloMode[];
}

export interface LeadOptHaloOptimizeInput {
  mode: LeadOptHaloMode;
  backend?: LeadOptHaloBackend;
  protein_upload?: {
    content_base64: string;
    file_name: string;
  };
  reference_upload?: {
    content_base64: string;
    file_name: string;
  };
  reference_smiles?: string;
  keep_fragment_smiles?: string;
  edit_atom_indices?: string;
  pocket?: string;
  scaffold_hop_ratio?: number;
  rounds?: number;
  budget_per_round?: number;
  oracle_concurrency?: number;
  target_chain?: string;
  priority?: string;
}

export interface LeadOptHaloOptimizeResponse {
  task_id: string;
  mode: LeadOptHaloMode;
  backend: LeadOptHaloBackend;
  queue: string;
}

export interface LeadOptHaloRoundEvent {
  stage: string;
  round?: number;
  total_rounds?: number;
  message?: string;
  candidates?: number;
  stats?: Record<string, unknown>;
  top_candidates?: Array<Record<string, unknown>>;
  rounds_completed?: number;
}

export interface LeadOptHaloStatusResponse {
  task_id: string;
  state: string;
  status: string;
  info: {
    status?: string;
    details?: string;
    payload?: { halo?: LeadOptHaloRoundEvent };
  } & Record<string, unknown>;
  result?: {
    status?: string;
    summary?: {
      mode?: string;
      backend?: string;
      rounds_completed?: number;
      total_rounds?: number;
      n_candidates?: number;
      top_candidates?: Array<Record<string, unknown>>;
    };
  } & Record<string, unknown>;
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

export async function fetchLeadOptimizationHaloBackends(): Promise<LeadOptHaloBackendsResponse> {
  const res = await requestBackend('/api/lead_optimization/halo_backends', {
    headers: { ...API_HEADERS, Accept: 'application/json' }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to list halo backends (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptHaloBackendsResponse;
}

export async function submitLeadOptimizationHaloOptimize(
  payload: LeadOptHaloOptimizeInput
): Promise<LeadOptHaloOptimizeResponse> {
  const res = await requestBackend('/api/lead_optimization/halo_optimize', {
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
    throw new Error(`Failed to start halo optimization (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptHaloOptimizeResponse;
}

export async function fetchLeadOptimizationHaloStatus(taskId: string): Promise<LeadOptHaloStatusResponse> {
  const normalized = String(taskId || '').trim();
  if (!normalized) throw new Error('taskId is required for halo status.');
  const res = await requestBackend(`/api/lead_optimization/halo_status/${encodeURIComponent(normalized)}`, {
    headers: { ...API_HEADERS, Accept: 'application/json' }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch halo status (${res.status}): ${text}`);
  }
  return (await res.json()) as LeadOptHaloStatusResponse;
}
