export type TaskState = 'DRAFT' | 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' | 'REVOKED';
export type MoleculeType = 'protein' | 'dna' | 'rna' | 'ligand';
export type LigandInputMethod = 'smiles' | 'ccd' | 'jsme';
export type PredictionConstraintType = 'contact' | 'bond' | 'pocket';

export interface InputComponent {
  id: string;
  type: MoleculeType;
  numCopies: number;
  sequence: string;
  useMsa?: boolean;
  cyclic?: boolean;
  inputMethod?: LigandInputMethod;
}

export interface ProteinTemplateUpload {
  fileName: string;
  format: 'pdb' | 'cif';
  content: string;
  chainId: string;
  chainSequences: Record<string, string>;
}

export interface PredictionTemplateUpload {
  fileName: string;
  format: 'pdb' | 'cif';
  content: string;
  templateChainId: string;
  targetChainIds: string[];
}

export interface ContactConstraint {
  id: string;
  type: 'contact';
  token1_chain: string;
  token1_residue: number;
  token2_chain: string;
  token2_residue: number;
  max_distance: number;
  force: boolean;
}

export interface BondConstraint {
  id: string;
  type: 'bond';
  atom1_chain: string;
  atom1_residue: number;
  atom1_atom: string;
  atom2_chain: string;
  atom2_residue: number;
  atom2_atom: string;
}

export interface PocketConstraint {
  id: string;
  type: 'pocket';
  binder: string;
  contacts: Array<[string, number]>;
  max_distance: number;
  force: boolean;
}

export type PredictionConstraint = ContactConstraint | BondConstraint | PocketConstraint;

export interface PredictionProperties {
  affinity: boolean;
  target: string | null;
  ligand: string | null;
  binder: string | null;
}

export interface PredictionOptions {
  seed: number | null;
}

export interface ProjectInputConfig {
  version: 1;
  components: InputComponent[];
  constraints: PredictionConstraint[];
  properties: PredictionProperties;
  options: PredictionOptions;
}

export interface ProjectTaskCounts {
  total: number;
  running: number;
  success: number;
  failure: number;
  queued: number;
  other: number;
}

export interface AppUser {
  id: string;
  username: string;
  name: string;
  email: string | null;
  avatar_url?: string | null;
  password_hash: string;
  is_admin: boolean;
  last_login_at: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface Project {
  id: string;
  user_id: string | null;
  name: string;
  summary: string;
  backend: string;
  use_msa: boolean;
  protein_sequence: string;
  ligand_smiles: string;
  color_mode: string;
  task_type: string;
  task_id: string;
  task_state: TaskState;
  status_text: string;
  error_text: string;
  confidence: Record<string, unknown>;
  affinity: Record<string, unknown>;
  submitted_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  structure_name: string;
  task_counts?: ProjectTaskCounts;
  created_at: string;
  updated_at: string;
  deleted_at: string | null;
}

export interface ProjectTask {
  id: string;
  project_id: string;
  name: string;
  summary: string;
  task_id: string;
  task_state: TaskState;
  status_text: string;
  error_text: string;
  backend: string;
  seed: number | null;
  protein_sequence: string;
  ligand_smiles: string;
  components: InputComponent[];
  constraints: PredictionConstraint[];
  properties: PredictionProperties;
  confidence: Record<string, unknown>;
  affinity: Record<string, unknown>;
  structure_name: string;
  submitted_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
  created_at: string;
  updated_at: string;
}

export interface Session {
  userId: string;
  username: string;
  name: string;
  email?: string | null;
  avatarUrl?: string | null;
  isAdmin: boolean;
  loginAt: string;
}

export interface AuthRegisterInput {
  username: string;
  name: string;
  email?: string;
  password: string;
}

export interface AuthLoginInput {
  identifier: string;
  password: string;
}

export interface PredictionSubmitInput {
  projectId: string;
  projectName: string;
  proteinSequence: string;
  ligandSmiles: string;
  components?: InputComponent[];
  constraints?: PredictionConstraint[];
  properties?: PredictionProperties;
  seed?: number | null;
  backend: string;
  useMsa: boolean;
  templateUploads?: PredictionTemplateUpload[];
}

export interface AffinityPreviewPayload {
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  structureName: string;
  targetStructureText: string;
  targetStructureFormat: 'cif' | 'pdb';
  ligandStructureText: string;
  ligandStructureFormat: 'cif' | 'pdb';
  ligandSmiles: string;
  targetChainIds: string[];
  ligandChainId: string;
  hasLigand: boolean;
  ligandIsSmallMolecule: boolean;
  supportsActivity: boolean;
  proteinFileName: string;
  ligandFileName: string;
}

export interface AffinitySubmitInput {
  inputStructureText: string;
  inputStructureName?: string;
  targetFile?: File | null;
  ligandFile?: File | null;
  backend?: string;
  seed?: number | null;
  enableAffinity: boolean;
  ligandSmiles?: string;
  targetChainIds?: string[];
  ligandChainId?: string;
  affinityRefine?: boolean;
  useMsa?: boolean;
  useTemplate?: boolean;
}

export interface TaskStatusResponse {
  task_id: string;
  state: string;
  info?: Record<string, unknown>;
}

export interface ParsedResultBundle {
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  structureName: string;
  confidence: Record<string, unknown>;
  affinity: Record<string, unknown>;
}
