import type { InputComponent, Project, ProjectTask } from '../../types/models';
import { assignChainIdsForComponents } from '../../utils/chainAssignments';
import { normalizeComponentSequence, normalizeInputComponents } from '../../utils/projectInputs';
import type {
  SeedFilterOption,
  SortDirection,
  SortKey,
  StructureSearchMode,
  SubmittedWithinDaysOption,
  TaskConfidenceMetrics,
  TaskMetricContext,
  TaskSelectionContext,
  TaskWorkspaceView,
  WorkspacePairPreference
} from './taskListTypes';

function normalizeTaskComponents(components: InputComponent[]): InputComponent[] {
  return normalizeInputComponents(components);
}

const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';

function isAffinityUploadRawComponent(value: unknown): boolean {
  if (!value || typeof value !== 'object') return false;
  const component = value as Record<string, unknown>;
  const id = typeof component.id === 'string' ? component.id.trim() : '';
  if (id === AFFINITY_TARGET_UPLOAD_COMPONENT_ID || id === AFFINITY_LIGAND_UPLOAD_COMPONENT_ID) {
    return true;
  }
  const uploadMeta =
    component.affinityUpload && typeof component.affinityUpload === 'object'
      ? (component.affinityUpload as Record<string, unknown>)
      : component.affinity_upload && typeof component.affinity_upload === 'object'
        ? (component.affinity_upload as Record<string, unknown>)
        : null;
  const role = typeof uploadMeta?.role === 'string' ? uploadMeta.role.trim().toLowerCase() : '';
  return role === 'target' || role === 'ligand';
}

function readTaskComponents(task: ProjectTask): InputComponent[] {
  const rawComponents = Array.isArray(task.components) ? (task.components as unknown[]) : [];
  const filteredRawComponents = rawComponents.filter((component) => !isAffinityUploadRawComponent(component));
  const components = filteredRawComponents.length > 0 ? normalizeTaskComponents(filteredRawComponents as InputComponent[]) : [];
  if (components.length > 0) return components;

  const fallback: InputComponent[] = [];
  const proteinSequence = normalizeComponentSequence('protein', task.protein_sequence || '');
  const ligandSmiles = normalizeComponentSequence('ligand', task.ligand_smiles || '');
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
  if (ligandSmiles) {
    fallback.push({
      id: 'task-ligand-1',
      type: 'ligand',
      numCopies: 1,
      sequence: ligandSmiles,
      inputMethod: 'jsme'
    });
  }
  return fallback;
}

function hasAffinityUploadComponent(task: ProjectTask): boolean {
  const rawComponents = Array.isArray(task.components) ? (task.components as unknown[]) : [];
  return rawComponents.some((component) => isAffinityUploadRawComponent(component));
}

