import type { InputComponent, Project, ProjectTask } from '../../types/models';
import { assignChainIdsForComponents } from '../../utils/chainAssignments';
import { normalizeComponentSequence, normalizeInputComponents } from '../../utils/projectInputs';
import type {
  SeedFilterOption,
  SortDirection,
  SortKey,
  StructureSearchMode,
  SubmittedWithinDaysOption,
  TaskSelectionContext,
  TaskWorkspaceView,
  WorkspacePairPreference
} from './taskListTypes';

function normalizeTaskComponents(components: InputComponent[]): InputComponent[] {
  return normalizeInputComponents(components);
}

const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';
const LEADOPT_TARGET_UPLOAD_COMPONENT_ID = '__leadopt_target_upload__';
const LEADOPT_LIGAND_UPLOAD_COMPONENT_ID = '__leadopt_ligand_upload__';

type TaskLigandSourceWorkflow = 'prediction' | 'peptide_design' | 'affinity' | 'lead_optimization' | 'auto';

function normalizeTaskLigandSourceWorkflow(value: string | null | undefined): TaskLigandSourceWorkflow {
  const normalized = String(value || '')
    .trim()
    .toLowerCase();
  if (normalized === 'prediction' || normalized === 'peptide_design' || normalized === 'affinity' || normalized === 'lead_optimization') {
    return normalized;
  }
  return 'auto';
}

function resolveAffinityUploadRole(component: Record<string, unknown>): 'target' | 'ligand' | null {
  const id = typeof component.id === 'string' ? component.id.trim() : '';
  if (id === AFFINITY_TARGET_UPLOAD_COMPONENT_ID || id === AFFINITY_LIGAND_UPLOAD_COMPONENT_ID) {
    return id === AFFINITY_TARGET_UPLOAD_COMPONENT_ID ? 'target' : 'ligand';
  }
  const uploadMeta =
    component.affinityUpload && typeof component.affinityUpload === 'object'
      ? (component.affinityUpload as Record<string, unknown>)
      : component.affinity_upload && typeof component.affinity_upload === 'object'
        ? (component.affinity_upload as Record<string, unknown>)
        : null;
  const role = typeof uploadMeta?.role === 'string' ? uploadMeta.role.trim().toLowerCase() : '';
  if (role === 'target' || role === 'ligand') return role;
  return null;
}

function resolveLeadOptUploadRole(component: Record<string, unknown>): 'target' | 'ligand' | null {
  const id = typeof component.id === 'string' ? component.id.trim() : '';
  if (id === LEADOPT_TARGET_UPLOAD_COMPONENT_ID || id === LEADOPT_LIGAND_UPLOAD_COMPONENT_ID) {
    return id === LEADOPT_TARGET_UPLOAD_COMPONENT_ID ? 'target' : 'ligand';
  }
  const uploadMeta =
    component.leadOptUpload && typeof component.leadOptUpload === 'object'
      ? (component.leadOptUpload as Record<string, unknown>)
      : component.lead_opt_upload && typeof component.lead_opt_upload === 'object'
        ? (component.lead_opt_upload as Record<string, unknown>)
        : null;
  const role = typeof uploadMeta?.role === 'string' ? uploadMeta.role.trim().toLowerCase() : '';
  if (role === 'target' || role === 'ligand') return role;
  return null;
}

function normalizeTaskRawComponent(component: Record<string, unknown>): Record<string, unknown> | null {
  const affinityUploadRole = resolveAffinityUploadRole(component);
  if (affinityUploadRole === 'target') return null;
  if (affinityUploadRole === 'ligand') {
    const sequence = normalizeComponentSequence('ligand', typeof component.sequence === 'string' ? component.sequence : '');
    if (!sequence) return null;
    return {
      ...component,
      type: 'ligand',
      inputMethod: 'jsme',
      sequence,
      affinityUpload: undefined,
      affinity_upload: undefined
    };
  }

  const leadOptUploadRole = resolveLeadOptUploadRole(component);
  if (leadOptUploadRole === 'target') return null;
  if (leadOptUploadRole === 'ligand') {
    const sequence = normalizeComponentSequence('ligand', typeof component.sequence === 'string' ? component.sequence : '');
    if (!sequence) return null;
    return {
      ...component,
      type: 'ligand',
      inputMethod: 'jsme',
      sequence,
      leadOptUpload: undefined,
      lead_opt_upload: undefined
    };
  }
  return component;
}

