import type {
  InputComponent,
  MoleculeType,
  PredictionConstraint,
  PredictionOptions,
  PredictionProperties,
  ProjectInputConfig,
  ProteinTemplateUpload
} from '../types/models';

const COMPONENT_KEY = 'vbio_project_input_config_v1';
const UI_STATE_KEY = 'vbio_project_ui_state_v1';
const TEMPLATE_CONTENT_REF_PREFIX = '@pool:';
const VALID_MOLECULE_TYPES: MoleculeType[] = ['protein', 'ligand', 'dna', 'rna'];
const VALID_LIGAND_INPUT_METHODS = new Set(['smiles', 'ccd', 'jsme']);
const INVALID_COMPONENT_ID_TOKENS = new Set(['undefined', 'null', 'nan']);
const DEFAULT_PEPTIDE_DESIGN_MODE = 'cyclic';
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
const DEFAULT_PEPTIDE_MUTATION_RATE = 0.25;
const DEFAULT_PEPTIDE_BICYCLIC_LINKER_CCD = 'SEZ';
const VALID_PEPTIDE_BICYCLIC_LINKER_CCD = new Set(['SEZ', '29N', 'BS3']);
const DEFAULT_PEPTIDE_BICYCLIC_CYS_POSITION_MODE = 'auto';
const VALID_PEPTIDE_BICYCLIC_CYS_POSITION_MODES = new Set(['auto', 'manual']);
const DEFAULT_PEPTIDE_BICYCLIC_FIX_TERMINAL_CYS = true;
const DEFAULT_PEPTIDE_BICYCLIC_INCLUDE_EXTRA_CYS = false;
const DEFAULT_PEPTIDE_BICYCLIC_CYS1_POS = 3;
const DEFAULT_PEPTIDE_BICYCLIC_CYS2_POS = 8;
const DEFAULT_PEPTIDE_BICYCLIC_CYS3_POS = 15;

export const PEPTIDE_DESIGNED_LIGAND_TOKEN = '__designed_peptide__';

