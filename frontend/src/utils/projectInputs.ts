function isPeptideStructureUpload(value: unknown): value is {
  fileName: string; format: 'pdb' | 'cif'; content: string; chainId: string;
} {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return typeof v.fileName === 'string'
    && typeof v.content === 'string'
    && (v.format === 'pdb' || v.format === 'cif');
}

import type {
  InputComponent,
  MoleculeType,
  ProteinModification,
  CustomCcdMoleculeInput,
  CustomResidueBackbone,
  PredictionConstraint,
  PredictionOptions,
  PredictionProperties,
  ProjectInputConfig,
  ProteinTemplateUpload,
  VirtualScreeningPredictionRecord,
  AffinityDockPocket
} from '../types/models';

const COMPONENT_KEY = 'vbio_project_input_config_v1';
const UI_STATE_KEY = 'vbio_project_ui_state_v1';
const COMPONENT_ITEM_KEY_PREFIX = `${COMPONENT_KEY}:`;
const UI_STATE_ITEM_KEY_PREFIX = `${UI_STATE_KEY}:`;
const SESSION_KEY = 'vbio_session';
const TEMPLATE_CONTENT_REF_PREFIX = '@pool:';
const QUOTA_ERROR_NAMES = new Set(['QuotaExceededError', 'NS_ERROR_DOM_QUOTA_REACHED']);
const VALID_MOLECULE_TYPES: MoleculeType[] = ['protein', 'ligand', 'dna', 'rna'];
const VALID_LIGAND_INPUT_METHODS = new Set(['smiles', 'ccd', 'jsme']);
const VALID_PROTEIN_MODIFICATION_INPUT_METHODS = new Set(['ccd', 'jsme']);
const VALID_PROTEIN_RESIDUES = new Set(['A', 'R', 'N', 'D', 'C', 'Q', 'E', 'G', 'H', 'I', 'L', 'K', 'M', 'F', 'P', 'S', 'T', 'W', 'Y', 'V']);
const INVALID_COMPONENT_ID_TOKENS = new Set(['undefined', 'null', 'nan']);
const DEFAULT_PEPTIDE_DESIGN_MODE = 'linear';
const VALID_PEPTIDE_DESIGN_MODES = new Set(['linear', 'cyclic', 'bicyclic']);
const VALID_PEPTIDE_MASK_CHARS = new Set(['A', 'R', 'N', 'D', 'C', 'Q', 'E', 'G', 'H', 'I', 'L', 'K', 'M', 'F', 'P', 'S', 'T', 'W', 'Y', 'V', 'X']);
const VALID_PEPTIDE_INITIAL_SEQUENCE_CHARS = new Set([
  'A',
  'R',
  'N',
  'D',
  'C',
  'Q',
  'E',
  'G',
  'H',
  'I',
  'L',
  'K',
  'M',
  'F',
  'P',
  'S',
  'T',
  'W',
  'Y',
  'V'
]);
const DEFAULT_PEPTIDE_BINDER_LENGTH = 20;
const DEFAULT_PEPTIDE_USE_INITIAL_SEQUENCE = false;
const DEFAULT_PEPTIDE_INITIAL_SEQUENCE = '';
const DEFAULT_PEPTIDE_SEQUENCE_MASK = '';
const DEFAULT_PEPTIDE_ITERATIONS = 12;
const DEFAULT_PEPTIDE_POPULATION_SIZE = 16;
const DEFAULT_PEPTIDE_ELITE_SIZE = 5;
const DEFAULT_PEPTIDE_BICYCLIC_LINKER_CCD = 'SEZ';
const VALID_PEPTIDE_BICYCLIC_LINKER_CCD = new Set(['SEZ', '29N', 'BS3']);
const DEFAULT_PEPTIDE_BICYCLIC_CYS_POSITION_MODE = 'auto';
const VALID_PEPTIDE_BICYCLIC_CYS_POSITION_MODES = new Set(['auto', 'manual']);
const DEFAULT_PEPTIDE_BICYCLIC_FIX_TERMINAL_CYS = true;
const DEFAULT_PEPTIDE_BICYCLIC_INCLUDE_EXTRA_CYS = false;
const DEFAULT_PEPTIDE_BICYCLIC_CYS1_POS = 3;
const DEFAULT_PEPTIDE_BICYCLIC_CYS2_POS = 8;
const DEFAULT_PEPTIDE_BICYCLIC_CYS3_POS = 15;
const DEFAULT_AFFINITY_MODE = 'dock';
const VALID_AFFINITY_MODES = new Set(['score', 'pose', 'refine', 'interface', 'dock']);
const VALID_PEPTIDE_POOL_KINDS = new Set(['natural', 'preset', 'custom']);
const DEFAULT_PEPTIDE_RESIDUE_POOL: NonNullable<PredictionOptions['peptideResiduePool']> = [
  'ALA',
  'ARG',
  'ASN',
  'ASP',
  'CYS',
  'GLN',
  'GLU',
  'GLY',
  'HIS',
  'ILE',
  'LEU',
  'LYS',
  'MET',
  'PHE',
  'PRO',
  'SER',
  'THR',
  'TRP',
  'TYR',
  'VAL'
].map((code) => ({ code, kind: 'natural' as const }));

export const PEPTIDE_DESIGNED_LIGAND_TOKEN = '__designed_peptide__';

export interface ProjectUiState {
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  customResidueLibrary?: CustomCcdMoleculeInput[];
  taskProteinTemplates?: Record<string, Record<string, ProteinTemplateUpload>>;
  templateContentPool?: Record<string, string>;
  taskAffinityUploads?: Record<
    string,
    {
      target: { fileName: string; content: string } | null;
      ligand: { fileName: string; content: string } | null;
    }
  >;
  affinityUploads?: {
    target: { fileName: string; content: string } | null;
    ligand: { fileName: string; content: string } | null;
  };
  activeConstraintId: string | null;
  selectedConstraintTemplateComponentId: string | null;
}

export function randomId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return Math.random().toString(36).slice(2);
}

export function createInputComponent(type: MoleculeType): InputComponent {
  if (type === 'ligand') {
    return {
      id: randomId(),
      type: 'ligand',
      numCopies: 1,
      sequence: '',
      inputMethod: 'jsme'
    };
  }

  return {
    id: randomId(),
    type,
    numCopies: 1,
    sequence: '',
    useMsa: type === 'protein',
    cyclic: false
  };
}

function normalizeComponentType(type: unknown): MoleculeType {
  if (typeof type === 'string' && (VALID_MOLECULE_TYPES as string[]).includes(type)) {
    return type as MoleculeType;
  }
  return 'protein';
}

function normalizeComponentId(rawId: unknown, type: MoleculeType, index: number): string {
  if (typeof rawId === 'string') {
    const trimmed = rawId.trim();
    if (trimmed && !INVALID_COMPONENT_ID_TOKENS.has(trimmed.toLowerCase())) {
      return trimmed;
    }
  }
  return `legacy-${type}-${index + 1}`;
}

function normalizeNumCopies(value: unknown): number {
  const num = Number(value);
  if (!Number.isFinite(num) || num < 1) return 1;
  return Math.floor(num);
}

function normalizeLigandInputMethod(value: unknown): 'smiles' | 'ccd' | 'jsme' {
  if (typeof value === 'string' && VALID_LIGAND_INPUT_METHODS.has(value)) {
    return value as 'smiles' | 'ccd' | 'jsme';
  }
  return 'jsme';
}

function normalizeProteinModificationInputMethod(value: unknown): 'ccd' | 'jsme' {
  if (typeof value === 'string' && VALID_PROTEIN_MODIFICATION_INPUT_METHODS.has(value)) {
    return value as 'ccd' | 'jsme';
  }
  return 'ccd';
}