function readTaskComponents(task: ProjectTask): InputComponent[] {
  const rawComponents = Array.isArray(task.components)
    ? (task.components as unknown[])
        .filter((component): component is Record<string, unknown> => Boolean(component && typeof component === 'object'))
        .map((component) => normalizeTaskRawComponent(component))
        .filter((component): component is Record<string, unknown> => component !== null)
    : [];
  const components = rawComponents.length > 0 ? normalizeTaskComponents(rawComponents as unknown as InputComponent[]) : [];
  return components;
}

function readTaskPrimaryLigand(
  components: InputComponent[],
  preferredComponentId: string | null,
  preferredLigandChainId: string | null = null,
  strictPreferredLigand = false,
  workflow: TaskLigandSourceWorkflow = 'auto'
): { smiles: string; isSmiles: boolean } {
  const normalizedWorkflow = normalizeTaskLigandSourceWorkflow(workflow);

  // Respect explicit ligand component selection first (e.g. binding ligand = Comp2).
  // Ligand View must reflect the exact user-selected component.
  if (preferredComponentId) {
    const selected = components.find((item) => item.id === preferredComponentId);
    const selectedValue =
      selected && selected.type === 'ligand' ? normalizeComponentSequence('ligand', selected.sequence) : '';
    if (selected && selected.type === 'ligand' && selectedValue) {
      return {
        smiles: selectedValue,
        isSmiles: selected.inputMethod !== 'ccd'
      };
    }
  }

  if (normalizedWorkflow === 'affinity' || normalizedWorkflow === 'lead_optimization') {
    const uploadedLigand = components.find((item) => {
      if (item.type !== 'ligand') return false;
      if (!normalizeComponentSequence('ligand', item.sequence)) return false;
      const raw = item as unknown as Record<string, unknown>;
      if (normalizedWorkflow === 'affinity') {
        return resolveAffinityUploadRole(raw) === 'ligand';
      }
      return resolveLeadOptUploadRole(raw) === 'ligand';
    });
    if (uploadedLigand) {
      const value = normalizeComponentSequence('ligand', uploadedLigand.sequence);
      return {
        smiles: value,
        isSmiles: uploadedLigand.inputMethod !== 'ccd'
      };
    }
    return {
      smiles: '',
      isSmiles: false
    };
  }

  if (strictPreferredLigand && preferredLigandChainId) {
    // Strict mode means the binding ligand must resolve from component selection.
    return {
      smiles: '',
      isSmiles: false
    };
  }

  return {
    smiles: '',
    isSmiles: false
  };
}

function sortProjectTasks(rows: ProjectTask[]): ProjectTask[] {
  return [...rows].sort((a, b) => {
    const at = new Date(a.submitted_at || a.created_at).getTime();
    const bt = new Date(b.submitted_at || b.created_at).getTime();
    return bt - at;
  });
}

function isProjectTaskRow(value: ProjectTask | null | undefined): value is ProjectTask {
  return Boolean(value && typeof value === 'object' && typeof value.id === 'string' && value.id.trim());
}

function sanitizeTaskRows(rows: Array<ProjectTask | null | undefined>): ProjectTask[] {
  return rows.filter((row): row is ProjectTask => isProjectTaskRow(row));
}

function isProjectRow(value: Project | null | undefined): value is Project {
  return Boolean(value && typeof value === 'object' && typeof value.id === 'string' && value.id.trim());
}

function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

function readFirstFiniteMetric(data: Record<string, unknown>, paths: string[]): number | null {
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function readFirstNonEmptyStringMetric(data: Record<string, unknown> | null, paths: string[]): string {
  if (!data) return '';
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return '';
}

function readStringListMetric(data: Record<string, unknown> | null, paths: string[]): string[] {
  if (!data) return [];
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (!Array.isArray(value)) continue;
    const rows = value
      .filter((item): item is string => typeof item === 'string')
      .map((item) => item.trim())
      .filter(Boolean);
    if (rows.length > 0) return rows;
  }
  return [];
}