export interface ProjectUiState {
  proteinTemplates: Record<string, ProteinTemplateUpload>;
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
        cyclic: Boolean(component?.cyclic)
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

function normalizeRateOption(value: unknown, fallback: number, minValue: number, maxValue: number): number {
  const parsed = readFiniteNumber(value);
  if (parsed === null) return fallback;
  const clamped = Math.max(minValue, Math.min(maxValue, parsed));
  return Math.round(clamped * 100) / 100;
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

export function buildDefaultInputConfig(workflowKey: string | null | undefined = 'prediction'): ProjectInputConfig {
  const isPeptideDesignWorkflow = String(workflowKey || '').trim().toLowerCase() === 'peptide_design';
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
      peptideDesignMode: DEFAULT_PEPTIDE_DESIGN_MODE,
      peptideBinderLength: DEFAULT_PEPTIDE_BINDER_LENGTH,
      peptideUseInitialSequence: DEFAULT_PEPTIDE_USE_INITIAL_SEQUENCE,
      peptideInitialSequence: DEFAULT_PEPTIDE_INITIAL_SEQUENCE,
      peptideSequenceMask: 'X'.repeat(DEFAULT_PEPTIDE_BINDER_LENGTH),
      peptideIterations: DEFAULT_PEPTIDE_ITERATIONS,
      peptidePopulationSize: DEFAULT_PEPTIDE_POPULATION_SIZE,
      peptideEliteSize: DEFAULT_PEPTIDE_ELITE_SIZE,
      peptideMutationRate: DEFAULT_PEPTIDE_MUTATION_RATE,
      peptideBicyclicLinkerCcd: DEFAULT_PEPTIDE_BICYCLIC_LINKER_CCD as 'SEZ' | '29N' | 'BS3',
      peptideBicyclicCysPositionMode: DEFAULT_PEPTIDE_BICYCLIC_CYS_POSITION_MODE as 'auto' | 'manual',
      peptideBicyclicFixTerminalCys: DEFAULT_PEPTIDE_BICYCLIC_FIX_TERMINAL_CYS,
      peptideBicyclicIncludeExtraCys: DEFAULT_PEPTIDE_BICYCLIC_INCLUDE_EXTRA_CYS,
      peptideBicyclicCys1Pos: DEFAULT_PEPTIDE_BICYCLIC_CYS1_POS,
      peptideBicyclicCys2Pos: DEFAULT_PEPTIDE_BICYCLIC_CYS2_POS,
      peptideBicyclicCys3Pos: DEFAULT_PEPTIDE_BICYCLIC_CYS3_POS
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

function normalizeOptions(value: unknown): PredictionOptions {
  const rawObj = value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
  const raw = rawObj as Partial<PredictionOptions>;
  const seed = raw.seed;
  const peptideDesignMode = normalizePeptideDesignMode(raw.peptideDesignMode ?? rawObj.peptide_design_mode);
  const minPeptideLength = peptideDesignMode === 'bicyclic' ? 8 : 5;
  const peptideBinderLength = normalizeIntegerOption(
    raw.peptideBinderLength ?? rawObj.peptide_binder_length ?? rawObj.binder_length,
    DEFAULT_PEPTIDE_BINDER_LENGTH,
    minPeptideLength,
    80
  );
  const peptideUseInitialSequence = normalizeBooleanOption(
    raw.peptideUseInitialSequence ?? rawObj.peptide_use_initial_sequence ?? rawObj.use_initial_sequence,
    DEFAULT_PEPTIDE_USE_INITIAL_SEQUENCE
  );
  const peptideInitialSequence = normalizePeptideInitialSequence(
    raw.peptideInitialSequence ?? rawObj.peptide_initial_sequence ?? rawObj.initial_sequence,
    peptideBinderLength
  );
  const peptideSequenceMask = normalizePeptideSequenceMask(
    raw.peptideSequenceMask ?? rawObj.peptide_sequence_mask ?? rawObj.sequence_mask,
    peptideBinderLength
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
  const peptideMutationRate = normalizeRateOption(
    raw.peptideMutationRate ?? rawObj.peptide_mutation_rate ?? rawObj.mutation_rate,
    DEFAULT_PEPTIDE_MUTATION_RATE,
    0.01,
    1
  );
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
    Math.max(1, peptideBinderLength - 2)
  );
  const peptideBicyclicCys2Pos = normalizeIntegerOption(
    raw.peptideBicyclicCys2Pos ?? rawObj.peptide_bicyclic_cys2_pos ?? rawObj.cys2_pos,
    DEFAULT_PEPTIDE_BICYCLIC_CYS2_POS,
    1,
    peptideBicyclicFixTerminalCys ? Math.max(1, peptideBinderLength - 2) : Math.max(1, peptideBinderLength - 1)
  );
  const peptideBicyclicCys3Pos = peptideBicyclicFixTerminalCys
    ? peptideBinderLength
    : normalizeIntegerOption(
        raw.peptideBicyclicCys3Pos ?? rawObj.peptide_bicyclic_cys3_pos ?? rawObj.cys3_pos,
        DEFAULT_PEPTIDE_BICYCLIC_CYS3_POS,
        1,
        peptideBinderLength
      );
  if (seed === null) {
    return {
      seed: null,
      peptideDesignMode,
      peptideBinderLength,
      peptideUseInitialSequence,
      peptideInitialSequence,
      peptideSequenceMask,
      peptideIterations,
      peptidePopulationSize,
      peptideEliteSize,
      peptideMutationRate,
      peptideBicyclicLinkerCcd,
      peptideBicyclicCysPositionMode,
      peptideBicyclicFixTerminalCys,
      peptideBicyclicIncludeExtraCys,
      peptideBicyclicCys1Pos,
      peptideBicyclicCys2Pos,
      peptideBicyclicCys3Pos
    };
  }
  return {
    seed: typeof seed === 'number' && Number.isFinite(seed) ? Math.max(0, Math.floor(seed)) : 42,
    peptideDesignMode,
    peptideBinderLength,
    peptideUseInitialSequence,
    peptideInitialSequence,
    peptideSequenceMask,
    peptideIterations,
    peptidePopulationSize,
    peptideEliteSize,
    peptideMutationRate,
    peptideBicyclicLinkerCcd,
    peptideBicyclicCysPositionMode,
    peptideBicyclicFixTerminalCys,
    peptideBicyclicIncludeExtraCys,
    peptideBicyclicCys1Pos,
    peptideBicyclicCys2Pos,
    peptideBicyclicCys3Pos
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
        return {
          id,
          type: 'contact' as const,
          token1_chain: typeof raw.token1_chain === 'string' && raw.token1_chain ? raw.token1_chain : 'A',
          token1_residue: Math.max(1, Number(raw.token1_residue || 1)),
          token2_chain: typeof raw.token2_chain === 'string' && raw.token2_chain ? raw.token2_chain : 'B',
          token2_residue: Math.max(1, Number(raw.token2_residue || 1)),
          max_distance: Math.max(1, Number(raw.max_distance || 5)),
          force: raw.force === undefined ? true : Boolean(raw.force)
        };
      }

      if (type === 'bond') {
        return {
          id,
          type: 'bond' as const,
          atom1_chain: typeof raw.atom1_chain === 'string' && raw.atom1_chain ? raw.atom1_chain : 'A',
          atom1_residue: Math.max(1, Number(raw.atom1_residue || 1)),
          atom1_atom: typeof raw.atom1_atom === 'string' && raw.atom1_atom ? raw.atom1_atom : 'CA',
          atom2_chain: typeof raw.atom2_chain === 'string' && raw.atom2_chain ? raw.atom2_chain : 'B',
          atom2_residue: Math.max(1, Number(raw.atom2_residue || 1)),
          atom2_atom: typeof raw.atom2_atom === 'string' && raw.atom2_atom ? raw.atom2_atom : 'CA'
        };
      }

      if (type === 'pocket') {
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
          binder: typeof raw.binder === 'string' && raw.binder ? raw.binder : 'A',
          contacts,
          max_distance: Math.max(1, Number(raw.max_distance || 6)),
          force: raw.force === undefined ? true : Boolean(raw.force)
        };
      }

      return null;
    })
    .filter(Boolean) as PredictionConstraint[];
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
  const store = readStore();
  const found = store[projectId];
  if (!found || !Array.isArray(found.components)) return null;
  return normalizeConfig(found);
}