function normalizeProteinModificationCcd(value: unknown): string {
  return typeof value === 'string' ? value.replace(/[^A-Za-z0-9_-]/g, '').toUpperCase().slice(0, 12) : '';
}

function normalizeBackboneOverride(raw: unknown): CustomResidueBackbone | undefined {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
  const obj = raw as Record<string, unknown>;
  const backbone = {} as CustomResidueBackbone;
  for (const slot of ['n', 'ca', 'c', 'o', 'oxt'] as const) {
    const num = Number(obj[slot]);
    if (!Number.isFinite(num) || num < 0 || Math.floor(num) !== num) return undefined;
    backbone[slot] = num;
  }
  return backbone;
}

function normalizeProteinModifications(value: unknown, sequence: string): ProteinModification[] {
  if (!Array.isArray(value)) return [];
  const sequenceLength = sequence.length;
  const usedPositions = new Set<number>();
  const normalized: ProteinModification[] = [];
  value.forEach((item) => {
    if (!item || typeof item !== 'object') return;
    const raw = item as Record<string, unknown>;
    const rawTerminal = typeof raw.terminal === 'string' ? raw.terminal : '';
    const terminal = rawTerminal === 'n_term' || rawTerminal === 'c_term' ? rawTerminal : 'internal';
    const requestedPosition = terminal === 'n_term' ? 1 : terminal === 'c_term' ? sequenceLength || 1 : Math.max(1, Math.floor(Number(raw.position || 1)));
    const position = Math.max(1, Math.floor(Number(requestedPosition || 1)));
    if (!Number.isFinite(position) || position < 1 || (sequenceLength > 0 && position > sequenceLength)) return;
    if (usedPositions.has(position)) return;
    const inputMethod = normalizeProteinModificationInputMethod(raw.inputMethod ?? raw.input_method);
    const ccd = normalizeProteinModificationCcd(raw.ccd ?? raw.ccdCode ?? raw.modification);
    if (!ccd) return;
    const sequenceResidue = sequence[position - 1]?.toUpperCase() || '';
    const rawBaseResidue = typeof raw.baseResidue === 'string' ? raw.baseResidue : typeof raw.base_residue === 'string' ? raw.base_residue : '';
    const baseResidue = rawBaseResidue.trim().toUpperCase().slice(0, 1) || sequenceResidue;
    if (!VALID_PROTEIN_RESIDUES.has(baseResidue)) return;
    const smiles = typeof raw.smiles === 'string' ? raw.smiles.trim() : '';
    if (inputMethod === 'jsme' && !smiles) return;
    usedPositions.add(position);
    normalized.push({
      id: typeof raw.id === 'string' && raw.id.trim() ? raw.id.trim() : randomId(),
      position,
      terminal,
      customEditorCollapsed: Boolean(raw.customEditorCollapsed ?? raw.custom_editor_collapsed),
      baseResidue,
      ccd,
      inputMethod,
      smiles: inputMethod === 'jsme' ? smiles : undefined,
      label: typeof raw.label === 'string' && raw.label.trim() ? raw.label.trim() : undefined,
      backbone: inputMethod === 'jsme' ? normalizeBackboneOverride(raw.backbone) : undefined,
      cTerminalAmidated: Boolean(raw.cTerminalAmidated) || undefined
    });
  });
  return normalized.sort((a, b) => a.position - b.position || a.ccd.localeCompare(b.ccd));
}

export function normalizeInputComponents(components: InputComponent[]): InputComponent[] {
  return components.map((component, index) => {
    const type = normalizeComponentType(component?.type);
    const id = normalizeComponentId(component?.id, type, index);
    const sequence = normalizeComponentSequence(type, typeof component?.sequence === 'string' ? component.sequence : '');
    const numCopies = normalizeNumCopies(component?.numCopies);

    if (type === 'protein') {
      return {
        id,
        type,
        numCopies,
        sequence,
        useMsa: component?.useMsa !== false,
        cyclic: Boolean(component?.cyclic),
        modifications: normalizeProteinModifications((component as unknown as Record<string, unknown>)?.modifications, sequence)
      };
    }

    if (type === 'ligand') {
      return {
        id,
        type,
        numCopies,
        sequence,
        inputMethod: normalizeLigandInputMethod(component?.inputMethod)
      };
    }

    return {
      id,
      type,
      numCopies,
      sequence
    };
  });
}

function normalizePeptideDesignMode(value: unknown): 'linear' | 'cyclic' | 'bicyclic' {
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (VALID_PEPTIDE_DESIGN_MODES.has(normalized)) {
      return normalized as 'linear' | 'cyclic' | 'bicyclic';
    }
  }
  return DEFAULT_PEPTIDE_DESIGN_MODE;
}

function normalizePeptideBicyclicLinkerCcd(value: unknown): 'SEZ' | '29N' | 'BS3' {
  if (typeof value === 'string') {
    const normalized = value.trim().toUpperCase();
    if (VALID_PEPTIDE_BICYCLIC_LINKER_CCD.has(normalized)) {
      return normalized as 'SEZ' | '29N' | 'BS3';
    }
  }
  return DEFAULT_PEPTIDE_BICYCLIC_LINKER_CCD as 'SEZ' | '29N' | 'BS3';
}

function normalizePeptideBicyclicCysPositionMode(value: unknown): 'auto' | 'manual' {
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (VALID_PEPTIDE_BICYCLIC_CYS_POSITION_MODES.has(normalized)) {
      return normalized as 'auto' | 'manual';
    }
  }
  return DEFAULT_PEPTIDE_BICYCLIC_CYS_POSITION_MODE as 'auto' | 'manual';
}

function normalizeAffinityDockPocket(value: unknown): AffinityDockPocket | null {
  if (!value || typeof value !== 'object') return null;
  const obj = value as Record<string, unknown>;
  const nums = ['centerX', 'centerY', 'centerZ', 'sizeX', 'sizeY', 'sizeZ'].map(
    (k) => Number(obj[k])
  );
  if (nums.some((v) => !Number.isFinite(v))) return null;
  if (nums.slice(3).some((v) => v <= 0)) return null;
  const method = String(obj.method || 'manual');
  return {
    centerX: nums[0], centerY: nums[1], centerZ: nums[2],
    sizeX: nums[3], sizeY: nums[4], sizeZ: nums[5],
    method: (['residues', 'manual', 'ligand'].includes(method) ? method : 'manual') as 'residues' | 'manual' | 'ligand'
  };
}

function normalizeAffinityMode(value: unknown): 'score' | 'pose' | 'refine' | 'interface' | 'dock' {
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (VALID_AFFINITY_MODES.has(normalized)) {
      return normalized as 'score' | 'pose' | 'refine' | 'interface' | 'dock';
    }
  }
  return DEFAULT_AFFINITY_MODE as 'score' | 'pose' | 'refine' | 'interface' | 'dock';
}

function readFiniteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

function normalizeIntegerOption(
  value: unknown,
  fallback: number,
  minValue: number,
  maxValue: number
): number {
  const parsed = readFiniteNumber(value);
  if (parsed === null) return fallback;
  return Math.max(minValue, Math.min(maxValue, Math.floor(parsed)));
}

function normalizeBooleanOption(value: unknown, fallback: boolean): boolean {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (normalized === 'true' || normalized === '1' || normalized === 'yes' || normalized === 'on') return true;
    if (normalized === 'false' || normalized === '0' || normalized === 'no' || normalized === 'off') return false;
  }
  return fallback;
}

function normalizePeptideInitialSequence(value: unknown, binderLength: number): string {
  if (typeof value !== 'string') return DEFAULT_PEPTIDE_INITIAL_SEQUENCE;
  const cleaned = value
    .replace(/[\s_-]/g, '')
    .toUpperCase()
    .split('')
    .filter((char) => VALID_PEPTIDE_INITIAL_SEQUENCE_CHARS.has(char))
    .join('');
  return cleaned.slice(0, Math.max(0, binderLength));
}

