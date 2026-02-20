import type {
  InputComponent,
  PredictionConstraint,
  PredictionConstraintType,
  ProjectInputConfig,
  ProjectTask,
  ProteinTemplateUpload,
} from '../../types/models';
import { normalizeInputComponents } from '../../utils/projectInputs';

export interface DraftFingerprintFields {
  taskName: string;
  taskSummary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: ProjectInputConfig;
}

export function normalizeComponents(components: InputComponent[]): InputComponent[] {
  return normalizeInputComponents(components);
}

export function nonEmptyComponents(components: InputComponent[]): InputComponent[] {
  return components.filter((component) => Boolean(component.sequence));
}

export function computeUseMsaFlag(components: InputComponent[], fallback = true): boolean {
  const proteinComponents = components.filter((component) => component.type === 'protein');
  if (proteinComponents.length === 0) return fallback;
  return proteinComponents.some((component) => component.useMsa !== false);
}

export function listIncompleteComponentOrders(components: InputComponent[]): number[] {
  const missing: number[] = [];
  components.forEach((component, index) => {
    if (!component.sequence.trim()) {
      missing.push(index + 1);
    }
  });
  return missing;
}

export function sortProjectTasks(rows: ProjectTask[]): ProjectTask[] {
  return [...rows].sort((a, b) => {
    const at = new Date(a.submitted_at || a.created_at).getTime();
    const bt = new Date(b.submitted_at || b.created_at).getTime();
    return bt - at;
  });
}

export function listConstraintResidues(constraint: PredictionConstraint): Array<{ chainId: string; residue: number }> {
  if (constraint.type === 'contact') {
    return [
      { chainId: constraint.token1_chain, residue: constraint.token1_residue },
      { chainId: constraint.token2_chain, residue: constraint.token2_residue }
    ];
  }
  if (constraint.type === 'bond') {
    return [
      { chainId: constraint.atom1_chain, residue: constraint.atom1_residue },
      { chainId: constraint.atom2_chain, residue: constraint.atom2_residue }
    ];
  }
  return constraint.contacts.map((item) => ({ chainId: item[0], residue: item[1] }));
}

const ALL_CONSTRAINT_TYPES: PredictionConstraintType[] = ['contact', 'bond', 'pocket'];
const AF3_CONSTRAINT_TYPES: PredictionConstraintType[] = ['bond'];

export function allowedConstraintTypesForBackend(backend: string): PredictionConstraintType[] {
  const normalized = String(backend || '').trim().toLowerCase();
  if (normalized === 'alphafold3' || normalized === 'protenix') {
    return AF3_CONSTRAINT_TYPES;
  }
  return ALL_CONSTRAINT_TYPES;
}

export function filterConstraintsByBackend(constraints: PredictionConstraint[], backend: string): PredictionConstraint[] {
  const allowedTypeSet = new Set(allowedConstraintTypesForBackend(backend));
  return constraints.filter((item) => allowedTypeSet.has(item.type));
}

export function normalizeConfigForBackend(inputConfig: ProjectInputConfig, backend: string): ProjectInputConfig {
  return {
    version: 1,
    components: normalizeComponents(inputConfig.components),
    constraints: filterConstraintsByBackend(inputConfig.constraints, backend),
    properties: inputConfig.properties,
    options: inputConfig.options
  };
}

export function createDraftFingerprint(draft: DraftFingerprintFields): string {
  const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, draft.backend);
  const hasMsa = computeUseMsaFlag(normalizedConfig.components, draft.use_msa);
  return JSON.stringify({
    taskName: draft.taskName.trim(),
    taskSummary: draft.taskSummary.trim(),
    backend: draft.backend,
    use_msa: hasMsa,
    color_mode: draft.color_mode || 'white',
    inputConfig: normalizedConfig
  });
}

export function createComputationFingerprint(draft: DraftFingerprintFields): string {
  const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, draft.backend);
  const hasMsa = computeUseMsaFlag(normalizedConfig.components, draft.use_msa);
  return JSON.stringify({
    backend: draft.backend,
    use_msa: hasMsa,
    inputConfig: normalizedConfig
  });
}

export function createProteinTemplatesFingerprint(templates: Record<string, ProteinTemplateUpload>): string {
  const normalized = Object.entries(templates)
    .sort(([componentA], [componentB]) => componentA.localeCompare(componentB))
    .map(([componentId, upload]) => ({
      componentId,
      fileName: upload.fileName,
      format: upload.format,
      content: upload.content,
      chainId: upload.chainId,
      chainSequences: Object.entries(upload.chainSequences || {}).sort(([chainA], [chainB]) =>
        chainA.localeCompare(chainB)
      )
    }));
  return JSON.stringify(normalized);
}

export function hasProteinTemplates(templates: Record<string, ProteinTemplateUpload> | null | undefined): boolean {
  return Boolean(templates && Object.keys(templates).length > 0);
}

export function hasRecordData(value: unknown): boolean {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && Object.keys(value as Record<string, unknown>).length > 0);
}
