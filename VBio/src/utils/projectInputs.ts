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

export interface ProjectUiState {
  proteinTemplates: Record<string, ProteinTemplateUpload>;
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
  const components = Array.isArray(value.components) && value.components.length > 0 ? value.components : base.components;
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

  const activeConstraintId =
    typeof found.activeConstraintId === 'string' && found.activeConstraintId.trim() ? found.activeConstraintId : null;
  const selectedConstraintTemplateComponentId =
    typeof (found as any).selectedConstraintTemplateComponentId === 'string' &&
    (found as any).selectedConstraintTemplateComponentId.trim()
      ? (found as any).selectedConstraintTemplateComponentId
      : null;

  const rawTemplates = found.proteinTemplates;
  const proteinTemplates: Record<string, ProteinTemplateUpload> = {};
  if (rawTemplates && typeof rawTemplates === 'object') {
    for (const [componentId, upload] of Object.entries(rawTemplates)) {
      if (!upload || typeof upload !== 'object') continue;
      const fileName = typeof (upload as any).fileName === 'string' ? (upload as any).fileName : '';
      const format = (upload as any).format === 'cif' ? 'cif' : (upload as any).format === 'pdb' ? 'pdb' : null;
      const content = typeof (upload as any).content === 'string' ? (upload as any).content : '';
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
  }

  return {
    proteinTemplates,
    activeConstraintId,
    selectedConstraintTemplateComponentId
  };
}

export function saveProjectUiState(projectId: string, uiState: ProjectUiState): void {
  const store = readUiStore();
  store[projectId] = uiState;
  writeUiStore(store);
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
