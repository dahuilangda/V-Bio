export type TaskState = 'DRAFT' | 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' | 'REVOKED';
export type MoleculeType = 'protein' | 'dna' | 'rna' | 'ligand';
export type LigandInputMethod = 'smiles' | 'ccd' | 'jsme';
export type ProteinModificationInputMethod = 'ccd' | 'jsme';
export type ProteinModificationTerminal = 'internal' | 'n_term' | 'c_term';
export type PredictionConstraintType = 'contact' | 'bond' | 'pocket';
export type PeptideDesignMode = 'linear' | 'cyclic' | 'bicyclic';
export type PeptideResiduePoolKind = 'natural' | 'preset' | 'custom';
export type AffinityScoringMode = 'score' | 'pose' | 'refine' | 'interface' | 'dock';

/** Serializable dock-mode pocket definition persisted in inputConfig.options. */
export interface AffinityDockPocket {
  centerX: number;
  centerY: number;
  centerZ: number;
  sizeX: number;
  sizeY: number;
  sizeZ: number;
  method: 'residues' | 'manual' | 'ligand';
}

// Manual backbone atom assignment for a custom non-natural residue, as 0-based RDKit
// heavy-atom indices — the same index space as the 2D depiction and the backend CCD builder.
// When present it overrides automatic backbone detection end-to-end (single source of truth).
export interface CustomResidueBackbone {
  n: number;
  ca: number;
  c: number;
  o: number;
  oxt: number;
}

export interface PeptideResiduePoolSelection {
  code: string;
  kind: PeptideResiduePoolKind;
  // Custom residues carry their own CCD source (SMILES + parent AA) so the definition
  // travels with the selection in the persisted config and reaches AF3/Protenix/Boltz
  // as a CCD regardless of the ephemeral in-memory residue library.
  smiles?: string;
  baseResidue?: string;
  label?: string;
  backbone?: CustomResidueBackbone;
  // When true the residue's C-terminus is -CONH2 (amide) instead of -COOH (carboxyl). The 5th
  // backbone slot then points at the amide nitrogen (NXT, non-leaving); the residue may only
  // occupy the C-terminal position of a linear chain.
  cTerminalAmidated?: boolean;
}

export interface ProteinModification {
  id: string;
  position: number;
  terminal?: ProteinModificationTerminal;
  customEditorCollapsed?: boolean;
  baseResidue: string;
  ccd: string;
  inputMethod: ProteinModificationInputMethod;
  smiles?: string;
  label?: string;
  backbone?: CustomResidueBackbone;
  cTerminalAmidated?: boolean;
}