function readLigandSmilesFromMap(data: Record<string, unknown> | null, preferredLigandChainId: string | null): string {
  if (!data) return '';
  const mapCandidates: unknown[] = [
    readObjectPath(data, 'ligand_smiles_map'),
    readObjectPath(data, 'ligand.smiles_map'),
    readObjectPath(data, 'ligand.smilesMap')
  ];
  const preferredChain = normalizeChainKey(String(preferredLigandChainId || ''));

  for (const mapValue of mapCandidates) {
    if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) continue;
    const entries = Object.entries(mapValue as Record<string, unknown>)
      .map(([key, value]) => {
        if (typeof value !== 'string') return null;
        const normalizedValue = normalizeComponentSequence('ligand', value);
        if (!normalizedValue) return null;
        const keyText = String(key || '').trim();
        return {
          key: keyText,
          keyChain: normalizeChainKey(keyText.includes(':') ? keyText.slice(0, keyText.indexOf(':')) : keyText),
          value: normalizedValue
        };
      })
      .filter((item): item is { key: string; keyChain: string; value: string } => item !== null);
    if (entries.length === 0) continue;

    if (preferredChain) {
      const matched = entries.find((item) => item.keyChain === preferredChain);
      if (matched) return matched.value;
    }

    if (entries.length === 1) return entries[0].value;
    return entries[0].value;
  }

  return '';
}

function readTaskLigandSmilesHint(task: ProjectTask, preferredLigandChainId: string | null = null): string {
  const taskLigandSmiles = normalizeComponentSequence('ligand', String(task.ligand_smiles || ''));
  if (taskLigandSmiles) return taskLigandSmiles;

  const confidenceData =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const affinityData =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : null;

  const directConfidenceSmiles = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(confidenceData, [
      'ligand_smiles',
      'ligand.smiles',
      'ligandSmiles',
      'request.ligand_smiles',
      'inputs.ligand_smiles'
    ])
  );
  if (directConfidenceSmiles) return directConfidenceSmiles;

  const mappedConfidenceSmiles = normalizeComponentSequence(
    'ligand',
    readLigandSmilesFromMap(confidenceData, preferredLigandChainId)
  );
  if (mappedConfidenceSmiles) return mappedConfidenceSmiles;

  const directAffinitySmiles = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(affinityData, [
      'ligand_smiles',
      'ligand.smiles',
      'ligandSmiles',
      'request.ligand_smiles',
      'inputs.ligand_smiles'
    ])
  );
  if (directAffinitySmiles) return directAffinitySmiles;

  return normalizeComponentSequence('ligand', readLigandSmilesFromMap(affinityData, preferredLigandChainId));
}

function readTaskLigandRenderSmiles(task: ProjectTask, _preferredLigandChainId: string | null = null): string {
  const confidenceData =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const affinityData =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : null;

  const fromConfidence = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(confidenceData, [
      'ligand_display_smiles',
      'ligand.display_smiles',
      'ligandDisplaySmiles'
    ])
  );
  if (fromConfidence) return fromConfidence;

  const fromAffinity = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(affinityData, [
      'ligand_display_smiles',
      'ligand.display_smiles',
      'ligandDisplaySmiles'
    ])
  );
  if (fromAffinity) return fromAffinity;

  return '';
}