function normalizePeptideSequenceMask(value: unknown, binderLength: number): string {
  const cleaned = typeof value === 'string' ? value.replace(/[\s_-]/g, '').toUpperCase() : '';
  const normalized = cleaned
    .split('')
    .filter((char) => VALID_PEPTIDE_MASK_CHARS.has(char))
    .join('')
    .slice(0, Math.max(0, binderLength));
  if (binderLength <= 0) return DEFAULT_PEPTIDE_SEQUENCE_MASK;
  if (!normalized) return 'X'.repeat(binderLength);
  return normalized.padEnd(binderLength, 'X');
}

function countPeptideNonNaturalResidues(pool: NonNullable<PredictionOptions['peptideResiduePool']>): number {
  return pool.filter((item) => item.kind !== 'natural').length;
}

function normalizePeptideResiduePool(value: unknown): NonNullable<PredictionOptions['peptideResiduePool']> {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const pool: NonNullable<PredictionOptions['peptideResiduePool']> = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const raw = item as Record<string, unknown>;
    const code = normalizeProteinModificationCcd(raw.code ?? raw.ccd);
    const kind = String(raw.kind || '').trim().toLowerCase();
    const key = `${kind}:${code}`;
    if (!code || !VALID_PEPTIDE_POOL_KINDS.has(kind) || seen.has(key)) continue;
    seen.add(key);
    const entry: NonNullable<PredictionOptions['peptideResiduePool']>[number] = {
      code,
      kind: kind as 'natural' | 'preset' | 'custom'
    };
    if (entry.kind === 'custom') {
      const smiles = String(raw.smiles || '').trim();
      if (smiles) {
        entry.smiles = smiles;
        const baseResidue = String(raw.baseResidue || '').trim().toUpperCase().slice(0, 1);
        if (baseResidue) entry.baseResidue = baseResidue;
        const label = String(raw.label || '').trim().slice(0, 80);
        if (label) entry.label = label;
        if (raw.cTerminalAmidated) entry.cTerminalAmidated = true;
      }
    }
    pool.push(entry);
  }
  return pool.slice(0, 160);
}

export function buildDefaultInputConfig(workflowKey: string | null | undefined = 'prediction'): ProjectInputConfig {
  const normalizedWorkflow = String(workflowKey || '').trim().toLowerCase();
  const isPeptideDesignWorkflow = normalizedWorkflow === 'peptide_design';
  const isVirtualScreeningWorkflow = normalizedWorkflow === 'virtual_screening';
  return {
    version: 1,
    components: [createInputComponent('protein')],
    constraints: [],
    properties: {
      affinity: false,
      target: null,
      ligand: isPeptideDesignWorkflow ? PEPTIDE_DESIGNED_LIGAND_TOKEN : null,
      binder: isPeptideDesignWorkflow ? PEPTIDE_DESIGNED_LIGAND_TOKEN : null
    },
    options: {
      seed: 42,
      virtualScreeningInput: isVirtualScreeningWorkflow ? '' : undefined,
      virtualScreeningInputMode: isVirtualScreeningWorkflow ? 'upload' : undefined,
      virtualScreeningInputFileName: isVirtualScreeningWorkflow ? '' : undefined,
      affinityMode: DEFAULT_AFFINITY_MODE as 'score' | 'pose' | 'refine' | 'interface' | 'dock',
      peptideDesignMode: DEFAULT_PEPTIDE_DESIGN_MODE,
      peptideChirality: 'l' as 'l' | 'd',
      peptideBinderLength: DEFAULT_PEPTIDE_BINDER_LENGTH,
      peptideUseInitialSequence: DEFAULT_PEPTIDE_USE_INITIAL_SEQUENCE,
      peptideInitialSequence: DEFAULT_PEPTIDE_INITIAL_SEQUENCE,
      peptideSequenceMask: 'X'.repeat(DEFAULT_PEPTIDE_BINDER_LENGTH),
      peptideIterations: DEFAULT_PEPTIDE_ITERATIONS,
      peptidePopulationSize: DEFAULT_PEPTIDE_POPULATION_SIZE,
      peptideEliteSize: DEFAULT_PEPTIDE_ELITE_SIZE,
      peptideResiduePool: DEFAULT_PEPTIDE_RESIDUE_POOL,
      peptideNonNaturalMin: 0,
      peptideNonNaturalMax: 0,
      peptideBicyclicLinkerCcd: DEFAULT_PEPTIDE_BICYCLIC_LINKER_CCD as 'SEZ' | '29N' | 'BS3',
      peptideBicyclicCysPositionMode: DEFAULT_PEPTIDE_BICYCLIC_CYS_POSITION_MODE as 'auto' | 'manual',
      peptideBicyclicFixTerminalCys: DEFAULT_PEPTIDE_BICYCLIC_FIX_TERMINAL_CYS,
      peptideBicyclicIncludeExtraCys: DEFAULT_PEPTIDE_BICYCLIC_INCLUDE_EXTRA_CYS,
      peptideBicyclicCys1Pos: DEFAULT_PEPTIDE_BICYCLIC_CYS1_POS,
      peptideBicyclicCys2Pos: DEFAULT_PEPTIDE_BICYCLIC_CYS2_POS,
      peptideBicyclicCys3Pos: DEFAULT_PEPTIDE_BICYCLIC_CYS3_POS,
      peptidePocketCenter: '',
      peptidePocketResidues: '',
      peptidePocketBox: 6,
      peptideDockPocket: null,
      leadOptDockPocket: null,
      leadOptPocketCenter: '',
      leadOptMode: 'fragment',
      leadOptBackend: 'protenix2dock',
      leadOptRounds: 6,
      leadOptBudgetPerRound: 48,
      leadOptScaffoldHopRatio: 0.4,
      leadOptReferenceSmiles: '',
      leadOptKeepFragmentSmiles: '',
      leadOptEditAtomIndices: ''
    }
  };
}

function normalizeProperties(value: unknown): PredictionProperties {
  const raw = (value || {}) as Partial<PredictionProperties>;
  const target = typeof raw.target === 'string' && raw.target.trim() ? raw.target.trim() : null;
  const ligand = typeof raw.ligand === 'string' && raw.ligand.trim() ? raw.ligand.trim() : null;
  const binder = typeof raw.binder === 'string' && raw.binder.trim() ? raw.binder.trim() : null;
  return {
    affinity: Boolean(raw.affinity),
    target,
    ligand,
    binder: binder || ligand
  };
}

function normalizeVirtualScreeningPredictions(value: unknown): Record<string, VirtualScreeningPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const normalized: Record<string, VirtualScreeningPredictionRecord> = {};
  const allowedBackends = new Set(['boltz', 'protenix', 'alphafold3']);
  const allowedStates = new Set(['QUEUED', 'RUNNING', 'SUCCESS', 'FAILURE']);
  for (const [rawKey, rawValue] of Object.entries(value as Record<string, unknown>).slice(0, 600)) {
    if (!rawValue || typeof rawValue !== 'object' || Array.isArray(rawValue)) continue;
    const record = rawValue as Record<string, unknown>;
    const key = String(rawKey || '').trim().slice(0, 600);
    const taskId = String(record.taskId ?? record.task_id ?? '').trim().slice(0, 240);
    const backend = String(record.backend || '').trim().toLowerCase();
    const state = String(record.state || '').trim().toUpperCase();
    if (!key || !taskId || !allowedBackends.has(backend) || !allowedStates.has(state)) continue;
    const numberOrNull = (item: unknown): number | null => {
      if (item === null || item === undefined || item === '' || typeof item === 'boolean') return null;
      const number = Number(item);
      return Number.isFinite(number) ? number : null;
    };
    normalized[key] = {
      taskId,
      backend: backend as VirtualScreeningPredictionRecord['backend'],
      state: state as VirtualScreeningPredictionRecord['state'],
      ligandPlddt: numberOrNull(record.ligandPlddt ?? record.ligand_plddt),
      interfaceMetricValue: numberOrNull(record.interfaceMetricValue ?? record.interface_metric_value),
      interfaceMetricLabel: String(record.interfaceMetricLabel ?? record.interface_metric_label) === 'ipTM' ? 'ipTM' : 'IPSAE',
      pairIptm: numberOrNull(record.pairIptm ?? record.pair_iptm),
      pairPae: numberOrNull(record.pairPae ?? record.pair_pae),
      error: String(record.error || '').trim().slice(0, 800),
      updatedAt: Number.isFinite(Number(record.updatedAt ?? record.updated_at))
        ? Number(record.updatedAt ?? record.updated_at)
        : 0
    };
  }
  return normalized;
}