export function saveProjectInputConfig(projectId: string, config: ProjectInputConfig): void {
  const store = readStore();
  store[projectId] = config;
  writeStore(store);
}

export function removeProjectInputConfig(projectId: string): void {
  const store = readStore();
  delete store[projectId];
  writeStore(store);
}

export function loadProjectUiState(projectId: string): ProjectUiState | null {
  const store = readUiStore();
  const found = store[projectId];
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
    taskProteinTemplates,
    templateContentPool,
    taskAffinityUploads,
    affinityUploads: legacyAffinityUploads,
    activeConstraintId,
    selectedConstraintTemplateComponentId
  };
}

export function saveProjectUiState(projectId: string, uiState: ProjectUiState): void {
  const baseStore = readUiStore();
  const currentStoredState = baseStore[projectId];
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

  baseStore[projectId] = {
    proteinTemplates,
    taskProteinTemplates,
    taskAffinityUploads,
    templateContentPool: compactTemplateContentPool(contentPool, usedPoolKeys),
    affinityUploads,
    activeConstraintId: uiState.activeConstraintId || null,
    selectedConstraintTemplateComponentId: uiState.selectedConstraintTemplateComponentId || null
  };

  try {
    writeUiStore(baseStore);
  } catch {
    baseStore[projectId] = {
      proteinTemplates: {},
      taskProteinTemplates: {},
      taskAffinityUploads: {},
      affinityUploads: {
        target: null,
        ligand: null
      },
      activeConstraintId: uiState.activeConstraintId || null,
      selectedConstraintTemplateComponentId: uiState.selectedConstraintTemplateComponentId || null
    };
    writeUiStore(baseStore);
  }
}

export function removeProjectUiState(projectId: string): void {
  const store = readUiStore();
  delete store[projectId];
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