export interface InputComponent {
  id: string;
  type: MoleculeType;
  numCopies: number;
  sequence: string;
  useMsa?: boolean;
  cyclic?: boolean;
  inputMethod?: LigandInputMethod;
  modifications?: ProteinModification[];
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

export type VirtualScreeningStructureBackend = 'boltz' | 'protenix' | 'alphafold3';
export type VirtualScreeningStructureState = 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE';

// Small, durable metadata for a structure job launched from a virtual-screening hit.
// The structure itself stays in the runtime result archive and is loaded only when opened.
export interface VirtualScreeningPredictionRecord {
  taskId: string;
  backend: VirtualScreeningStructureBackend;
  state: VirtualScreeningStructureState;
  ligandPlddt: number | null;
  interfaceMetricValue: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  pairIptm: number | null;
  pairPae: number | null;
  error: string;
  updatedAt: number;
  /** Hydrated lazily when a user opens a completed hit. */
  structureText?: string;
  structureFormat?: 'cif' | 'pdb';
  structureName?: string;
  ligandRenderSmiles?: string;
  ligandRenderAtomPlddts?: number[];
  resultBundleHydrated?: boolean;
}

export interface PredictionOptions {
  seed: number | null;
  virtualScreeningInput?: string;
  virtualScreeningInputMode?: 'upload' | 'paste';
  virtualScreeningInputFileName?: string;
  virtualScreeningPredictions?: Record<string, VirtualScreeningPredictionRecord>;
  affinityMode?: AffinityScoringMode;
  affinityDockPocket?: AffinityDockPocket | null;
  peptideDesignMode?: PeptideDesignMode;
  peptideChirality?: 'l' | 'd';
  peptideBinderLength?: number;
  peptideLengthMin?: number;
  peptideLengthMax?: number;
  peptideUseInitialSequence?: boolean;
  peptideInitialSequence?: string;
  peptideSequenceMask?: string;
  peptideIterations?: number;
  peptidePopulationSize?: number;
  peptideEliteSize?: number;
  peptideResiduePool?: PeptideResiduePoolSelection[];
  peptideCustomResidueDefinitions?: CustomCcdMoleculeInput[];
  peptideNonNaturalMin?: number;
  peptideNonNaturalMax?: number;
  peptideBicyclicLinkerCcd?: 'SEZ' | '29N' | 'BS3';
  peptideStructureUpload?: {
    fileName: string;
    format: 'pdb' | 'cif';
    content: string;
    chainId: string;
  } | null;
  peptideBicyclicCysPositionMode?: 'auto' | 'manual';
  /**
   * How the three Cys anchors follow the (possibly ranged) peptide length:
   * ring = pinned ring sizes with a rigid C-terminal block; ratio =
   * percentage-scaled anchors; absolute = literal positions (fixed length
   * only); auto = engine layout. Legacy `manual` mode maps to `absolute`.
   */
  peptideBicyclicCysLayout?: 'auto' | 'ring' | 'ratio' | 'absolute';
  peptideBicyclicRing1?: number;
  peptideBicyclicRing2?: number;
  peptideBicyclicRatio1?: number;
  peptideBicyclicRatio2?: number;
  peptideBicyclicRatio3?: number;
  peptideBicyclicFixTerminalCys?: boolean;
  peptideBicyclicIncludeExtraCys?: boolean;
  peptideBicyclicCys1Pos?: number;
  peptideBicyclicCys2Pos?: number;
  peptideBicyclicCys3Pos?: number;
  peptidePocketCenter?: string;
  peptidePocketResidues?: string;
  peptidePocketBox?: number;
  peptideDockPocket?: AffinityDockPocket | null;
  /** Lead optimization (HALO): interactive pocket box on the target upload. */
  leadOptDockPocket?: AffinityDockPocket | null;
  /** Derived pocket center "x,y,z" submitted with the run (empty = blind). */
  leadOptPocketCenter?: string;
  leadOptMode?: 'denovo' | 'fragment' | 'scaffold_hop';
  leadOptBackend?: 'protenix2dock' | 'boltz2dock' | 'alphafold3';
  leadOptRounds?: number;
  leadOptBudgetPerRound?: number;
  leadOptScaffoldHopRatio?: number;
  leadOptReferenceSmiles?: string;
  leadOptKeepFragmentSmiles?: string;
  leadOptEditAtomIndices?: string;
  lowVram?: boolean;
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

export type ProjectAccessScope = 'owner' | 'project_share' | 'task_share';
export type ShareAccessLevel = 'viewer' | 'editor';
export type EffectiveAccessLevel = 'viewer' | 'editor' | 'owner';

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

export interface ApiToken {
  id: string;
  user_id: string;
  name: string;
  token_hash: string;
  token_plain: string;
  token_prefix: string;
  token_last4: string;
  project_id: string | null;
  allow_submit: boolean;
  allow_delete: boolean;
  allow_cancel: boolean;
  scopes: string[];
  is_active: boolean;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ApiTokenUsage {
  id: string;
  token_id: string | null;
  user_id: string | null;
  method: string;
  path: string;
  action: string;
  status_code: number;
  succeeded: boolean;
  duration_ms: number | null;
  client: string;
  meta: Record<string, unknown>;
  created_at: string;
}

export interface ApiTokenUsageDaily {
  token_id: string;
  usage_day: string;
  total_count: number;
  success_count: number;
  error_count: number;
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
  access_scope?: ProjectAccessScope;
  access_level?: EffectiveAccessLevel;
  accessible_task_ids?: string[];
  editable_task_ids?: string[];
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
  access_scope?: ProjectAccessScope;
  access_level?: EffectiveAccessLevel;
  created_at: string;
  updated_at: string;
}

export interface ProjectShareRecord {
  id: string;
  project_id: string;
  user_id: string;
  granted_by_user_id: string | null;
  access_level: ShareAccessLevel;
  created_at: string;
  updated_at: string;
  project_name?: string;
  project_summary?: string;
  target_username?: string;
  target_name?: string;
  granted_by_username?: string;
  granted_by_name?: string;
}

export interface ProjectTaskShareRecord {
  id: string;
  project_id: string;
  project_task_id: string;
  user_id: string;
  granted_by_user_id: string | null;
  access_level: ShareAccessLevel;
  created_at: string;
  updated_at: string;
  project_name?: string;
  project_summary?: string;
  task_name?: string;
  task_summary?: string;
  target_username?: string;
  target_name?: string;
  granted_by_username?: string;
  granted_by_name?: string;
}

export type CopilotMessageRole = 'user' | 'assistant' | 'system';

export type CopilotContextType = 'project_list' | 'task_list' | 'task_detail';

export interface ProjectCopilotMessage {
  id: string;
  context_type: CopilotContextType;
  project_id: string | null;
  project_task_id: string | null;
  user_id: string | null;
  role: CopilotMessageRole;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  username?: string;
  user_name?: string;
}

export interface ProjectCopilotState {
  id: string;
  user_id: string;
  state_key: string;
  data: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type CopilotOperationEffect = 'create' | 'update' | 'delete' | 'execute' | 'navigate';

/**
 * A structured question the planner asks the user to resolve an ambiguity before planning (e.g.
 * which task type or modeling backend). The frontend renders these as clickable choice chips when
 * kind=choice, a yes/no pair when kind=confirm, or a highlighted prompt when kind=freeform.
 */
export type CopilotQuestionKind = 'choice' | 'confirm' | 'freeform';

export interface CopilotQuestionOption {
  label: string;
  value: string;
  hint?: string;
}

export interface CopilotPlannerQuestion {
  text: string;
  kind: CopilotQuestionKind;
  options?: CopilotQuestionOption[];
  defaultValue?: string;
  /** False disables the free-text "Other ___" answer on choice questions. Default: enabled. */
  allowOther?: boolean;
}

/**
 * One observable step in the planner loop (model call → harness audit → read-skill
 * observation → terminal). Returned by the Copilot turn endpoint as a `trace[]` so the
 * UI can show how the planner reasoned, general and domain-agnostic (event names + compact
 * detail, never payload bodies). Mirrors the agent-trace pattern (OTel GenAI / agent SDKs).
 */
export interface CopilotTraceStep {
  round: number;
  event: string;
  detail?: Record<string, unknown>;
}

export interface CopilotPlanAction {
  id: string;
  operation_id?: string;
  plan_id?: string;
  sequence?: number;
  label: string;
  description: string;
  arguments?: Record<string, unknown>;
  payload?: Record<string, unknown>;
  effect?: CopilotOperationEffect;
  needs_confirmation?: boolean;
  execute_now?: boolean;
}

export type AuthProviderType = 'local' | 'jwt';

export interface Session {
  userId: string;
  username: string;
  name: string;
  email?: string | null;
  avatarUrl?: string | null;
  isAdmin: boolean;
  isSuperAdmin?: boolean;
  loginAt: string;
  authProvider?: AuthProviderType;
  managementToken?: string | null;
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
  workflow?: 'prediction' | 'peptide_design' | 'virtual_screening';
  virtualScreeningInput?: string;
  components?: InputComponent[];
  constraints?: PredictionConstraint[];
  properties?: PredictionProperties;
  peptideDesignOptions?: PredictionOptions;
  peptideDesignTargetChainId?: string | null;
  peptideStructureUpload?: {
    fileName: string;
    format: 'pdb' | 'cif';
    content: string;
    chainId: string;
  } | null;
  seed?: number | null;
  backend: string;
  useMsa: boolean;
  templateUploads?: PredictionTemplateUpload[];
  customCcdMolecules?: CustomCcdMoleculeInput[];
  lowVram?: boolean;
}

export interface CustomCcdMoleculeInput {
  ccd: string;
  smiles: string;
  baseResidue?: string;
  label?: string;
  kind?: 'residue' | 'ligand';
  backbone?: CustomResidueBackbone;
  cTerminalAmidated?: boolean;
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
  projectId: string;
  inputStructureText: string;
  inputStructureName?: string;
  targetFile?: File | null;
  ligandFile?: File | null;
  backend?: string;
  seed?: number | null;
  mode?: AffinityScoringMode;
  computeIpsae?: boolean;
  enableAffinity: boolean;
  ligandSmiles?: string;
  targetChainIds?: string[];
  ligandChainId?: string;
  affinityRefine?: boolean;
  useMsa?: boolean;
  useTemplate?: boolean;
  dockPocket?: AffinityDockPocket | null;
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