function normalizeOptions(value: unknown): PredictionOptions {
  const rawObj = value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
  const raw = rawObj as Partial<PredictionOptions>;
  const seed = raw.seed;
  const virtualScreeningInput = typeof (raw.virtualScreeningInput ?? rawObj.virtual_screening_input) === 'string'
    ? String(raw.virtualScreeningInput ?? rawObj.virtual_screening_input).slice(0, 1_000_000)
    : '';
  const virtualScreeningInputModeRaw = String(
    raw.virtualScreeningInputMode ?? rawObj.virtual_screening_input_mode ?? ''
  ).trim().toLowerCase();
  const virtualScreeningInputMode = virtualScreeningInputModeRaw === 'paste'
    ? 'paste'
    : virtualScreeningInputModeRaw === 'upload'
      ? 'upload'
      : virtualScreeningInput
        ? 'paste'
        : 'upload';
  const virtualScreeningInputFileName = String(
    raw.virtualScreeningInputFileName ?? rawObj.virtual_screening_input_file_name ?? ''
  ).trim().slice(0, 240);
  const virtualScreeningPredictions = normalizeVirtualScreeningPredictions(
    raw.virtualScreeningPredictions ?? rawObj.virtual_screening_predictions
  );
  const affinityMode = normalizeAffinityMode(raw.affinityMode ?? rawObj.affinity_mode ?? rawObj.mode);
  const affinityDockPocket = normalizeAffinityDockPocket(raw.affinityDockPocket ?? rawObj.affinity_dock_pocket);
  const peptideDockPocket = normalizeAffinityDockPocket(raw.peptideDockPocket ?? rawObj.peptide_dock_pocket);
  const leadOptDockPocket = normalizeAffinityDockPocket(raw.leadOptDockPocket ?? rawObj.lead_opt_dock_pocket);
  const leadOptMode = raw.leadOptMode === 'denovo' || raw.leadOptMode === 'scaffold_hop' ? raw.leadOptMode : 'fragment';
  const leadOptBackend =
    raw.leadOptBackend === 'boltz2dock' || raw.leadOptBackend === 'alphafold3' ? raw.leadOptBackend : 'protenix2dock';
  const leadOptRoundsRaw = Number(raw.leadOptRounds ?? rawObj.lead_opt_rounds);
  const leadOptRounds = Number.isFinite(leadOptRoundsRaw) ? Math.min(100, Math.max(1, Math.floor(leadOptRoundsRaw))) : 6;
  const leadOptBudgetRaw = Number(raw.leadOptBudgetPerRound ?? rawObj.lead_opt_budget_per_round);
  const leadOptBudgetPerRound = Number.isFinite(leadOptBudgetRaw) ? Math.min(512, Math.max(1, Math.floor(leadOptBudgetRaw))) : 48;
  const leadOptHopRaw = Number(raw.leadOptScaffoldHopRatio ?? rawObj.lead_opt_scaffold_hop_ratio);
  const leadOptScaffoldHopRatio = Number.isFinite(leadOptHopRaw) ? Math.min(1, Math.max(0, leadOptHopRaw)) : 0.4;
  const readLeadOptText = (camelKey: keyof PredictionOptions, snakeKey: string): string =>
    String(raw[camelKey] ?? rawObj[snakeKey] ?? '').trim();
  const peptideDesignMode = normalizePeptideDesignMode(raw.peptideDesignMode ?? rawObj.peptide_design_mode);
  const peptideChirality =
    String(raw.peptideChirality ?? rawObj.peptide_chirality ?? 'l').trim().toLowerCase() === 'd'
      ? 'd'
      : 'l';
  const minPeptideLength = peptideDesignMode === 'bicyclic' ? 8 : 5;
  // length window (min/max); min == max behaves as a fixed value.
  const peptideLengthMin = normalizeIntegerOption(
    raw.peptideLengthMin ?? rawObj.peptide_length_min,
    DEFAULT_PEPTIDE_BINDER_LENGTH,
    minPeptideLength,
    80
  );
  const peptideLengthMax = normalizeIntegerOption(
    raw.peptideLengthMax ?? rawObj.peptide_length_max,
    DEFAULT_PEPTIDE_BINDER_LENGTH,
    peptideLengthMin,
    80
  );
  // mask/pool layout normalization needs a concrete length: use the window max
  const effectiveBinderLength = peptideLengthMax;
  // single-value legacy field: only set when the user pinned min == max
  const peptideBinderLength = peptideLengthMin === peptideLengthMax
    ? peptideLengthMin : undefined;
  const peptideUseInitialSequence = normalizeBooleanOption(
    raw.peptideUseInitialSequence ?? rawObj.peptide_use_initial_sequence ?? rawObj.use_initial_sequence,
    DEFAULT_PEPTIDE_USE_INITIAL_SEQUENCE
  );
  const peptideInitialSequence = normalizePeptideInitialSequence(
    raw.peptideInitialSequence ?? rawObj.peptide_initial_sequence ?? rawObj.initial_sequence,
    effectiveBinderLength
  );
  const peptideSequenceMask = normalizePeptideSequenceMask(
    raw.peptideSequenceMask ?? rawObj.peptide_sequence_mask ?? rawObj.sequence_mask,
    effectiveBinderLength
  );
  const peptideIterations = normalizeIntegerOption(
    raw.peptideIterations ?? rawObj.peptide_iterations ?? rawObj.generations,
    DEFAULT_PEPTIDE_ITERATIONS,
    2,
    100
  );
  const peptidePopulationSize = normalizeIntegerOption(
    raw.peptidePopulationSize ?? rawObj.peptide_population_size ?? rawObj.population_size,
    DEFAULT_PEPTIDE_POPULATION_SIZE,
    2,
    100
  );
  const peptideEliteSize = Math.max(
    1,
    Math.min(
      Math.max(1, peptidePopulationSize - 1),
      normalizeIntegerOption(
        raw.peptideEliteSize ?? rawObj.peptide_elite_size ?? rawObj.elite_size ?? rawObj.num_elites,
        DEFAULT_PEPTIDE_ELITE_SIZE,
        1,
        99
      )
    )
  );
  const rawPeptideResiduePool = raw.peptideResiduePool ?? rawObj.peptide_residue_pool;
  const peptideResiduePool = Array.isArray(rawPeptideResiduePool)
    ? normalizePeptideResiduePool(rawPeptideResiduePool)
    : DEFAULT_PEPTIDE_RESIDUE_POOL;
  const hasSelectedNonNaturalResidues = countPeptideNonNaturalResidues(peptideResiduePool) > 0;
  const peptideNonNaturalMin = hasSelectedNonNaturalResidues
    ? normalizeIntegerOption(
        raw.peptideNonNaturalMin ?? rawObj.peptide_non_natural_min ?? rawObj.non_natural_min,
        0,
        0,
        effectiveBinderLength
      )
    : 0;
  const peptideNonNaturalMax = hasSelectedNonNaturalResidues
    ? normalizeIntegerOption(
        raw.peptideNonNaturalMax ?? rawObj.peptide_non_natural_max ?? rawObj.non_natural_max,
        Math.max(1, peptideNonNaturalMin),
        Math.max(1, peptideNonNaturalMin),
        effectiveBinderLength
      )
    : 0;
  const peptideBicyclicLinkerCcd = normalizePeptideBicyclicLinkerCcd(
    raw.peptideBicyclicLinkerCcd ?? rawObj.peptide_bicyclic_linker_ccd ?? rawObj.linker_ccd
  );
  const peptideBicyclicCysPositionMode = normalizePeptideBicyclicCysPositionMode(
    raw.peptideBicyclicCysPositionMode ?? rawObj.peptide_bicyclic_cys_position_mode ?? rawObj.cys_position_mode
  );
  const peptideBicyclicFixTerminalCys = normalizeBooleanOption(
    raw.peptideBicyclicFixTerminalCys ?? rawObj.peptide_bicyclic_fix_terminal_cys ?? rawObj.fix_terminal_cys,
    DEFAULT_PEPTIDE_BICYCLIC_FIX_TERMINAL_CYS
  );
  const peptideBicyclicIncludeExtraCys = normalizeBooleanOption(
    raw.peptideBicyclicIncludeExtraCys ??
      rawObj.peptide_bicyclic_include_extra_cys ??
      rawObj.include_extra_cys ??
      rawObj.include_cysteine,
    DEFAULT_PEPTIDE_BICYCLIC_INCLUDE_EXTRA_CYS
  );
  const peptideBicyclicCys1Pos = normalizeIntegerOption(
    raw.peptideBicyclicCys1Pos ?? rawObj.peptide_bicyclic_cys1_pos ?? rawObj.cys1_pos,
    DEFAULT_PEPTIDE_BICYCLIC_CYS1_POS,
    1,
    Math.max(1, effectiveBinderLength - 2)
  );
  const peptideBicyclicCys2Pos = normalizeIntegerOption(
    raw.peptideBicyclicCys2Pos ?? rawObj.peptide_bicyclic_cys2_pos ?? rawObj.cys2_pos,
    DEFAULT_PEPTIDE_BICYCLIC_CYS2_POS,
    1,
    peptideBicyclicFixTerminalCys ? Math.max(1, effectiveBinderLength - 2) : Math.max(1, effectiveBinderLength - 1)
  );
  const peptidePocketCenter = typeof raw.peptidePocketCenter === 'string'
    ? raw.peptidePocketCenter.trim()
    : (typeof rawObj.peptide_pocket_center === 'string' ? String(rawObj.peptide_pocket_center).trim() : '');
  const peptidePocketResidues = typeof raw.peptidePocketResidues === 'string'
    ? raw.peptidePocketResidues.trim()
    : (typeof rawObj.peptide_pocket_residues === 'string' ? String(rawObj.peptide_pocket_residues).trim() : '');
  const peptidePocketBox = normalizeIntegerOption(
    raw.peptidePocketBox ?? rawObj.peptide_pocket_box,
    4, 40, 6
  );
  const peptideBicyclicCys3Pos = peptideBicyclicFixTerminalCys
    ? effectiveBinderLength
    : normalizeIntegerOption(
        raw.peptideBicyclicCys3Pos ?? rawObj.peptide_bicyclic_cys3_pos ?? rawObj.cys3_pos,
        DEFAULT_PEPTIDE_BICYCLIC_CYS3_POS,
        1,
        effectiveBinderLength
      );
  if (seed === null) {
    return {
      seed: null,
      virtualScreeningInput,
      virtualScreeningInputMode,
      virtualScreeningInputFileName,
      virtualScreeningPredictions,
      affinityMode,
      affinityDockPocket,
      peptideDesignMode,
      peptideChirality,
      peptideStructureUpload: isPeptideStructureUpload(raw.peptideStructureUpload)
        ? raw.peptideStructureUpload
        : null,
      peptideBinderLength,
      peptideUseInitialSequence,
      peptideInitialSequence,
      peptideSequenceMask,
      peptideIterations,
      peptidePopulationSize,
      peptideEliteSize,
      peptideResiduePool,
      peptideNonNaturalMin,
      peptideNonNaturalMax,
      peptideBicyclicLinkerCcd,
      peptideBicyclicCysPositionMode,
      peptideBicyclicFixTerminalCys,
      peptideBicyclicIncludeExtraCys,
      peptideBicyclicCys1Pos,
      peptideBicyclicCys2Pos,
      peptideBicyclicCys3Pos,
      peptidePocketCenter,
      peptidePocketResidues,
      peptidePocketBox,
      peptideDockPocket,
      leadOptDockPocket,
      leadOptPocketCenter: readLeadOptText('leadOptPocketCenter', 'lead_opt_pocket_center'),
      leadOptMode,
      leadOptBackend,
      leadOptRounds,
      leadOptBudgetPerRound,
      leadOptScaffoldHopRatio,
      leadOptReferenceSmiles: readLeadOptText('leadOptReferenceSmiles', 'lead_opt_reference_smiles'),
      leadOptKeepFragmentSmiles: readLeadOptText('leadOptKeepFragmentSmiles', 'lead_opt_keep_fragment_smiles'),
      leadOptEditAtomIndices: readLeadOptText('leadOptEditAtomIndices', 'lead_opt_edit_atom_indices')
    };
  }
  return {
    seed: typeof seed === 'number' && Number.isFinite(seed) ? Math.max(0, Math.floor(seed)) : null,
    virtualScreeningInput,
    virtualScreeningInputMode,
    virtualScreeningInputFileName,
    virtualScreeningPredictions,
    affinityMode,
    affinityDockPocket,
    peptideDesignMode,
    peptideChirality,
    peptideStructureUpload: isPeptideStructureUpload(raw.peptideStructureUpload)
      ? raw.peptideStructureUpload
      : null,
    peptideBinderLength,
    peptideUseInitialSequence,
    peptideInitialSequence,
    peptideSequenceMask,
    peptideIterations,
    peptidePopulationSize,
    peptideEliteSize,
    peptideResiduePool,
    peptideNonNaturalMin,
    peptideNonNaturalMax,
    peptideBicyclicLinkerCcd,
    peptideBicyclicCysPositionMode,
    peptideBicyclicFixTerminalCys,
    peptideBicyclicIncludeExtraCys,
    peptideBicyclicCys1Pos,
    peptideBicyclicCys2Pos,
    peptideBicyclicCys3Pos,
    peptidePocketCenter,
    peptidePocketResidues,
    peptidePocketBox,
    peptideDockPocket,
    leadOptDockPocket,
    leadOptPocketCenter: readLeadOptText('leadOptPocketCenter', 'lead_opt_pocket_center'),
    leadOptMode,
    leadOptBackend,
    leadOptRounds,
    leadOptBudgetPerRound,
    leadOptScaffoldHopRatio,
    leadOptReferenceSmiles: readLeadOptText('leadOptReferenceSmiles', 'lead_opt_reference_smiles'),
    leadOptKeepFragmentSmiles: readLeadOptText('leadOptKeepFragmentSmiles', 'lead_opt_keep_fragment_smiles'),
    leadOptEditAtomIndices: readLeadOptText('leadOptEditAtomIndices', 'lead_opt_edit_atom_indices')
  };
}

