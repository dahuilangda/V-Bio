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

export interface ProjectUiState {
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  taskProteinTemplates?: Record<string, Record<string, ProteinTemplateUpload>>;
  templateContentPool?: Record<string, string>;
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

export function buildDefaultInputConfig(): ProjectInputConfig {
  return {
    version: 1,
    components: [createInputComponent('protein')],
    constraints: [],
    properties: {
      affinity: false,
      target: null,
      ligand: null,
      binder: null
    },
    options: {
      seed: 42
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
  const raw = (value || {}) as Partial<PredictionOptions>;
  const seed = raw.seed;
  if (seed === null) {
    return { seed: null };
  }
  return {
    seed: typeof seed === 'number' && Number.isFinite(seed) ? Math.max(0, Math.floor(seed)) : 42
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
    templateContentPool: compactTemplateContentPool(contentPool, usedPoolKeys),
    activeConstraintId: uiState.activeConstraintId || null,
    selectedConstraintTemplateComponentId: uiState.selectedConstraintTemplateComponentId || null
  };

  try {
    writeUiStore(baseStore);
  } catch {
    baseStore[projectId] = {
      proteinTemplates: {},
      taskProteinTemplates: {},
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