function readTaskPrimaryLigand(
  task: ProjectTask,
  components: InputComponent[],
  preferredComponentId: string | null,
  preferredLigandChainId: string | null = null
): { smiles: string; isSmiles: boolean } {
  const affinityUploadTask = hasAffinityUploadComponent(task);
  const hasAtomLevelLigandConfidence = hasTaskLigandAtomPlddts(task, preferredLigandChainId, true);
  const directLigand = normalizeComponentSequence('ligand', task.ligand_smiles || '');
  const affinityData =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : null;
  const affinityLigand = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(affinityData, ['ligand_smiles', 'ligandSmiles', 'smiles', 'ligand.smiles'])
  );
  const affinityMapLigand = normalizeComponentSequence('ligand', readLigandSmilesFromMap(affinityData, preferredLigandChainId));
  const confidenceData =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const confidenceLigand = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(confidenceData, ['ligand_smiles', 'ligandSmiles', 'smiles', 'ligand.smiles'])
  );
  const confidenceMapLigand = normalizeComponentSequence('ligand', readLigandSmilesFromMap(confidenceData, preferredLigandChainId));

  if (affinityUploadTask) {
    if (confidenceLigand) return { smiles: confidenceLigand, isSmiles: true };
    if (confidenceMapLigand) return { smiles: confidenceMapLigand, isSmiles: true };
    if (affinityLigand) return { smiles: affinityLigand, isSmiles: true };
    if (affinityMapLigand) return { smiles: affinityMapLigand, isSmiles: true };
    if (directLigand) return { smiles: directLigand, isSmiles: true };
  }

  // When atom-level confidence exists, always bind 2D rendering to the aligned
  // confidence/affinity SMILES first, otherwise atom colors and ordering can drift.
  if (hasAtomLevelLigandConfidence) {
    if (confidenceMapLigand) {
      return { smiles: confidenceMapLigand, isSmiles: true };
    }
    if (confidenceLigand) {
      return { smiles: confidenceLigand, isSmiles: true };
    }
    if (affinityMapLigand) {
      return { smiles: affinityMapLigand, isSmiles: true };
    }
    if (affinityLigand) {
      return { smiles: affinityLigand, isSmiles: true };
    }
    if (directLigand) {
      return { smiles: directLigand, isSmiles: true };
    }
  }

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

  const ligand = components.find((item) => item.type === 'ligand' && normalizeComponentSequence('ligand', item.sequence));
  if (ligand) {
    const value = normalizeComponentSequence('ligand', ligand.sequence);
    return {
      smiles: value,
      isSmiles: ligand.inputMethod !== 'ccd'
    };
  }

  if (directLigand) {
    return { smiles: directLigand, isSmiles: true };
  }

  if (affinityLigand) {
    return { smiles: affinityLigand, isSmiles: true };
  }

  if (affinityMapLigand) {
    return { smiles: affinityMapLigand, isSmiles: true };
  }

  if (confidenceLigand) {
    return { smiles: confidenceLigand, isSmiles: true };
  }

  if (confidenceMapLigand) {
    return { smiles: confidenceMapLigand, isSmiles: true };
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

function readLigandSmilesFromMap(data: Record<string, unknown> | null, preferredLigandChainId: string | null = null): string {
  if (!data) return '';
  const mapCandidates: unknown[] = [
    readObjectPath(data, 'ligand_smiles_map'),
    readObjectPath(data, 'ligand.smiles_map'),
    readObjectPath(data, 'ligand.smilesMap')
  ];
  const preferredChain = normalizeChainKey(String(preferredLigandChainId || ''));
  const modelChain = normalizeChainKey(readFirstNonEmptyStringMetric(data, ['model_ligand_chain_id']));
  const requestedChain = normalizeChainKey(readFirstNonEmptyStringMetric(data, ['requested_ligand_chain_id', 'requested_ligand_chain']));
  const preferredCandidates = [preferredChain, modelChain, requestedChain].filter(Boolean);
  for (const mapValue of mapCandidates) {
    if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) continue;
    const entries = Object.entries(mapValue as Record<string, unknown>)
      .map(([key, value]) => {
        if (typeof value !== 'string') return null;
        const next = value.trim();
        if (!next) return null;
        return { key: normalizeChainKey(key), value: next };
      })
      .filter((entry): entry is { key: string; value: string } => entry !== null);
    if (entries.length === 0) continue;

    for (const preferred of preferredCandidates) {
      const matched = entries.find((entry) => {
        const keyChain = entry.key.includes(':') ? entry.key.slice(0, entry.key.indexOf(':')).trim() : entry.key;
        return chainKeysMatch(keyChain, preferred) || chainKeysMatch(preferred, keyChain);
      });
      if (matched?.value) return matched.value;
    }

    if (entries.length === 1) {
      return entries[0].value;
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

function splitChainTokens(value: string): string[] {
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function normalizeProbability(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

const TASKS_PAGE_FILTERS_STORAGE_KEY = 'vbio:tasks-page-filters:v1';
const TASK_SORT_KEYS: SortKey[] = ['plddt', 'iptm', 'pae', 'submitted', 'backend', 'seed', 'duration'];
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

  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * series.length) / sequenceLength);
    expanded.push(series[Math.min(series.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

function readPairIptmForChains(
  confidence: Record<string, unknown>,
  chainA: string | null,
  chainB: string | null,
  fallbackChainIds: string[]
): number | null {
  if (!chainA || !chainB) return null;
  if (chainA === chainB) return null;

  const pairMap = readObjectPath(confidence, 'pair_chains_iptm');
  if (pairMap && typeof pairMap === 'object' && !Array.isArray(pairMap)) {
    const byChain = pairMap as Record<string, unknown>;
    const rowA = byChain[chainA];
    const rowB = byChain[chainB];
    const v1 =
      rowA && typeof rowA === 'object' && !Array.isArray(rowA)
        ? normalizeProbability(toFiniteNumber((rowA as Record<string, unknown>)[chainB]))
        : null;
    const v2 =
      rowB && typeof rowB === 'object' && !Array.isArray(rowB)
        ? normalizeProbability(toFiniteNumber((rowB as Record<string, unknown>)[chainA]))
        : null;
    if (v1 !== null || v2 !== null) {
      return Math.max(v1 ?? Number.NEGATIVE_INFINITY, v2 ?? Number.NEGATIVE_INFINITY);
    }
  }

  const pairMatrixRaw =
    readObjectPath(confidence, 'chain_pair_iptm') ??
    readObjectPath(confidence, 'chain_pair_iptm_global');
  if (!Array.isArray(pairMatrixRaw)) return null;
  const pairMatrix = pairMatrixRaw;
  const chainIdsRaw = readObjectPath(confidence, 'chain_ids');
  const chainIds =
    Array.isArray(chainIdsRaw) && chainIdsRaw.every((item) => typeof item === 'string')
      ? (chainIdsRaw as string[])
      : fallbackChainIds;
  const i = chainIds.findIndex((item) => item === chainA);
  const j = chainIds.findIndex((item) => item === chainB);
  if (i < 0 || j < 0) return null;
  const rowI = pairMatrix[i];
  const rowJ = pairMatrix[j];
  const m1 = Array.isArray(rowI) ? normalizeProbability(toFiniteNumber(rowI[j])) : null;
  const m2 = Array.isArray(rowJ) ? normalizeProbability(toFiniteNumber(rowJ[i])) : null;
  if (m1 !== null || m2 !== null) {
    return Math.max(m1 ?? Number.NEGATIVE_INFINITY, m2 ?? Number.NEGATIVE_INFINITY);
  }
  return null;
}

function readChainMeanPlddtForChain(confidence: Record<string, unknown>, chainId: string | null): number | null {
  if (!chainId) return null;
  const map = readObjectPath(confidence, 'chain_mean_plddt');
  if (!map || typeof map !== 'object' || Array.isArray(map)) return null;
  const value = toFiniteNumber((map as Record<string, unknown>)[chainId]);
  if (value === null) return null;
  return value >= 0 && value <= 1 ? value * 100 : value;
}

function resolveTaskSelectionContext(task: ProjectTask, workspacePreference?: WorkspacePairPreference): TaskSelectionContext {
  const taskProperties = task.properties && typeof task.properties === 'object' ? task.properties : null;
  const rawTarget = taskProperties && typeof taskProperties.target === 'string' ? taskProperties.target.trim() : '';
  const rawLigand = taskProperties && typeof taskProperties.ligand === 'string' ? taskProperties.ligand.trim() : '';
  const rawBinder = taskProperties && typeof taskProperties.binder === 'string' ? taskProperties.binder.trim() : '';
  const affinityData =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : null;
  const confidenceData =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const affinityTargetHint = readFirstNonEmptyStringMetric(affinityData, [
    'requested_target_chain',
    'target_chain',
    'binder_chain'
  ]);
  const affinityLigandHint = readFirstNonEmptyStringMetric(affinityData, [
    'requested_ligand_chain',
    'ligand_chain'
  ]);
  const affinityModelLigandHint = readFirstNonEmptyStringMetric(affinityData, [
    'model_ligand_chain_id'
  ]);
  const confidenceModelLigandHint = readFirstNonEmptyStringMetric(confidenceData, [
    'model_ligand_chain_id'
  ]);
  const confidenceChainIds = readStringListMetric(confidenceData, ['chain_ids']);
  const preferredTarget = String(workspacePreference?.targetChainId || '')
    .trim();
  const preferredLigand = String(workspacePreference?.ligandChainId || '')
    .trim();
  // Row-level task setting should win over workspace preference.
  // For prediction workflows, "binding ligand" is represented by `binder`.
  const targetCandidate = rawTarget || affinityTargetHint || preferredTarget;
  const ligandCandidate = rawBinder || rawLigand || affinityLigandHint || preferredLigand;
  const activeComponents = readTaskComponents(task).filter((item) => Boolean(item.sequence.trim()));
  const ligandComponentCount = activeComponents.filter((item) => item.type === 'ligand').length;

  if (activeComponents.length === 0) {
    const ligand = readTaskPrimaryLigand(task, [], null, ligandCandidate || null);
    const fallbackChainIds = Array.from(
      new Set([
        ...splitChainTokens(targetCandidate),
        ...splitChainTokens(ligandCandidate),
        ...confidenceChainIds
      ])
    );
    return {
      chainIds: fallbackChainIds,
      targetChainId: targetCandidate || null,
      ligandChainId: ligandCandidate || null,
      ligandSmiles: ligand.smiles,
      ligandIsSmiles: ligand.isSmiles,
      ligandComponentCount: ligand.smiles ? 1 : 0,
      ligandSequence: '',
      ligandSequenceType: null
    };
  }

  const chainAssignments = assignChainIdsForComponents(activeComponents);
  const chainIdsByConfig = chainAssignments.flat().map((value) => value.toUpperCase());
  const chainIdByKey = new Map<string, string>();
  for (const chainId of [...chainIdsByConfig, ...confidenceChainIds]) {
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
  const confidenceLigandHintCandidates: string[] = [];
  const confidenceLigandId = confidenceData && typeof confidenceData.ligand_chain_id === 'string'
    ? confidenceData.ligand_chain_id.trim()
    : '';
  if (confidenceLigandId) {
    confidenceLigandHintCandidates.push(confidenceLigandId);
  }
  const confidenceLigandIds = confidenceData?.ligand_chain_ids;
  if (Array.isArray(confidenceLigandIds)) {
    for (const candidate of confidenceLigandIds) {
      if (typeof candidate !== 'string' || !candidate.trim()) continue;
      confidenceLigandHintCandidates.push(candidate.trim());
    }
  }
  const confidenceByChain = confidenceData?.ligand_atom_plddts_by_chain;
  if (confidenceByChain && typeof confidenceByChain === 'object' && !Array.isArray(confidenceByChain)) {
    for (const key of Object.keys(confidenceByChain as Record<string, unknown>)) {
      if (!key.trim()) continue;
      confidenceLigandHintCandidates.push(key.trim());
    }
  }
  const ligandHintKeys = new Set(
    [
      ...confidenceLigandHintCandidates,
      ...splitChainTokens(rawBinder),
      ...splitChainTokens(rawLigand),
      ...splitChainTokens(affinityModelLigandHint),
      ...splitChainTokens(confidenceModelLigandHint),
      ...splitChainTokens(affinityLigandHint),
      ...splitChainTokens(preferredLigand),
      ...splitChainTokens(ligandCandidate)
    ]
      .map((item) => normalizeChainKey(item))
      .filter(Boolean)
  );
  const fallbackTargetChainId =
    componentOptions.find((item) => item.type !== 'ligand' && item.chainId)?.chainId || chainAssignments[0]?.[0] || null;
  const normalizedFallbackTarget = fallbackTargetChainId ? resolveChainFromCandidate(fallbackTargetChainId) : null;
  const inferredFallbackTarget =
    chainIds.find((chainId) => !ligandHintKeys.has(normalizeChainKey(chainId))) ||
    chainIds[0] ||
    null;
  const targetChainId = resolveChainFromCandidate(targetCandidate) || normalizedFallbackTarget || inferredFallbackTarget;
  const targetComponentId = targetChainId ? chainToComponent.get(normalizeChainKey(targetChainId))?.id || null : null;
  const candidateLigandOptions = componentOptions.filter((item) => {
    if (!item.chainId) return false;
    if (item.chainId === targetChainId) return false;
    if (targetComponentId && item.componentId === targetComponentId) return false;
    return true;
  });
  const defaultLigandOption =
    candidateLigandOptions.find((item) => item.isSmiles) ||
    componentOptions.find((item) => item.isSmiles && item.chainId) ||
    candidateLigandOptions[0] ||
    componentOptions.find((item) => Boolean(item.chainId)) ||
    null;
  const confidencePreferredLigandChainId = confidenceLigandHintCandidates
    .map((candidate) => resolveChainFromCandidate(candidate))
    .find((candidate): candidate is string => Boolean(candidate));
  const preferredLigandChainId =
    resolveChainFromCandidate(rawBinder) ||
    resolveChainFromCandidate(rawLigand) ||
    resolveChainFromCandidate(affinityModelLigandHint) ||
    resolveChainFromCandidate(confidenceModelLigandHint) ||
    resolveChainFromCandidate(affinityLigandHint) ||
    resolveChainFromCandidate(preferredLigand) ||
    resolveChainFromCandidate(ligandCandidate) ||
    confidencePreferredLigandChainId ||
    null;
  const preferredLigandComponentId = preferredLigandChainId
    ? chainToComponent.get(normalizeChainKey(preferredLigandChainId))?.id || null
    : null;
  const inferredFallbackLigand =
    chainIds.find((chainId) => chainId !== targetChainId && ligandHintKeys.has(normalizeChainKey(chainId))) ||
    chainIds.find((chainId) => chainId !== targetChainId) ||
    null;
  const ligandChainId =
    preferredLigandChainId &&
    preferredLigandChainId !== targetChainId &&
    preferredLigandComponentId &&
    preferredLigandComponentId !== targetComponentId
      ? preferredLigandChainId
      : defaultLigandOption?.chainId || inferredFallbackLigand;
  const selectedLigandComponent = ligandChainId ? chainToComponent.get(normalizeChainKey(ligandChainId)) || null : null;
  const ligand =
    selectedLigandComponent?.type === 'ligand'
      ? readTaskPrimaryLigand(task, activeComponents, selectedLigandComponent.id || null, ligandChainId)
      : selectedLigandComponent
        ? {
            smiles: '',
            isSmiles: false
          }
        : readTaskPrimaryLigand(task, activeComponents, null, ligandChainId);
  const ligandSequence =
    selectedLigandComponent && isSequenceLigandType(selectedLigandComponent.type)
      ? normalizeComponentSequence(selectedLigandComponent.type, selectedLigandComponent.sequence || '')
      : '';
  const ligandSequenceType = selectedLigandComponent?.type || null;

  return {
    chainIds,
    targetChainId,
    ligandChainId,
    ligandSmiles: ligand.smiles,
    ligandIsSmiles: ligand.isSmiles,
    ligandComponentCount,
    ligandSequence,
    ligandSequenceType
  };
}

function readTaskConfidenceMetrics(task: ProjectTask, context?: TaskMetricContext): TaskConfidenceMetrics {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const selectedLigandPlddt = context
    ? readChainMeanPlddtForChain(confidence, context.ligandChainId)
    : null;
  const selectedPairIptm = context
    ? readPairIptmForChains(confidence, context.targetChainId, context.ligandChainId, context.chainIds)
    : null;
  const plddtRaw = readFirstFiniteMetric(confidence, [
    'ligand_plddt',
    'ligand_mean_plddt',
    'complex_iplddt',
    'complex_plddt_protein',
    'complex_plddt',
    'plddt'
  ]);
  const iptmRaw = selectedPairIptm ?? readFirstFiniteMetric(confidence, ['iptm', 'ligand_iptm', 'protein_iptm']);
  const paeRaw = readFirstFiniteMetric(confidence, ['complex_pde', 'complex_pae', 'gpde', 'pae']);
  const mergedPlddt = selectedLigandPlddt ?? plddtRaw;
  return {
    plddt: mergedPlddt === null ? null : mergedPlddt <= 1 ? mergedPlddt * 100 : mergedPlddt,
    iptm: normalizeProbability(iptmRaw),
    pae: paeRaw
  };
}

interface LeadOptTaskSummary {
  stage: string;
  summary: string;
  transformCount: number | null;
  candidateCount: number | null;
  bucketCount: number | null;
  predictionTotal: number | null;
  predictionQueued: number | null;
  predictionRunning: number | null;
  predictionSuccess: number | null;
  predictionFailure: number | null;
  selectedFragmentIds: string[];
  selectedAtomIndices: number[];
  selectedFragmentQuery: string;
}

function readFiniteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === 'string' ? item.trim() : ''))
    .filter(Boolean);
}

function readLeadOptVariableItems(leadOptMmp: Record<string, unknown>): Array<Record<string, unknown>> {
  const selection = readObjectPath(leadOptMmp, 'selection');
  if (selection && typeof selection === 'object' && !Array.isArray(selection)) {
    const selectedItems = readObjectPath(selection as Record<string, unknown>, 'variable_items');
    if (Array.isArray(selectedItems)) {
      return selectedItems
        .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
        .map((item) => item as Record<string, unknown>);
    }
  }
  const queryPayload = readObjectPath(leadOptMmp, 'query_payload');
  if (queryPayload && typeof queryPayload === 'object' && !Array.isArray(queryPayload)) {
    const variableSpec = readObjectPath(queryPayload as Record<string, unknown>, 'variable_spec');
    if (variableSpec && typeof variableSpec === 'object' && !Array.isArray(variableSpec)) {
      const items = readObjectPath(variableSpec as Record<string, unknown>, 'items');
      if (Array.isArray(items)) {
        return items
          .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
          .map((item) => item as Record<string, unknown>);
      }
    }
  }
  return [];
}

function normalizeLeadOptTaskStage(value: string): string {
  const token = value.trim().toLowerCase();
  if (!token) return 'unknown';
  return token;
}

function readLeadOptTaskSummary(task: ProjectTask): LeadOptTaskSummary | null {
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const leadOptMmp =
    confidence?.lead_opt_mmp && typeof confidence.lead_opt_mmp === 'object' && !Array.isArray(confidence.lead_opt_mmp)
      ? (confidence.lead_opt_mmp as Record<string, unknown>)
      : null;
  if (!leadOptMmp) return null;

  const stage = normalizeLeadOptTaskStage(
    String(
      leadOptMmp.prediction_stage ||
      leadOptMmp.stage ||
      leadOptMmp.prediction_state ||
      task.task_state ||
      ''
    )
  );
  const predictionSummary =
    leadOptMmp.prediction_summary && typeof leadOptMmp.prediction_summary === 'object' && !Array.isArray(leadOptMmp.prediction_summary)
      ? (leadOptMmp.prediction_summary as Record<string, unknown>)
      : {};
  const predictionMap =
    leadOptMmp.prediction_by_smiles && typeof leadOptMmp.prediction_by_smiles === 'object' && !Array.isArray(leadOptMmp.prediction_by_smiles)
      ? (leadOptMmp.prediction_by_smiles as Record<string, unknown>)
      : {};
  const bucketCountFromSummary = readFiniteNumber(predictionSummary.total);
  const bucketCount = bucketCountFromSummary !== null ? Math.max(0, Math.floor(bucketCountFromSummary)) : Object.keys(predictionMap).length;
  const predictionQueued = (() => {
    const value = readFiniteNumber(predictionSummary.queued);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionRunning = (() => {
    const value = readFiniteNumber(predictionSummary.running);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionSuccess = (() => {
    const value = readFiniteNumber(predictionSummary.success);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionFailure = (() => {
    const value = readFiniteNumber(predictionSummary.failure);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionTotal = (() => {
    const value = readFiniteNumber(predictionSummary.total);
    if (value !== null) return Math.max(0, Math.floor(value));
    if (Object.keys(predictionMap).length > 0) return Object.keys(predictionMap).length;
    if (
      predictionQueued !== null ||
      predictionRunning !== null ||
      predictionSuccess !== null ||
      predictionFailure !== null
    ) {
      return (predictionQueued || 0) + (predictionRunning || 0) + (predictionSuccess || 0) + (predictionFailure || 0);
    }
    return null;
  })();
  const transformCount = readFiniteNumber(leadOptMmp.transform_count);
  const candidateCount = readFiniteNumber(leadOptMmp.candidate_count);
  const selectedFragmentIds = (() => {
    const selectionIds = readStringArray(readObjectPath(leadOptMmp, 'selection.selected_fragment_ids'));
    if (selectionIds.length > 0) return Array.from(new Set(selectionIds));
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    return Array.from(
      new Set(
        variableItems
          .map((item) => String(item.fragment_id || '').trim())
          .filter(Boolean)
      )
    );
  })();
  const selectedAtomIndices = (() => {
    const selectionAtomsRaw = readObjectPath(leadOptMmp, 'selection.selected_fragment_atom_indices');
    if (Array.isArray(selectionAtomsRaw)) {
      return Array.from(
        new Set(
          selectionAtomsRaw
            .map((value) => Number(value))
            .filter((value) => Number.isFinite(value) && value >= 0)
            .map((value) => Math.floor(value))
        )
      );
    }
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    return Array.from(
      new Set(
        variableItems.flatMap((item) => {
          if (!Array.isArray(item.atom_indices)) return [];
          return item.atom_indices
            .map((value) => Number(value))
            .filter((value) => Number.isFinite(value) && value >= 0)
            .map((value) => Math.floor(value));
        })
      )
    );
  })();
  const selectedFragmentQuery = (() => {
    const selectionQueries = readStringArray(readObjectPath(leadOptMmp, 'selection.variable_queries'));
    if (selectionQueries.length > 0) return selectionQueries[0];
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    for (const item of variableItems) {
      const query = String(item.query || '').trim();
      if (query) return query;
    }
    return '';
  })();

  const stageLabel = (() => {
    if (stage === 'prediction_running' || stage === 'running') return 'Running';
    if (stage === 'prediction_completed' || stage === 'completed') return 'Completed';
    if (stage === 'prediction_failed' || stage === 'failed') return 'Failed';
    if (stage === 'prediction_queued' || stage === 'queued') return 'Queued';
    if (stage === 'idle') return 'Idle';
    return stage ? stage.replace(/_/g, ' ') : 'Unknown';
  })();

  const summary = (() => {
    const parts: string[] = [stageLabel];
    if (transformCount !== null) parts.push(`${Math.max(0, Math.floor(transformCount))} transforms`);
    if (candidateCount !== null) parts.push(`${Math.max(0, Math.floor(candidateCount))} candidates`);
    if (bucketCount > 0) parts.push(`${bucketCount} buckets`);
    if (predictionTotal !== null && predictionTotal > 0) {
      parts.push(
        `q${predictionQueued || 0}/r${predictionRunning || 0}/s${predictionSuccess || 0}/f${predictionFailure || 0}`
      );
    }
    return parts.join(' · ');
  })();

  return {
    stage,
    summary,
    transformCount: transformCount === null ? null : Math.max(0, Math.floor(transformCount)),
    candidateCount: candidateCount === null ? null : Math.max(0, Math.floor(candidateCount)),
    bucketCount,
    predictionTotal,
    predictionQueued,
    predictionRunning,
    predictionSuccess,
    predictionFailure,
    selectedFragmentIds,
    selectedAtomIndices,
    selectedFragmentQuery
  };
}

function hasLeadOptPredictionRuntime(task: ProjectTask): boolean {
  const summary = readLeadOptTaskSummary(task);
  if (!summary) return false;
  const stage = summary.stage;
  if (stage === 'prediction_running' || stage === 'running' || stage === 'prediction_queued' || stage === 'queued') {
    return true;
  }
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const leadOptMmp =
    confidence?.lead_opt_mmp && typeof confidence.lead_opt_mmp === 'object' && !Array.isArray(confidence.lead_opt_mmp)
      ? (confidence.lead_opt_mmp as Record<string, unknown>)
      : null;
  if (!leadOptMmp) return false;
  const predictionSummary =
    leadOptMmp.prediction_summary && typeof leadOptMmp.prediction_summary === 'object' && !Array.isArray(leadOptMmp.prediction_summary)
      ? (leadOptMmp.prediction_summary as Record<string, unknown>)
      : {};
  const queued = readFiniteNumber(predictionSummary.queued) || 0;
  const running = readFiniteNumber(predictionSummary.running) || 0;
  return queued + running > 0;
}

function toFiniteNumberArray(value: unknown): number[] {
  const normalizeItems = (items: unknown[]): number[] =>
    items
      .map((item) => {
        if (typeof item === 'number') return Number.isFinite(item) ? item : null;
        if (typeof item === 'string') {
          const parsed = Number(item.trim());
          return Number.isFinite(parsed) ? parsed : null;
        }
        return null;
      })
      .filter((item): item is number => item !== null);

  if (Array.isArray(value)) {
    return normalizeItems(value);
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const nestedCandidates: unknown[] = [
      obj.values,
      obj.value,
      obj.plddt,
      obj.plddts,
      obj.residue_plddt,
      obj.residue_plddts,
      obj.token_plddt,
      obj.token_plddts,
      obj.scores
    ];
    for (const candidate of nestedCandidates) {
      if (Array.isArray(candidate)) {
        const parsed = normalizeItems(candidate);
        if (parsed.length > 0) return parsed;
      }
    }
  }
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value) as unknown;
      if (Array.isArray(parsed)) {
        return normalizeItems(parsed);
      }
      if (parsed && typeof parsed === 'object') {
        return toFiniteNumberArray(parsed);
      }
    } catch {
      return [];
    }
  }
  return [];
}

function normalizeAtomPlddts(values: number[]): number[] {
  const normalized = values
    .filter((value) => Number.isFinite(value))
    .map((value) => {
      if (value >= 0 && value <= 1) return value * 100;
      return value;
    })
    .map((value) => Math.max(0, Math.min(100, value)));
  if (normalized.length === 0) return [];
  return normalized;
}

function mean(values: number[] | null): number | null {
  if (!values || values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
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

function readTaskLigandAtomPlddtsFromChainMap(
  value: unknown,
  preferredChainKeys: Set<string>,
  allowFallbackToAnyChain: boolean
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const parsedEntries = Object.entries(value as Record<string, unknown>)
    .map(([key, chainValues]) => {
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length === 0) return null;
      return {
        chainId: normalizeChainKey(key),
        values: parsed
      };
    })
    .filter((entry): entry is { chainId: string; values: number[] } => entry !== null);
  if (parsedEntries.length === 0) return null;

  const pickLongest = (values: number[][]): number[] | null => {
    if (values.length === 0) return null;
    return values.reduce((best, current) => (current.length > best.length ? current : best), values[0]);
  };

  if (preferredChainKeys.size > 0) {
    const matched = parsedEntries
      .filter((entry) =>
        Array.from(preferredChainKeys).some(
          (preferred) => chainKeysMatch(entry.chainId, preferred) || chainKeysMatch(preferred, entry.chainId)
        )
      )
      .map((entry) => entry.values);
    const selected = pickLongest(matched);
    if (selected) return selected;
  }

  if (allowFallbackToAnyChain) {
    return pickLongest(parsedEntries.map((entry) => entry.values));
  }
  return null;
}

function readTaskResiduePlddtsFromChainMap(
  value: unknown,
  preferredChainKeys: Set<string>
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const map = value as Record<string, unknown>;
  const entries = Object.entries(map);
  if (entries.length === 0) return null;

  if (preferredChainKeys.size > 0) {
    for (const [key, chainValues] of entries) {
      const matched = Array.from(preferredChainKeys).some((preferred) => chainKeysMatch(key, preferred));
      if (!matched) continue;
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length > 0) return parsed;
    }
  }
  return null;
}

function collectTaskPreferredLigandChainKeys(
  confidence: Record<string, unknown>,
  preferredLigandChainId: string | null
): Set<string> {
  const keys = new Set<string>();
  if (preferredLigandChainId) {
    keys.add(normalizeChainKey(preferredLigandChainId));
  }
  const modelLigandChain = readFirstNonEmptyStringMetric(confidence, ['model_ligand_chain_id']);
  if (modelLigandChain) {
    keys.add(normalizeChainKey(modelLigandChain));
  }
  const requestedLigandChain = readFirstNonEmptyStringMetric(confidence, ['requested_ligand_chain_id', 'ligand_chain_id']);
  if (requestedLigandChain) {
    keys.add(normalizeChainKey(requestedLigandChain));
  }
  return keys;
}

function readTaskTokenPlddtsForChain(
  confidence: Record<string, unknown>,
  preferredChainKeys: Set<string>
): number[] | null {
  const tokenPlddtCandidates: unknown[] = [
    confidence.token_plddts,
    confidence.token_plddt,
    readObjectPath(confidence, 'token_plddts'),
    readObjectPath(confidence, 'token_plddt'),
    readObjectPath(confidence, 'plddt_by_token')
  ];
  const tokenChainCandidates: unknown[] = [
    confidence.token_chain_ids,
    confidence.token_chain_id,
    readObjectPath(confidence, 'token_chain_ids'),
    readObjectPath(confidence, 'token_chain_id'),
    readObjectPath(confidence, 'chain_ids_by_token')
  ];

  if (preferredChainKeys.size === 0) return null;
  for (const plddtCandidate of tokenPlddtCandidates) {
    const tokenPlddts = normalizeAtomPlddts(toFiniteNumberArray(plddtCandidate));
    if (tokenPlddts.length === 0) continue;

    for (const chainCandidate of tokenChainCandidates) {
      if (!Array.isArray(chainCandidate)) continue;
      if (chainCandidate.length !== tokenPlddts.length) continue;
      const tokenChains = chainCandidate.map((value) => normalizeChainKey(String(value || '')));
      if (tokenChains.some((value) => !value)) continue;

      const byChain = tokenPlddts.filter((_, index) => {
        return Array.from(preferredChainKeys).some((preferred) => chainKeysMatch(tokenChains[index], preferred));
      });
      if (byChain.length > 0) return byChain;
    }
  }
  return null;
}

function readTaskLigandResiduePlddts(task: ProjectTask, preferredLigandChainId: string | null): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const preferredChainKeys = collectTaskPreferredLigandChainKeys(confidence, preferredLigandChainId);
  if (preferredChainKeys.size === 0) return null;
  const chainMapCandidates: unknown[] = [
    confidence.residue_plddt_by_chain,
    confidence.chain_residue_plddt,
    confidence.chain_plddt,
    confidence.chain_plddts,
    confidence.plddt_by_chain,
    readObjectPath(confidence, 'residue_plddt_by_chain'),
    readObjectPath(confidence, 'chain_residue_plddt'),
    readObjectPath(confidence, 'chain_plddt'),
    readObjectPath(confidence, 'chain_plddts'),
    readObjectPath(confidence, 'plddt.by_chain')
  ];
  for (const candidate of chainMapCandidates) {
    const parsed = readTaskResiduePlddtsFromChainMap(candidate, preferredChainKeys);
    if (parsed && parsed.length > 0) return parsed;
  }

  const tokenPlddts = readTaskTokenPlddtsForChain(confidence, preferredChainKeys);
  if (tokenPlddts && tokenPlddts.length > 0) return tokenPlddts;

  return null;
}

function readTaskLigandAtomPlddts(
  task: ProjectTask,
  preferredLigandChainId: string | null = null,
  allowFlatFallback = true
): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const preferredChainKeys = collectTaskPreferredLigandChainKeys(confidence, preferredLigandChainId);
  const byChainCandidates: unknown[] = [
    confidence.ligand_atom_plddts_by_chain,
    readObjectPath(confidence, 'ligand.atom_plddts_by_chain'),
    readObjectPath(confidence, 'ligand_confidence.atom_plddts_by_chain')
  ];
  let hasChainMap = false;
  for (const candidate of byChainCandidates) {
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
      hasChainMap = true;
    }
    const parsed = readTaskLigandAtomPlddtsFromChainMap(
      candidate,
      preferredChainKeys,
      preferredChainKeys.size === 0
    );
    if (parsed && parsed.length > 0) {
      return parsed;
    }
  }
  if (preferredChainKeys.size > 0 && hasChainMap && !allowFlatFallback) return null;
  if (!allowFlatFallback) return null;

  const candidates: unknown[] = [
    confidence.ligand_atom_plddts,
    confidence.ligand_atom_plddt,
    readObjectPath(confidence, 'ligand.atom_plddts'),
    readObjectPath(confidence, 'ligand.atom_plddt'),
    readObjectPath(confidence, 'ligand_confidence.atom_plddts')
  ];
  for (const candidate of candidates) {
    const parsed = normalizeAtomPlddts(toFiniteNumberArray(candidate));
    if (parsed.length > 0) {
      return parsed;
    }
  }
  return null;
}

function hasTaskLigandAtomPlddts(
  task: ProjectTask,
  preferredLigandChainId: string | null = null,
  allowFlatFallback = true
): boolean {
  return Boolean(readTaskLigandAtomPlddts(task, preferredLigandChainId, allowFlatFallback)?.length);
}

function hasTaskSummaryMetrics(task: ProjectTask): boolean {
  const context = resolveTaskSelectionContext(task);
  const metrics = readTaskConfidenceMetrics(task, context);
  return metrics.plddt !== null || metrics.iptm !== null || metrics.pae !== null;
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
  hasTaskSummaryMetrics,
  hasTaskLigandAtomPlddts,
  readTaskLigandAtomPlddts,
  readTaskLigandResiduePlddts,
  readTaskConfidenceMetrics,
  readLeadOptTaskSummary,
  hasLeadOptPredictionRuntime,
  isSequenceLigandType,
  alignConfidenceSeriesToLength,
  mean
};

export {
  SILENT_CACHE_SYNC_WINDOW_MS,
  mapTaskState,
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