function normalizeConstraints(value: unknown): PredictionConstraint[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (!item || typeof item !== 'object') return null;
      const raw = item as Record<string, unknown>;
      const id = typeof raw.id === 'string' && raw.id ? raw.id : randomId();
      const type = raw.type;

      if (type === 'contact') {
        const token1Chain = typeof raw.token1_chain === 'string' ? raw.token1_chain.trim() : '';
        const token2Chain = typeof raw.token2_chain === 'string' ? raw.token2_chain.trim() : '';
        if (!token1Chain || !token2Chain) return null;
        return {
          id,
          type: 'contact' as const,
          token1_chain: token1Chain,
          token1_residue: Math.max(1, Number(raw.token1_residue || 1)),
          token2_chain: token2Chain,
          token2_residue: Math.max(1, Number(raw.token2_residue || 1)),
          max_distance: Math.max(1, Number(raw.max_distance || 5)),
          force: raw.force === undefined ? true : Boolean(raw.force)
        };
      }

      if (type === 'bond') {
        const atom1Chain = typeof raw.atom1_chain === 'string' ? raw.atom1_chain.trim() : '';
        const atom2Chain = typeof raw.atom2_chain === 'string' ? raw.atom2_chain.trim() : '';
        if (!atom1Chain || !atom2Chain) return null;
        return {
          id,
          type: 'bond' as const,
          atom1_chain: atom1Chain,
          atom1_residue: Math.max(1, Number(raw.atom1_residue || 1)),
          atom1_atom: typeof raw.atom1_atom === 'string' && raw.atom1_atom ? raw.atom1_atom : 'CA',
          atom2_chain: atom2Chain,
          atom2_residue: Math.max(1, Number(raw.atom2_residue || 1)),
          atom2_atom: typeof raw.atom2_atom === 'string' && raw.atom2_atom ? raw.atom2_atom : 'CA'
        };
      }

      if (type === 'pocket') {
        const binder = typeof raw.binder === 'string' ? raw.binder.trim() : '';
        if (!binder) return null;
        const contacts = Array.isArray(raw.contacts)
          ? raw.contacts
              .map((c) =>
                Array.isArray(c) && typeof c[0] === 'string'
                  ? ([c[0], Math.max(1, Number(c[1] || 1))] as [string, number])
                  : null
              )
              .filter(Boolean) as Array<[string, number]>
          : [];
        return {
          id,
          type: 'pocket' as const,
          binder,
          contacts,
          max_distance: Math.max(1, Number(raw.max_distance || 6)),
          force: raw.force === undefined ? true : Boolean(raw.force)
        };
      }

      return null;
    })
    .filter(Boolean) as PredictionConstraint[];
}


