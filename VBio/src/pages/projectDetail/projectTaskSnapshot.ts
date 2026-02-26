import type { InputComponent, PredictionConstraint, Project, ProjectInputConfig, ProjectTask, ProteinTemplateUpload } from '../../types/models';
import type { AffinityPersistedUpload, AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import {
  buildDefaultInputConfig,
  createInputComponent,
  normalizeComponentSequence,
  normalizeInputComponents,
} from '../../utils/projectInputs';
import { normalizeWorkflowKey } from '../../utils/workflows';

export const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
export const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';
export const AFFINITY_UPLOAD_SCOPE_PREFIX = '__new_from_';
export const AFFINITY_UPLOAD_SCOPE_NEW = '__new__';
export const LEADOPT_TARGET_UPLOAD_COMPONENT_ID = '__leadopt_target_upload__';
export const LEADOPT_LIGAND_UPLOAD_COMPONENT_ID = '__leadopt_ligand_upload__';

export interface LeadOptPersistedUpload {
  fileName: string;
  content: string;
}

export interface LeadOptPersistedUploads {
  target: LeadOptPersistedUpload | null;
  ligand: LeadOptPersistedUpload | null;
}

function normalizeComponents(components: InputComponent[]): InputComponent[] {
  return normalizeInputComponents(components);
}

export function defaultConfigFromProject(project: Project): ProjectInputConfig {
  const workflowKey = normalizeWorkflowKey(project.task_type);
  const config = buildDefaultInputConfig(workflowKey);
  if (workflowKey === 'affinity') {
    config.components = [createInputComponent('protein')];
    return config;
  }
  const proteinSequence = (project.protein_sequence || '').trim();
  const ligandSmiles = (project.ligand_smiles || '').trim();

  const components: InputComponent[] = [];
  if (proteinSequence) {
    components.push({
      ...createInputComponent('protein'),
      sequence: proteinSequence,
      useMsa: project.use_msa
    });
  }
  if (ligandSmiles) {
    components.push({
      ...createInputComponent('ligand'),
      sequence: ligandSmiles,
      inputMethod: 'jsme'
    });
  }
  if (components.length === 0) {
    components.push(createInputComponent('protein'));
  }
  config.components = components;
  return config;
}

export function affinityUploadRoleFromComponentId(componentId: string): 'target' | 'ligand' | null {
  const normalizedId = String(componentId || '').trim();
  if (normalizedId === AFFINITY_TARGET_UPLOAD_COMPONENT_ID) return 'target';
  if (normalizedId === AFFINITY_LIGAND_UPLOAD_COMPONENT_ID) return 'ligand';
  return null;
}

export function leadOptUploadRoleFromComponentId(componentId: string): 'target' | 'ligand' | null {
  const normalizedId = String(componentId || '').trim();
  if (normalizedId === LEADOPT_TARGET_UPLOAD_COMPONENT_ID) return 'target';
  if (normalizedId === LEADOPT_LIGAND_UPLOAD_COMPONENT_ID) return 'ligand';
  return null;
}

export function extractAffinityUploadFromRawComponent(component: Record<string, unknown>): {
  role: 'target' | 'ligand';
  fileName: string;
  content: string;
} | null {
  const componentId = typeof component.id === 'string' ? component.id.trim() : '';
  const uploadMeta =
    component.affinityUpload && typeof component.affinityUpload === 'object'
      ? (component.affinityUpload as Record<string, unknown>)
      : component.affinity_upload && typeof component.affinity_upload === 'object'
        ? (component.affinity_upload as Record<string, unknown>)
        : null;
  const roleFromMeta = uploadMeta?.role;
  const role =
    roleFromMeta === 'target' || roleFromMeta === 'ligand'
      ? roleFromMeta
      : affinityUploadRoleFromComponentId(componentId);
  if (!role) return null;
  const fileNameFromMeta = uploadMeta && typeof uploadMeta.fileName === 'string' ? uploadMeta.fileName.trim() : '';
  const fallbackName = role === 'target' ? 'target_upload.pdb' : 'ligand_upload.sdf';
  const fileName = fileNameFromMeta || fallbackName;
  const contentFromMeta = uploadMeta && typeof uploadMeta.content === 'string' ? uploadMeta.content : '';
  const contentFromSequence = typeof component.sequence === 'string' ? component.sequence : '';
  const content = (contentFromMeta || contentFromSequence || '').trim();
  if (!content) return null;
  return { role, fileName, content };
}

export function readTaskAffinityUploads(task: ProjectTask | null): AffinityPersistedUploads {
  const empty: AffinityPersistedUploads = { target: null, ligand: null };
  if (!task || !Array.isArray(task.components)) return empty;
  const rawComponents = task.components as unknown as Array<Record<string, unknown>>;
  let target: AffinityPersistedUpload | null = null;
  let ligand: AffinityPersistedUpload | null = null;
  for (const component of rawComponents) {
    if (!component || typeof component !== 'object') continue;
    const parsed = extractAffinityUploadFromRawComponent(component);
    if (!parsed) continue;
    if (parsed.role === 'target' && !target) {
      target = { fileName: parsed.fileName, content: parsed.content };
    }
    if (parsed.role === 'ligand' && !ligand) {
      ligand = { fileName: parsed.fileName, content: parsed.content };
    }
  }
  return { target, ligand };
}

function extractLeadOptUploadFromRawComponent(component: Record<string, unknown>): {
  role: 'target' | 'ligand';
  fileName: string;
  content: string;
} | null {
  const componentId = typeof component.id === 'string' ? component.id.trim() : '';
  const uploadMeta =
    component.leadOptUpload && typeof component.leadOptUpload === 'object'
      ? (component.leadOptUpload as Record<string, unknown>)
      : component.lead_opt_upload && typeof component.lead_opt_upload === 'object'
        ? (component.lead_opt_upload as Record<string, unknown>)
        : null;
  const roleFromMeta = uploadMeta?.role;
  const role =
    roleFromMeta === 'target' || roleFromMeta === 'ligand'
      ? roleFromMeta
      : leadOptUploadRoleFromComponentId(componentId);
  if (!role) return null;
  const fileNameFromMeta = uploadMeta && typeof uploadMeta.fileName === 'string' ? uploadMeta.fileName.trim() : '';
  const fallbackName = role === 'target' ? 'leadopt_target_upload.pdb' : 'leadopt_ligand_upload.sdf';
  const fileName = fileNameFromMeta || fallbackName;
  const contentFromMeta = uploadMeta && typeof uploadMeta.content === 'string' ? uploadMeta.content : '';
  const contentFromSequence = typeof component.sequence === 'string' ? component.sequence : '';
  const content = (contentFromMeta || contentFromSequence || '').trim();
  if (!content) return null;
  return { role, fileName, content };
}

export function readTaskLeadOptUploads(task: ProjectTask | null): LeadOptPersistedUploads {
  const empty: LeadOptPersistedUploads = { target: null, ligand: null };
  if (!task || !Array.isArray(task.components)) return empty;
  const rawComponents = task.components as unknown as Array<Record<string, unknown>>;
  let target: LeadOptPersistedUpload | null = null;
  let ligand: LeadOptPersistedUpload | null = null;
  for (const component of rawComponents) {
    if (!component || typeof component !== 'object') continue;
    const parsed = extractLeadOptUploadFromRawComponent(component);
    if (!parsed) continue;
    if (parsed.role === 'target' && !target) {
      target = { fileName: parsed.fileName, content: parsed.content };
    }
    if (parsed.role === 'ligand' && !ligand) {
      ligand = { fileName: parsed.fileName, content: parsed.content };
    }
  }
  return { target, ligand };
}

export function buildLeadOptUploadSnapshotComponents(
  baseComponents: InputComponent[],
  uploads: LeadOptPersistedUploads,
  ligandSmiles = ''
): InputComponent[] {
  const filteredBase = normalizeComponents(baseComponents).filter((component) => {
    const role = leadOptUploadRoleFromComponentId(component.id);
    return role === null;
  });
  const targetUpload = uploads.target;
  if (!targetUpload || !targetUpload.fileName.trim() || !targetUpload.content.trim()) {
    return filteredBase;
  }
  const targetUploadComponent = ({
    id: LEADOPT_TARGET_UPLOAD_COMPONENT_ID,
    type: 'protein',
    numCopies: 1,
    sequence: '',
    useMsa: false,
    cyclic: false,
    leadOptUpload: {
      role: 'target',
      fileName: targetUpload.fileName,
      content: targetUpload.content
    }
  } as unknown) as InputComponent;
  const next: InputComponent[] = [...filteredBase, targetUploadComponent];

  const ligandUpload = uploads.ligand;
  if (ligandUpload && ligandUpload.fileName.trim() && ligandUpload.content.trim()) {
    const ligandUploadComponent = ({
      id: LEADOPT_LIGAND_UPLOAD_COMPONENT_ID,
      type: 'ligand',
      numCopies: 1,
      sequence: normalizeComponentSequence('ligand', ligandSmiles),
      inputMethod: 'jsme',
      leadOptUpload: {
        role: 'ligand',
        fileName: ligandUpload.fileName,
        content: ligandUpload.content
      }
    } as unknown) as InputComponent;
    next.push(ligandUploadComponent);
  }
  return next;
}

export async function buildAffinityUploadSnapshotComponents(
  baseComponents: InputComponent[],
  targetFile: File | null,
  ligandFile: File | null,
  ligandSmiles = ''
): Promise<InputComponent[]> {
  const filteredBase = normalizeComponents(baseComponents).filter((component) => {
    const role = affinityUploadRoleFromComponentId(component.id);
    return role === null;
  });
  if (!targetFile) return filteredBase;
  const [targetContent, ligandContent] = await Promise.all([
    targetFile
      .text()
      .then((value) => value)
      .catch(() => ''),
    ligandFile
      ? ligandFile
          .text()
          .then((value) => value)
          .catch(() => '')
      : Promise.resolve('')
  ]);

  const targetUploadComponent = ({
    id: AFFINITY_TARGET_UPLOAD_COMPONENT_ID,
    type: 'protein',
    numCopies: 1,
    sequence: '',
    useMsa: false,
    cyclic: false,
    affinityUpload: {
      role: 'target',
      fileName: targetFile.name,
      content: targetContent
    }
  } as unknown) as InputComponent;

  const next: InputComponent[] = [...filteredBase, targetUploadComponent];

  if (ligandFile) {
    const ligandUploadComponent = ({
      id: AFFINITY_LIGAND_UPLOAD_COMPONENT_ID,
      type: 'ligand',
      numCopies: 1,
      sequence: normalizeComponentSequence('ligand', ligandSmiles),
      inputMethod: 'jsme',
      affinityUpload: {
        role: 'ligand',
        fileName: ligandFile.name,
        content: ligandContent
      }
    } as unknown) as InputComponent;
    next.push(ligandUploadComponent);
  }

  return next;
}

export function readTaskComponents(task: ProjectTask | null): InputComponent[] {
  if (!task) return [];

  const rawComponents = Array.isArray(task.components) ? (task.components as unknown as Array<Record<string, unknown>>) : [];
  const normalizedRawComponents: Array<Record<string, unknown>> = [];
  for (const component of rawComponents) {
    if (!component || typeof component !== 'object') continue;
    if (leadOptUploadRoleFromComponentId(String(component.id || '').trim())) {
      continue;
    }
    const upload = extractAffinityUploadFromRawComponent(component);
    if (!upload) {
      normalizedRawComponents.push(component);
      continue;
    }
    if (upload.role === 'target') {
      continue;
    }
    const ligandSequence = normalizeComponentSequence(
      'ligand',
      typeof component.sequence === 'string' ? component.sequence : ''
    );
    if (!ligandSequence) {
      continue;
    }
    normalizedRawComponents.push({
      ...component,
      type: 'ligand',
      inputMethod: 'jsme',
      sequence: ligandSequence,
      affinityUpload: undefined,
      affinity_upload: undefined
    });
  }

  const components =
    normalizedRawComponents.length > 0 ? normalizeComponents(normalizedRawComponents as unknown as InputComponent[]) : [];
  if (components.length > 0) return components;

  const fallback: InputComponent[] = [];
  const proteinSequence = normalizeComponentSequence('protein', task.protein_sequence || '');
  const ligandValue = normalizeComponentSequence('ligand', task.ligand_smiles || '');
  if (proteinSequence) {
    fallback.push({
      id: 'task-protein-1',
      type: 'protein',
      numCopies: 1,
      sequence: proteinSequence,
      useMsa: true,
      cyclic: false
    });
  }
  if (ligandValue) {
    fallback.push({
      id: 'task-ligand-1',
      type: 'ligand',
      numCopies: 1,
      sequence: ligandValue,
      inputMethod: 'jsme'
    });
  }
  return fallback;
}

function normalizeTaskTemplateUpload(value: unknown): ProteinTemplateUpload | null {
  if (!value || typeof value !== 'object') return null;
  const fileName = typeof (value as any).fileName === 'string' ? (value as any).fileName.trim() : '';
  const format = (value as any).format === 'cif' ? 'cif' : (value as any).format === 'pdb' ? 'pdb' : null;
  const content = typeof (value as any).content === 'string' ? (value as any).content : '';
  const chainId = typeof (value as any).chainId === 'string' ? (value as any).chainId.trim() : '';
  const chainSequencesValue = (value as any).chainSequences;
  const chainSequences =
    chainSequencesValue && typeof chainSequencesValue === 'object'
      ? (chainSequencesValue as Record<string, string>)
      : {};
  if (!fileName || !format || !content || !chainId) return null;
  return {
    fileName,
    format,
    content,
    chainId,
    chainSequences
  };
}

export function readTaskProteinTemplates(task: ProjectTask | null): Record<string, ProteinTemplateUpload> {
  const templates: Record<string, ProteinTemplateUpload> = {};
  if (!task || !Array.isArray(task.components)) return templates;
  const rawComponents = task.components as unknown as Array<Record<string, unknown>>;
  for (const component of rawComponents) {
    if (!component || component.type !== 'protein') continue;
    const componentId = typeof component.id === 'string' ? component.id.trim() : '';
    if (!componentId) continue;
    const upload = normalizeTaskTemplateUpload((component as any).templateUpload || (component as any).template_upload);
    if (!upload) continue;
    templates[componentId] = upload;
  }
  return templates;
}

export function addTemplatesToTaskSnapshotComponents(
  components: InputComponent[],
  templates: Record<string, ProteinTemplateUpload>
): InputComponent[] {
  return components.map((component) => {
    if (component.type !== 'protein') return component;
    const upload = templates[component.id];
    if (!upload) return component;
    const compactTemplateUpload = {
      fileName: upload.fileName,
      format: upload.format,
      chainId: upload.chainId,
      chainSequences: upload.chainSequences
    };
    return ({
      ...(component as unknown as Record<string, unknown>),
      templateUpload: compactTemplateUpload
    } as unknown) as InputComponent;
  });
}

export function mergeTaskSnapshotIntoConfig(baseConfig: ProjectInputConfig, task: ProjectTask | null): ProjectInputConfig {
  if (!task) return baseConfig;

  const taskComponents = readTaskComponents(task);
  const taskConstraints = Array.isArray(task.constraints) ? (task.constraints as PredictionConstraint[]) : null;
  const taskProperties =
    task.properties && typeof task.properties === 'object' ? (task.properties as ProjectInputConfig['properties']) : null;
  const taskSeed = typeof task.seed === 'number' && Number.isFinite(task.seed) ? Math.max(0, Math.floor(task.seed)) : null;

  return {
    ...baseConfig,
    components: taskComponents.length > 0 ? taskComponents : baseConfig.components,
    constraints: taskConstraints ?? baseConfig.constraints,
    properties: taskProperties ?? baseConfig.properties,
    options: {
      ...baseConfig.options,
      seed: taskSeed ?? baseConfig.options.seed
    }
  };
}

export function isDraftTaskSnapshot(task: ProjectTask | null): boolean {
  if (!task) return false;
  return task.task_state === 'DRAFT' && !String(task.task_id || '').trim();
}

export function resolveAffinityUploadStorageTaskRowId(taskRowId: string | null | undefined): string | null {
  const normalized = String(taskRowId || '').trim();
  if (!normalized || normalized === AFFINITY_UPLOAD_SCOPE_NEW) return null;
  return normalized;
}