function splitChainTokens(value: string): string[] {
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function isLikelyChainToken(value: string): boolean {
  const raw = String(value || '').trim();
  if (!raw) return false;
  if (raw.length > 12) return false;
  return /^[A-Za-z0-9._:-]+$/.test(raw);
}

function collectCoverageChainBuckets(confidence: Record<string, unknown> | null): {
  all: string[];
  ligand: string[];
  polymer: string[];
} {
  const all: string[] = [];
  const ligand: string[] = [];
  const polymer: string[] = [];
  if (!confidence) {
    return { all, ligand, polymer };
  }
  const pushUnique = (bucket: string[], chainIdRaw: unknown) => {
    const chainId = String(chainIdRaw || '').trim();
    if (!chainId) return;
    if (!bucket.some((item) => chainKeysMatch(item, chainId) || chainKeysMatch(chainId, item))) {
      bucket.push(chainId);
    }
  };
  const pushAll = (chainIdRaw: unknown) => pushUnique(all, chainIdRaw);

  const ligandCoverage = confidence.ligand_atom_coverage;
  if (Array.isArray(ligandCoverage)) {
    for (const row of ligandCoverage) {
      if (!row || typeof row !== 'object') continue;
      const chainId = (row as Record<string, unknown>).chain;
      pushUnique(ligand, chainId);
      pushAll(chainId);
    }
  }

  const chainCoverage = confidence.chain_atom_coverage;
  if (Array.isArray(chainCoverage)) {
    for (const row of chainCoverage) {
      if (!row || typeof row !== 'object') continue;
      const entry = row as Record<string, unknown>;
      const chainId = entry.chain;
      pushAll(chainId);
      const molType = String(entry.mol_type || '').trim().toLowerCase();
      if (molType.includes('nonpolymer') || molType.includes('ligand')) {
        pushUnique(ligand, chainId);
      } else if (molType.includes('protein') || molType.includes('dna') || molType.includes('rna') || molType.includes('polymer')) {
        pushUnique(polymer, chainId);
      }
    }
  }

  return { all, ligand, polymer };
}

function resolveChainFromPool(candidate: string, pool: string[]): string | null {
  const tokens = splitChainTokens(candidate);
  if (tokens.length === 0) return null;
  for (const token of tokens) {
    if (!isLikelyChainToken(token)) continue;
    const matched = pool.find((chainId) => chainKeysMatch(chainId, token) || chainKeysMatch(token, chainId));
    if (matched) return matched;
  }
  return null;
}

function normalizeProbability(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

const TASKS_PAGE_FILTERS_STORAGE_KEY = 'vbio:tasks-page-filters:v1';
const TASK_SORT_KEYS: SortKey[] = ['plddt', 'ipsae', 'iptm', 'pae', 'submitted', 'backend', 'seed', 'mode'];
const TASK_SORT_DIRECTIONS: SortDirection[] = ['asc', 'desc'];
const TASK_SUBMITTED_WINDOW_OPTIONS: SubmittedWithinDaysOption[] = ['all', '1', '7', '30', '90'];
const TASK_SEED_FILTER_OPTIONS: SeedFilterOption[] = ['all', 'with_seed', 'without_seed'];
const TASK_STRUCTURE_SEARCH_MODES: StructureSearchMode[] = ['exact', 'substructure'];
const TASK_PAGE_SIZE_OPTIONS = [8, 12, 20, 50];

function normalizeTaskWorkspaceView(value: string | null): TaskWorkspaceView {
  return value === 'api' ? 'api' : 'tasks';
}

function isSequenceLigandType(type: InputComponent['type'] | null): boolean {
  return type === 'protein' || type === 'dna' || type === 'rna';
}

function clampPlddtValue(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value >= 0 && value <= 1) return Math.max(0, Math.min(100, value * 100));
  return Math.max(0, Math.min(100, value));
}

function alignConfidenceSeriesToLength(
  values: number[] | null,
  sequenceLength: number,
  fallbackValue: number | null
): number[] | null {
  if (sequenceLength <= 0) return null;
  const series = (values || []).filter((value) => Number.isFinite(value)).map((value) => clampPlddtValue(value));
  if (series.length === 0) {
    if (fallbackValue === null || !Number.isFinite(fallbackValue)) return null;
    const normalizedFallback = clampPlddtValue(fallbackValue);
    return Array.from({ length: sequenceLength }, () => normalizedFallback);
  }

  if (series.length === sequenceLength) return series;

  if (series.length > sequenceLength) {
    const reduced: number[] = [];
    for (let i = 0; i < sequenceLength; i += 1) {
      const start = Math.floor((i * series.length) / sequenceLength);
      const end = Math.max(start + 1, Math.floor(((i + 1) * series.length) / sequenceLength));
      const chunk = series.slice(start, end);
      const avg = chunk.reduce((sum, value) => sum + value, 0) / chunk.length;
      reduced.push(clampPlddtValue(avg));
    }
    return reduced;
  }

  // Treat very short series as under-specified summaries (e.g. mean-only) rather than
  // broadcasting one/few values across all residues.
  if (series.length < Math.min(sequenceLength, 4)) {
    return null;
  }

  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * series.length) / sequenceLength);
    expanded.push(series[Math.min(series.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}

function resolveTaskSelectionContext(
  task: ProjectTask,
  workspacePreference?: WorkspacePairPreference,
  workflowHint: TaskLigandSourceWorkflow | string = 'auto'
): TaskSelectionContext {
  const workflow = normalizeTaskLigandSourceWorkflow(workflowHint);
  const preferTaskSmilesLigand = workflow === 'affinity' || workflow === 'lead_optimization';
  const taskLigandSmiles = readTaskLigandSmilesHint(task, workspacePreference?.ligandChainId || null);
  const useBindingPreference = workflow === 'prediction' || workflow === 'peptide_design' || workflow === 'auto';
  const taskProperties = task.properties && typeof task.properties === 'object' ? task.properties : null;
  const rawTarget = taskProperties && typeof taskProperties.target === 'string' ? taskProperties.target.trim() : '';
  const rawLigand = taskProperties && typeof taskProperties.ligand === 'string' ? taskProperties.ligand.trim() : '';
  const rawBinder = taskProperties && typeof taskProperties.binder === 'string' ? taskProperties.binder.trim() : '';
  const affinityData =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : null;
  const affinityTargetHint = readFirstNonEmptyStringMetric(affinityData, [
    'requested_target_chain',
    'target_chain',
    'binder_chain'
  ]);
  const confidenceData =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const confidenceTargetHint = readFirstNonEmptyStringMetric(confidenceData, [
    'requested_target_chain_id',
    'target_chain_id',
    'target_chain',
    'protein_chain_id'
  ]);
  const confidenceLigandHint = readFirstNonEmptyStringMetric(confidenceData, [
    'requested_ligand_chain_id',
    'ligand_chain_id',
    'model_ligand_chain_id',
    'binder_chain_id'
  ]);
  const confidenceChainIds = readStringListMetric(confidenceData, ['chain_ids']);
  const coverageChains = collectCoverageChainBuckets(confidenceData);
  const preferredTarget = String(workspacePreference?.targetChainId || '')
    .trim();
  const targetCandidate = rawTarget || affinityTargetHint || confidenceTargetHint || preferredTarget;
  const activeComponents = readTaskComponents(task).filter((item) => Boolean(item.sequence.trim()));
  const ligandComponentCount = activeComponents.filter((item) => item.type === 'ligand').length;

  if (activeComponents.length === 0) {
    const fallbackChainIds = Array.from(
      new Set([
        ...splitChainTokens(targetCandidate).filter((item) => isLikelyChainToken(item)),
        ...confidenceChainIds.filter((item) => isLikelyChainToken(item)),
        ...coverageChains.all.filter((item) => isLikelyChainToken(item))
      ])
    );
    const resolvedLigandChainId =
      resolveChainFromPool(confidenceLigandHint, fallbackChainIds) ||
      coverageChains.ligand[0] ||
      null;
    let resolvedTargetChainId =
      resolveChainFromPool(targetCandidate, fallbackChainIds) ||
      resolveChainFromPool(confidenceTargetHint, fallbackChainIds) ||
      coverageChains.polymer.find((chainId) => !resolvedLigandChainId || !chainKeysMatch(chainId, resolvedLigandChainId)) ||
      fallbackChainIds.find((chainId) => !resolvedLigandChainId || !chainKeysMatch(chainId, resolvedLigandChainId)) ||
      null;
    if (
      resolvedTargetChainId &&
      resolvedLigandChainId &&
      chainKeysMatch(resolvedTargetChainId, resolvedLigandChainId)
    ) {
      resolvedTargetChainId =
        coverageChains.polymer.find((chainId) => !chainKeysMatch(chainId, resolvedLigandChainId)) ||
        fallbackChainIds.find((chainId) => !chainKeysMatch(chainId, resolvedLigandChainId)) ||
        resolvedTargetChainId;
    }
    const fallbackLigandSmiles = taskLigandSmiles;
    return {
      chainIds: fallbackChainIds,
      targetChainId: resolvedTargetChainId || targetCandidate || null,
      ligandChainId: resolvedLigandChainId,
      ligandSmiles: fallbackLigandSmiles,
      ligandIsSmiles: Boolean(fallbackLigandSmiles),
      ligandComponentCount,
      ligandSequence: '',
      ligandSequenceType: null,
      ligandSequenceModifications: []
    };
  }

  const chainAssignments = assignChainIdsForComponents(activeComponents);
  const chainIdsByConfig = chainAssignments.flat().map((value) => value.toUpperCase());
  const chainIdByKey = new Map<string, string>();
  for (const chainId of [...chainIdsByConfig, ...confidenceChainIds, ...coverageChains.all]) {
    const key = normalizeChainKey(chainId);
    if (!key || chainIdByKey.has(key)) continue;
    chainIdByKey.set(key, chainId);
  }
  const chainIds = Array.from(chainIdByKey.values());

  const chainToComponent = new Map<string, InputComponent>();
  chainAssignments.forEach((chainGroup, index) => {
    chainGroup.forEach((chainId) => {
      chainToComponent.set(normalizeChainKey(chainId), activeComponents[index]);
    });
  });
  const componentOptions = activeComponents.map((component, index) => {
    const firstChain = chainAssignments[index]?.[0] || null;
    return {
      componentId: component.id,
      chainId: firstChain ? firstChain.toUpperCase() : null,
      type: component.type,
      isSmiles: component.type === 'ligand' && component.inputMethod !== 'ccd'
    };
  });
  const resolveChainFromCandidate = (candidate: string): string | null => {
    const raw = String(candidate || '').trim();
    if (!raw) return null;
    const normalized = raw.toUpperCase();
    const byOrdinalMatch = raw.match(/^comp(?:onent)?\s*#?(\d+)$/i) || raw.match(/^#?(\d+)$/);
    if (byOrdinalMatch) {
      const ordinal = Number.parseInt(byOrdinalMatch[1], 10);
      if (Number.isFinite(ordinal) && ordinal > 0) {
        const byOrdinal = componentOptions[ordinal - 1];
        if (byOrdinal?.chainId) return byOrdinal.chainId;
      }
    }
    const byKnownChain = chainIdByKey.get(normalizeChainKey(raw));
    if (byKnownChain) return byKnownChain;
    const byComponent = componentOptions.find((item) => item.componentId === raw);
    if (byComponent?.chainId) return byComponent.chainId;
    const byNormalizedChain = componentOptions.find(
      (item) => normalizeChainKey(String(item.chainId || '')) === normalizeChainKey(normalized)
    );
    if (byNormalizedChain?.chainId) return byNormalizedChain.chainId;
    for (const chainId of chainIds) {
      if (chainKeysMatch(chainId, normalized) || chainKeysMatch(normalized, chainId)) {
        return chainId;
      }
    }
    return null;
  };
  const resolveComponentFromCandidate = (
    candidate: string,
    options?: { allowAnyType?: boolean }
  ): { componentId: string; chainId: string | null; type: InputComponent['type']; isSmiles: boolean } | null => {
    const allowAnyType = Boolean(options?.allowAnyType);
    const raw = String(candidate || '').trim();
    if (!raw) return null;
    const chainId = resolveChainFromCandidate(raw);
    if (chainId) {
      const byChain = componentOptions.find((item) => item.chainId === chainId);
      if (byChain && (allowAnyType || byChain.type === 'ligand')) return byChain;
    }
    const byComponentId = componentOptions.find((item) => item.componentId === raw);
    if (byComponentId && (allowAnyType || byComponentId.type === 'ligand')) return byComponentId;
    return null;
  };
  const resolveWorkflowUploadLigandComponent = (
    workflowType: 'affinity' | 'lead_optimization'
  ): { componentId: string; chainId: string | null; type: InputComponent['type']; isSmiles: boolean } | null => {
    for (const component of activeComponents) {
      if (component.type !== 'ligand') continue;
      if (!normalizeComponentSequence('ligand', component.sequence)) continue;
      const raw = component as unknown as Record<string, unknown>;
      const role =
        workflowType === 'affinity' ? resolveAffinityUploadRole(raw) : resolveLeadOptUploadRole(raw);
      if (role !== 'ligand') continue;
      const option = componentOptions.find((item) => item.componentId === component.id) || null;
      if (option) return option;
    }
    return null;
  };
  const resolvedConfidenceTargetChain = resolveChainFromCandidate(confidenceTargetHint);
  const resolvedConfidenceLigandChain = resolveChainFromCandidate(confidenceLigandHint);
  let targetChainId =
    resolveChainFromCandidate(targetCandidate) ||
    resolvedConfidenceTargetChain ||
    componentOptions.find((item) => item.type !== 'ligand' && item.chainId)?.chainId ||
    chainAssignments[0]?.[0] ||
    chainIds[0] ||
    null;
  const predictionBindingCandidates = [
    ...splitChainTokens(rawBinder),
    ...splitChainTokens(rawLigand)
  ];
  const selectedLigandOption = (() => {
    if (useBindingPreference) {
      for (const candidate of predictionBindingCandidates) {
        const resolved = resolveComponentFromCandidate(candidate, { allowAnyType: true });
        if (resolved) return resolved;
      }
      const firstLigandComponent =
        componentOptions.find((item) => item.type === 'ligand' && item.chainId && item.isSmiles) ||
        componentOptions.find((item) => item.type === 'ligand' && item.chainId) ||
        null;
      if (firstLigandComponent) return firstLigandComponent;
      return null;
    }
    if (workflow === 'affinity') return resolveWorkflowUploadLigandComponent('affinity');
    if (workflow === 'lead_optimization') return resolveWorkflowUploadLigandComponent('lead_optimization');
    return null;
  })();
  let ligandChainId = selectedLigandOption?.chainId || resolvedConfidenceLigandChain || null;
  if (ligandChainId && !targetChainId) {
    const ligandChainKey = ligandChainId;
    targetChainId =
      chainIds.find((chainId) => !chainKeysMatch(chainId, ligandChainKey)) ||
      resolvedConfidenceTargetChain ||
      targetChainId;
  }
  if (targetChainId && !ligandChainId) {
    const targetChainKey = targetChainId;
    ligandChainId =
      resolvedConfidenceLigandChain ||
      chainIds.find((chainId) => !chainKeysMatch(chainId, targetChainKey)) ||
      null;
  }
  if (
    targetChainId &&
    ligandChainId &&
    chainKeysMatch(targetChainId, ligandChainId) &&
    (workflow === 'affinity' || workflow === 'lead_optimization')
  ) {
    const preferredDifferentTarget =
      resolvedConfidenceTargetChain && !chainKeysMatch(resolvedConfidenceTargetChain, ligandChainId)
        ? resolvedConfidenceTargetChain
        : null;
    const firstDifferentChain = chainIds.find((chainId) => !chainKeysMatch(chainId, ligandChainId)) || null;
    targetChainId = preferredDifferentTarget || firstDifferentChain || targetChainId;
  }
  const selectedLigandComponent = selectedLigandOption
    ? chainToComponent.get(normalizeChainKey(String(selectedLigandOption.chainId || ''))) ||
      activeComponents.find((item) => item.id === selectedLigandOption.componentId) ||
      null
    : null;
  let ligand =
    selectedLigandComponent?.type === 'ligand'
      ? readTaskPrimaryLigand(activeComponents, selectedLigandComponent.id || null, ligandChainId, true, workflow)
      : {
          smiles: '',
          isSmiles: false
        };
  if (!selectedLigandComponent && (workflow === 'affinity' || workflow === 'lead_optimization')) {
    ligand = readTaskPrimaryLigand(activeComponents, null, null, true, workflow);
  }
  const resolvedTaskLigandSmiles = preferTaskSmilesLigand
    ? readTaskLigandSmilesHint(task, ligandChainId || workspacePreference?.ligandChainId || null) || taskLigandSmiles
    : '';
  if (preferTaskSmilesLigand && resolvedTaskLigandSmiles) {
    ligand = {
      smiles: resolvedTaskLigandSmiles,
      isSmiles: true
    };
  }
  const ligandSequence =
    !preferTaskSmilesLigand && selectedLigandComponent && isSequenceLigandType(selectedLigandComponent.type)
      ? normalizeComponentSequence(selectedLigandComponent.type, selectedLigandComponent.sequence || '')
      : '';
  const ligandSequenceType = preferTaskSmilesLigand ? null : selectedLigandComponent?.type || null;
  const ligandSequenceModifications =
    !preferTaskSmilesLigand && selectedLigandComponent && isSequenceLigandType(selectedLigandComponent.type)
      ? selectedLigandComponent.modifications || []
      : [];

  return {
    chainIds,
    targetChainId,
    ligandChainId,
    ligandSmiles: ligand.smiles,
    ligandIsSmiles: ligand.isSmiles,
    ligandComponentCount,
    ligandSequence,
    ligandSequenceType,
    ligandSequenceModifications
  };
}

function normalizeChainKey(value: string): string {
  return value.trim().toUpperCase();
}

function chainKeysMatch(candidate: string, preferred: string): boolean {
  const normalizedCandidate = normalizeChainKey(candidate);
  const normalizedPreferred = normalizeChainKey(preferred);
  if (!normalizedCandidate || !normalizedPreferred) return false;
  if (normalizedCandidate === normalizedPreferred) return true;

  const compactCandidate = normalizedCandidate.replace(/[^A-Z0-9]/g, '');
  const compactPreferred = normalizedPreferred.replace(/[^A-Z0-9]/g, '');
  if (compactCandidate && compactPreferred && compactCandidate === compactPreferred) return true;

  const candidateTokens = normalizedCandidate.split(/[^A-Z0-9]+/).filter(Boolean);
  if (candidateTokens.includes(normalizedPreferred) || (compactPreferred && candidateTokens.includes(compactPreferred))) {
    return true;
  }

  if (compactCandidate && compactPreferred) {
    if (compactCandidate.startsWith(compactPreferred) || compactCandidate.endsWith(compactPreferred)) {
      return true;
    }
    if (compactPreferred.startsWith(compactCandidate) || compactPreferred.endsWith(compactCandidate)) {
      return true;
    }
    return compactCandidate.endsWith(compactPreferred);
  }
  return false;
}

export {
  TASKS_PAGE_FILTERS_STORAGE_KEY,
  TASK_SORT_DIRECTIONS,
  TASK_SORT_KEYS,
  TASK_SUBMITTED_WINDOW_OPTIONS,
  TASK_SEED_FILTER_OPTIONS,
  TASK_STRUCTURE_SEARCH_MODES,
  TASK_PAGE_SIZE_OPTIONS,
  normalizeTaskWorkspaceView,
  sanitizeTaskRows,
  sortProjectTasks,
  isProjectTaskRow,
  isProjectRow,
  resolveTaskSelectionContext,
  readTaskLigandRenderSmiles,
  isSequenceLigandType,
  alignConfidenceSeriesToLength,
  readObjectPath,
  readFirstFiniteMetric,
  readFirstNonEmptyStringMetric,
  readStringListMetric,
  normalizeProbability,
  normalizeChainKey,
  chainKeysMatch
};

export {
  hasTaskSummaryMetrics,
  hasTaskLigandAtomPlddts,
  readTaskLigandAtomPlddts,
  readTaskLigandResiduePlddts,
  readTaskConfidenceMetrics,
  mean
} from './taskDataConfidence';

export {
  readPeptideTaskSummary,
  readPeptideBestCandidatePreview,
  readLeadOptTaskSummary,
  hasLeadOptPredictionRuntime
} from './taskDataPeptide';

export {
  SILENT_CACHE_SYNC_WINDOW_MS,
  mapTaskState,
  inferTaskStateFromStatusPayload,
  readStatusText,
  resolveTaskBackendValue,
  compareNullableNumber,
  defaultSortDirection,
  nextSortDirection,
  parseNumberOrNull,
  normalizePlddtThreshold,
  normalizeIptmThreshold,
  normalizeSmilesForSearch,
  hasSubstructureMatchPayload,
  sanitizeFileName,
  toBase64FromBytes,
  waitForRuntimeTaskToStop
} from './taskRuntimeUiUtils';

export type { LoadTaskDataOptions } from './taskRuntimeUiUtils';