function normalizeCustomResidueLibrary(value: unknown): CustomCcdMoleculeInput[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const normalized: CustomCcdMoleculeInput[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const raw = item as Record<string, unknown>;
    const ccd = normalizeProteinModificationCcd(raw.ccd ?? raw.ccdCode);
    const smiles = typeof raw.smiles === 'string' ? raw.smiles.trim() : '';
    if (!ccd || !smiles || seen.has(ccd)) continue;
    seen.add(ccd);
    normalized.push({
      ccd,
      smiles,
      baseResidue: typeof raw.baseResidue === 'string' ? raw.baseResidue.trim().toUpperCase().slice(0, 1) : undefined,
      label: typeof raw.label === 'string' && raw.label.trim() ? raw.label.trim().slice(0, 80) : undefined,
      cTerminalAmidated: Boolean(raw.cTerminalAmidated) || undefined
    });
  }
  return normalized;
}

function normalizeConfig(value: ProjectInputConfig): ProjectInputConfig {
  const base = buildDefaultInputConfig();
  const components =
    Array.isArray(value.components) && value.components.length > 0 ? normalizeInputComponents(value.components) : base.components;
  return {
    version: 1,
    components,
    constraints: normalizeConstraints((value as unknown as Record<string, unknown>).constraints),
    properties: normalizeProperties((value as unknown as Record<string, unknown>).properties),
    options: normalizeOptions((value as unknown as Record<string, unknown>).options)
  };
}

export function normalizeProjectInputConfig(value: ProjectInputConfig): ProjectInputConfig {
  return normalizeConfig(value);
}

function readStore(): Record<string, ProjectInputConfig> {
  try {
    const raw = localStorage.getItem(COMPONENT_KEY);
    if (!raw) return {};
    const data = JSON.parse(raw) as Record<string, ProjectInputConfig>;
    if (data && typeof data === 'object') {
      return data;
    }
  } catch {
    // ignore malformed storage
  }
  return {};
}

function writeStore(data: Record<string, ProjectInputConfig>): void {
  localStorage.setItem(COMPONENT_KEY, JSON.stringify(data));
}

function readSessionIdentityForStorage(): string {
  if (typeof localStorage === 'undefined') return '';
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return '';
    const payload = JSON.parse(raw) as Record<string, unknown>;
    const userId = typeof payload.userId === 'string' ? payload.userId.trim() : '';
    const username = typeof payload.username === 'string' ? payload.username.trim().toLowerCase() : '';
    return userId || username;
  } catch {
    return '';
  }
}

function buildScopedProjectStorageKey(projectId: string): string {
  const normalizedProjectId = String(projectId || '').trim();
  if (!normalizedProjectId) return '';
  const sessionIdentity = readSessionIdentityForStorage() || '__anonymous__';
  return `${sessionIdentity}:${normalizedProjectId}`;
}

function buildScopedStorageItemKey(prefix: string, projectId: string): string {
  const scopedProjectKey = buildScopedProjectStorageKey(projectId);
  if (!scopedProjectKey) return '';
  return `${prefix}${scopedProjectKey}`;
}

function buildStorageItemKeyFromScopedKey(prefix: string, scopedProjectKey: string): string {
  const normalizedScopedProjectKey = String(scopedProjectKey || '').trim();
  if (!normalizedScopedProjectKey) return '';
  return `${prefix}${normalizedScopedProjectKey}`;
}

function readScopedStorageItem<T>(storageKey: string): T | null {
  if (typeof localStorage === 'undefined' || !storageKey) return null;
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return null;
    const data = JSON.parse(raw) as T;
    if (data && typeof data === 'object') {
      return data;
    }
  } catch {
    // ignore malformed storage
  }
  return null;
}

function isQuotaExceededError(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const name = typeof (error as { name?: unknown }).name === 'string' ? (error as { name: string }).name : '';
  const code = typeof (error as { code?: unknown }).code === 'number' ? (error as { code: number }).code : null;
  return QUOTA_ERROR_NAMES.has(name) || code === 22 || code === 1014;
}

function writeScopedStorageItem(
  storageKey: string,
  value: unknown,
  options?: {
    legacyStoreKey?: string;
    fallbackValue?: unknown;
  }
): boolean {
  if (typeof localStorage === 'undefined' || !storageKey) return false;
  const payload = JSON.stringify(value);
  const hasFallbackValue = Boolean(options && 'fallbackValue' in options);
  const fallbackPayload = hasFallbackValue ? JSON.stringify(options?.fallbackValue) : '';

  try {
    localStorage.setItem(storageKey, payload);
    return true;
  } catch (error) {
    if (!isQuotaExceededError(error)) {
      console.warn(`Failed to persist storage item "${storageKey}".`, error);
      return false;
    }
  }

  if (options?.legacyStoreKey) {
    try {
      localStorage.removeItem(options.legacyStoreKey);
      localStorage.setItem(storageKey, payload);
      return true;
    } catch (error) {
      if (!isQuotaExceededError(error)) {
        console.warn(`Failed to persist storage item "${storageKey}" after clearing legacy store.`, error);
        return false;
      }
    }
  }

  if (hasFallbackValue) {
    try {
      localStorage.setItem(storageKey, fallbackPayload);
      return true;
    } catch (error) {
      if (!isQuotaExceededError(error)) {
        console.warn(`Failed to persist fallback storage item "${storageKey}".`, error);
        return false;
      }
    }
  }

  console.warn(`Skipped persisting storage item "${storageKey}" because browser storage quota was exceeded.`);
  return false;
}

function readUiStore(): Record<string, ProjectUiState> {
  try {
    const raw = localStorage.getItem(UI_STATE_KEY);
    if (!raw) return {};
    const data = JSON.parse(raw) as Record<string, ProjectUiState>;
    if (data && typeof data === 'object') {
      return data;
    }
  } catch {
    // ignore malformed storage
  }
  return {};
}

function writeUiStore(data: Record<string, ProjectUiState>): void {
  localStorage.setItem(UI_STATE_KEY, JSON.stringify(data));
}

function resolveTemplateContent(value: unknown, pool: Record<string, string>): string {
  if (typeof value !== 'string') return '';
  if (!value.startsWith(TEMPLATE_CONTENT_REF_PREFIX)) {
    return value;
  }
  const key = value.slice(TEMPLATE_CONTENT_REF_PREFIX.length).trim();
  return key ? pool[key] || '' : '';
}

function normalizeTemplateContentPool(value: unknown): Record<string, string> {
  const pool: Record<string, string> = {};
  if (!value || typeof value !== 'object') return pool;
  for (const [key, text] of Object.entries(value as Record<string, unknown>)) {
    if (!key || typeof text !== 'string') continue;
    if (!text.trim()) continue;
    pool[key] = text;
  }
  return pool;
}

function normalizeStoredProteinTemplates(
  value: unknown,
  contentPool: Record<string, string>
): Record<string, ProteinTemplateUpload> {
  const proteinTemplates: Record<string, ProteinTemplateUpload> = {};
  if (!value || typeof value !== 'object') return proteinTemplates;

  for (const [componentId, upload] of Object.entries(value as Record<string, unknown>)) {
    if (!upload || typeof upload !== 'object') continue;
    const fileName = typeof (upload as any).fileName === 'string' ? (upload as any).fileName : '';
    const format = (upload as any).format === 'cif' ? 'cif' : (upload as any).format === 'pdb' ? 'pdb' : null;
    const content = resolveTemplateContent((upload as any).content, contentPool);
    const chainId = typeof (upload as any).chainId === 'string' ? (upload as any).chainId : '';
    if (!fileName || !format || !content) continue;
    const chainSequencesValue = (upload as any).chainSequences;
    const chainSequences =
      chainSequencesValue && typeof chainSequencesValue === 'object' ? (chainSequencesValue as Record<string, string>) : {};
    proteinTemplates[componentId] = {
      fileName,
      format,
      content,
      chainId,
      chainSequences
    };
  }

  return proteinTemplates;
}

function hashTemplateContent(content: string): string {
  let hash = 2166136261;
  for (let i = 0; i < content.length; i += 1) {
    hash ^= content.charCodeAt(i);
    hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
  }
  return (hash >>> 0).toString(36);
}

function serializeProteinTemplates(
  templates: Record<string, ProteinTemplateUpload> | undefined,
  contentPool: Record<string, string>,
  usedPoolKeys: Set<string>
): Record<string, ProteinTemplateUpload> {
  const serialized: Record<string, ProteinTemplateUpload> = {};
  if (!templates || typeof templates !== 'object') return serialized;

  for (const [componentId, upload] of Object.entries(templates)) {
    if (!upload || typeof upload !== 'object') continue;
    const fileName = typeof upload.fileName === 'string' ? upload.fileName.trim() : '';
    const format = upload.format === 'cif' ? 'cif' : upload.format === 'pdb' ? 'pdb' : null;
    const content = typeof upload.content === 'string' ? upload.content : '';
    const chainId = typeof upload.chainId === 'string' ? upload.chainId.trim() : '';
    if (!fileName || !format || !content.trim()) continue;
    const key = `tpl-${hashTemplateContent(content)}-${content.length.toString(36)}`;
    contentPool[key] = content;
    usedPoolKeys.add(key);
    serialized[componentId] = {
      fileName,
      format,
      content: `${TEMPLATE_CONTENT_REF_PREFIX}${key}`,
      chainId,
      chainSequences: upload.chainSequences && typeof upload.chainSequences === 'object' ? upload.chainSequences : {}
    };
  }

  return serialized;
}

function compactTemplateContentPool(pool: Record<string, string>, usedPoolKeys: Set<string>): Record<string, string> {
  const compacted: Record<string, string> = {};
  for (const key of usedPoolKeys) {
    const content = pool[key];
    if (typeof content !== 'string' || !content.trim()) continue;
    compacted[key] = content;
  }
  return compacted;
}

function serializeAffinityUpload(
  upload: { fileName: string; content: string } | null | undefined,
  contentPool: Record<string, string>,
  usedPoolKeys: Set<string>
): { fileName: string; content: string } | null {
  if (!upload || typeof upload !== 'object') return null;
  const fileName = typeof upload.fileName === 'string' ? upload.fileName.trim() : '';
  const content = typeof upload.content === 'string' ? upload.content : '';
  if (!fileName || !content.trim()) return null;
  const key = `aff-${hashTemplateContent(content)}-${content.length.toString(36)}`;
  contentPool[key] = content;
  usedPoolKeys.add(key);
  return {
    fileName,
    content: `${TEMPLATE_CONTENT_REF_PREFIX}${key}`
  };
}

function normalizeAffinityUpload(
  upload: unknown,
  contentPool: Record<string, string>
): { fileName: string; content: string } | null {
  if (!upload || typeof upload !== 'object') return null;
  const fileName = typeof (upload as any).fileName === 'string' ? (upload as any).fileName.trim() : '';
  const content = resolveTemplateContent((upload as any).content, contentPool);
  if (!fileName || !content.trim()) return null;
  return { fileName, content };
}

export function loadProjectInputConfig(projectId: string): ProjectInputConfig | null {
  const scopedProjectKey = buildScopedProjectStorageKey(projectId);
  if (!scopedProjectKey) return null;
  const directStorageKey = buildStorageItemKeyFromScopedKey(COMPONENT_ITEM_KEY_PREFIX, scopedProjectKey);
  const directConfig = readScopedStorageItem<ProjectInputConfig>(directStorageKey);
  const found = directConfig || readStore()[scopedProjectKey];
  if (!found || !Array.isArray(found.components)) return null;
  return normalizeConfig(found);
}

export function saveProjectInputConfig(projectId: string, config: ProjectInputConfig): void {
  const storageKey = buildScopedStorageItemKey(COMPONENT_ITEM_KEY_PREFIX, projectId);
  if (!storageKey) return;
  writeScopedStorageItem(storageKey, config, { legacyStoreKey: COMPONENT_KEY });
}

export function removeProjectInputConfig(projectId: string): void {
  const storageKey = buildScopedStorageItemKey(COMPONENT_ITEM_KEY_PREFIX, projectId);
  if (storageKey && typeof localStorage !== 'undefined') {
    try {
      localStorage.removeItem(storageKey);
    } catch {
      // ignore storage cleanup errors
    }
  }
  const scopedProjectKey = buildScopedProjectStorageKey(projectId);
  if (!scopedProjectKey) return;
  const store = readStore();
  delete store[scopedProjectKey];
  writeStore(store);
}

export function loadProjectUiState(projectId: string): ProjectUiState | null {
  const scopedProjectKey = buildScopedProjectStorageKey(projectId);
  if (!scopedProjectKey) return null;
  const directStorageKey = buildStorageItemKeyFromScopedKey(UI_STATE_ITEM_KEY_PREFIX, scopedProjectKey);
  const directState = readScopedStorageItem<ProjectUiState>(directStorageKey);
  const found = directState || readUiStore()[scopedProjectKey];
  if (!found || typeof found !== 'object') return null;
  const templateContentPool = normalizeTemplateContentPool((found as any).templateContentPool);

  const activeConstraintId =
    typeof found.activeConstraintId === 'string' && found.activeConstraintId.trim() ? found.activeConstraintId : null;
  const selectedConstraintTemplateComponentId =
    typeof (found as any).selectedConstraintTemplateComponentId === 'string' &&
    (found as any).selectedConstraintTemplateComponentId.trim()
      ? (found as any).selectedConstraintTemplateComponentId
      : null;

  const proteinTemplates = normalizeStoredProteinTemplates((found as any).proteinTemplates, templateContentPool);
  const legacyAffinityUploads = (() => {
    const raw = (found as any).affinityUploads;
    if (!raw || typeof raw !== 'object') {
      return { target: null, ligand: null };
    }
    return {
      target: normalizeAffinityUpload((raw as any).target, templateContentPool),
      ligand: normalizeAffinityUpload((raw as any).ligand, templateContentPool)
    };
  })();
  const taskAffinityUploads: Record<
    string,
    {
      target: { fileName: string; content: string } | null;
      ligand: { fileName: string; content: string } | null;
    }
  > = {};
  const rawTaskAffinityUploads = (found as any).taskAffinityUploads;
  if (rawTaskAffinityUploads && typeof rawTaskAffinityUploads === 'object') {
    for (const [taskRowId, uploads] of Object.entries(rawTaskAffinityUploads as Record<string, unknown>)) {
      const normalizedTaskRowId = String(taskRowId || '').trim();
      if (!normalizedTaskRowId || !uploads || typeof uploads !== 'object') continue;
      const normalized = {
        target: normalizeAffinityUpload((uploads as any).target, templateContentPool),
        ligand: normalizeAffinityUpload((uploads as any).ligand, templateContentPool)
      };
      if (!normalized.target && !normalized.ligand) continue;
      taskAffinityUploads[normalizedTaskRowId] = normalized;
    }
  }

  if (Object.keys(taskAffinityUploads).length === 0 && (legacyAffinityUploads.target || legacyAffinityUploads.ligand)) {
    taskAffinityUploads.__legacy__ = legacyAffinityUploads;
  }
  const taskProteinTemplates: Record<string, Record<string, ProteinTemplateUpload>> = {};
  const rawTaskTemplates = (found as any).taskProteinTemplates;
  if (rawTaskTemplates && typeof rawTaskTemplates === 'object') {
    for (const [taskRowId, taskTemplates] of Object.entries(rawTaskTemplates as Record<string, unknown>)) {
      const normalized = normalizeStoredProteinTemplates(taskTemplates, templateContentPool);
      if (Object.keys(normalized).length === 0) continue;
      taskProteinTemplates[taskRowId] = normalized;
    }
  }

  return {
    proteinTemplates,
    customResidueLibrary: normalizeCustomResidueLibrary((found as any).customResidueLibrary),
    taskProteinTemplates,
    templateContentPool,
    taskAffinityUploads,
    affinityUploads: legacyAffinityUploads,
    activeConstraintId,
    selectedConstraintTemplateComponentId
  };
}

export function saveProjectUiState(projectId: string, uiState: ProjectUiState): void {
  const scopedProjectKey = buildScopedProjectStorageKey(projectId);
  if (!scopedProjectKey) return;
  const storageKey = buildStorageItemKeyFromScopedKey(UI_STATE_ITEM_KEY_PREFIX, scopedProjectKey);
  const currentStoredState =
    readScopedStorageItem<Record<string, unknown>>(storageKey) || readUiStore()[scopedProjectKey] || null;
  const contentPool = normalizeTemplateContentPool(uiState.templateContentPool || (currentStoredState as any)?.templateContentPool);
  const usedPoolKeys = new Set<string>();
  const proteinTemplates = serializeProteinTemplates(uiState.proteinTemplates, contentPool, usedPoolKeys);
  const affinityUploads = {
    target: serializeAffinityUpload(uiState.affinityUploads?.target || null, contentPool, usedPoolKeys),
    ligand: serializeAffinityUpload(uiState.affinityUploads?.ligand || null, contentPool, usedPoolKeys)
  };
  const taskAffinityUploads: Record<
    string,
    {
      target: { fileName: string; content: string } | null;
      ligand: { fileName: string; content: string } | null;
    }
  > = {};
  if (uiState.taskAffinityUploads && typeof uiState.taskAffinityUploads === 'object') {
    for (const [taskRowId, uploads] of Object.entries(uiState.taskAffinityUploads)) {
      const normalizedTaskRowId = String(taskRowId || '').trim();
      if (!normalizedTaskRowId || !uploads || typeof uploads !== 'object') continue;
      const serialized = {
        target: serializeAffinityUpload((uploads as any).target || null, contentPool, usedPoolKeys),
        ligand: serializeAffinityUpload((uploads as any).ligand || null, contentPool, usedPoolKeys)
      };
      if (!serialized.target && !serialized.ligand) continue;
      taskAffinityUploads[normalizedTaskRowId] = serialized;
    }
  }
  const taskProteinTemplates: Record<string, Record<string, ProteinTemplateUpload>> = {};

  if (uiState.taskProteinTemplates && typeof uiState.taskProteinTemplates === 'object') {
    for (const [taskRowId, templates] of Object.entries(uiState.taskProteinTemplates)) {
      if (!taskRowId) continue;
      const serialized = serializeProteinTemplates(templates, contentPool, usedPoolKeys);
      if (Object.keys(serialized).length === 0) continue;
      taskProteinTemplates[taskRowId] = serialized;
    }
  }

  const customResidueLibrary = normalizeCustomResidueLibrary(uiState.customResidueLibrary);

  const serializedState: ProjectUiState = {
    proteinTemplates,
    customResidueLibrary,
    taskProteinTemplates,
    taskAffinityUploads,
    templateContentPool: compactTemplateContentPool(contentPool, usedPoolKeys),
    affinityUploads,
    activeConstraintId: uiState.activeConstraintId || null,
    selectedConstraintTemplateComponentId: uiState.selectedConstraintTemplateComponentId || null
  };
  const fallbackState: ProjectUiState = {
    proteinTemplates: {},
    customResidueLibrary,
    taskProteinTemplates: {},
    taskAffinityUploads: {},
    affinityUploads: {
      target: null,
      ligand: null
    },
    activeConstraintId: uiState.activeConstraintId || null,
    selectedConstraintTemplateComponentId: uiState.selectedConstraintTemplateComponentId || null
  };

  writeScopedStorageItem(storageKey, serializedState, {
    legacyStoreKey: UI_STATE_KEY,
    fallbackValue: fallbackState
  });
}

export function removeProjectUiState(projectId: string): void {
  const storageKey = buildScopedStorageItemKey(UI_STATE_ITEM_KEY_PREFIX, projectId);
  if (storageKey && typeof localStorage !== 'undefined') {
    try {
      localStorage.removeItem(storageKey);
    } catch {
      // ignore storage cleanup errors
    }
  }
  const scopedProjectKey = buildScopedProjectStorageKey(projectId);
  if (!scopedProjectKey) return;
  const store = readUiStore();
  delete store[scopedProjectKey];
  writeUiStore(store);
}

export function normalizeComponentSequence(type: MoleculeType, value: string): string {
  const clean = value.trim();
  if (type === 'protein' || type === 'dna' || type === 'rna') {
    return clean.replace(/\s+/g, '');
  }
  return clean;
}

export function extractPrimaryProteinAndLigand(config: ProjectInputConfig): {
  proteinSequence: string;
  ligandSmiles: string;
} {
  const primaryProtein =
    config.components.find((c) => c.type === 'protein' && c.sequence.trim())?.sequence ?? '';
  const primaryLigand =
    config.components.find((c) => c.type === 'ligand' && c.sequence.trim())?.sequence ?? '';

  return {
    proteinSequence: primaryProtein,
    ligandSmiles: primaryLigand
  };
}

export function componentTypeLabel(type: MoleculeType): string {
  if (type === 'protein') return 'Protein';
  if (type === 'dna') return 'DNA';
  if (type === 'rna') return 'RNA';
  return 'Ligand';
}
