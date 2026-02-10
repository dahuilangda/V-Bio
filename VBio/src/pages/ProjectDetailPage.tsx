import type { CSSProperties, FormEvent, KeyboardEvent, PointerEvent } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import {
  ArrowLeft,
  Clock3,
  ChevronDown,
  ChevronRight,
  Download,
  Dna,
  Eye,
  FlaskConical,
  LoaderCircle,
  Play,
  Plus,
  RefreshCcw,
  Save,
  SlidersHorizontal,
  Target
} from 'lucide-react';
import type {
  InputComponent,
  PredictionConstraint,
  PredictionConstraintType,
  Project,
  ProjectTask,
  ProjectInputConfig,
  ProteinTemplateUpload,
  TaskState
} from '../types/models';
import { useAuth } from '../hooks/useAuth';
import { downloadResultBlob, downloadResultFile, getTaskStatus, parseResultBundle, submitPrediction } from '../api/backendApi';
import { getProjectById, insertProjectTask, listProjectTasks, updateProject, updateProjectTask } from '../api/supabaseLite';
import { formatDuration } from '../utils/date';
import type { MolstarResidueHighlight, MolstarResiduePick } from '../components/project/MolstarViewer';
import { MolstarViewer } from '../components/project/MolstarViewer';
import { MetricsPanel } from '../components/project/MetricsPanel';
import { ComponentInputEditor } from '../components/project/ComponentInputEditor';
import type { ConstraintResiduePick } from '../components/project/ConstraintEditor';
import { ConstraintEditor } from '../components/project/ConstraintEditor';
import { Ligand2DPreview } from '../components/project/Ligand2DPreview';
import { LigandPropertyGrid } from '../components/project/LigandPropertyGrid';
import { assignChainIdsForComponents, buildChainInfos } from '../utils/chainAssignments';
import { extractProteinChainResidueIndexMap } from '../utils/structureParser';
import {
  buildDefaultInputConfig,
  componentTypeLabel,
  createInputComponent,
  extractPrimaryProteinAndLigand,
  loadProjectInputConfig,
  loadProjectUiState,
  normalizeComponentSequence,
  saveProjectUiState,
  saveProjectInputConfig
} from '../utils/projectInputs';
import { validateComponents } from '../utils/inputValidation';
import { getWorkflowDefinition } from '../utils/workflows';

interface DraftFields {
  name: string;
  summary: string;
  backend: string;
  use_msa: boolean;
  color_mode: string;
  inputConfig: ProjectInputConfig;
}

type WorkspaceTab = 'results' | 'basics' | 'components' | 'constraints';
type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';
type ResultsGridStyle = CSSProperties & { '--results-main-width'?: string };
type InputsGridStyle = CSSProperties & { '--inputs-main-width'?: string };
type ConstraintsGridStyle = CSSProperties & { '--constraints-main-width'?: string };

const RESULTS_MAIN_WIDTH_STORAGE_KEY = 'vbio:results-main-width';
const DEFAULT_RESULTS_MAIN_WIDTH = 67;
const MIN_RESULTS_MAIN_WIDTH = 56;
const MAX_RESULTS_MAIN_WIDTH = 80;
const COMPONENTS_MAIN_WIDTH_STORAGE_KEY = 'vbio:components-main-width';
const DEFAULT_COMPONENTS_MAIN_WIDTH = 74;
const MIN_COMPONENTS_MAIN_WIDTH = 55;
const MAX_COMPONENTS_MAIN_WIDTH = 84;
const CONSTRAINTS_MAIN_WIDTH_STORAGE_KEY = 'vbio:constraints-main-width';
const DEFAULT_CONSTRAINTS_MAIN_WIDTH = 63;
const MIN_CONSTRAINTS_MAIN_WIDTH = 52;
const MAX_CONSTRAINTS_MAIN_WIDTH = 82;

function clampResultsMainWidth(value: number): number {
  return Math.max(MIN_RESULTS_MAIN_WIDTH, Math.min(MAX_RESULTS_MAIN_WIDTH, value));
}

function clampComponentsMainWidth(value: number): number {
  return Math.max(MIN_COMPONENTS_MAIN_WIDTH, Math.min(MAX_COMPONENTS_MAIN_WIDTH, value));
}

function clampConstraintsMainWidth(value: number): number {
  return Math.max(MIN_CONSTRAINTS_MAIN_WIDTH, Math.min(MAX_CONSTRAINTS_MAIN_WIDTH, value));
}

function mapTaskState(raw: string): TaskState {
  const normalized = raw.toUpperCase();
  if (normalized === 'SUCCESS') return 'SUCCESS';
  if (normalized === 'FAILURE') return 'FAILURE';
  if (normalized === 'REVOKED') return 'REVOKED';
  if (normalized === 'PENDING') return 'QUEUED';
  return 'RUNNING';
}

function readStatusText(task: { info?: Record<string, unknown>; state: string }) {
  if (!task.info) return task.state;
  const s1 = task.info.status;
  const s2 = task.info.message;
  if (typeof s1 === 'string' && s1.trim()) return s1;
  if (typeof s2 === 'string' && s2.trim()) return s2;
  return task.state;
}

function findProgressPercent(data: unknown): number | null {
  if (typeof data !== 'object' || data === null) return null;
  const obj = data as Record<string, unknown>;

  const directCandidates = ['progress', 'percent', 'percentage', 'pct', 'ratio'];
  for (const key of directCandidates) {
    const value = obj[key];
    if (typeof value === 'number' && Number.isFinite(value)) {
      const normalized = value <= 1 ? value * 100 : value;
      if (normalized >= 0 && normalized <= 100) {
        return normalized;
      }
    }
  }

  const nestedCandidates = ['tracker', 'meta', 'details', 'info'];
  for (const key of nestedCandidates) {
    const nested = obj[key];
    const nestedPercent = findProgressPercent(nested);
    if (nestedPercent !== null) return nestedPercent;
  }

  const current = obj.current;
  const total = obj.total;
  if (typeof current === 'number' && typeof total === 'number' && total > 0) {
    return Math.min(100, Math.max(0, (current / total) * 100));
  }

  return null;
}

function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const segment of path.split('.')) {
    if (!current || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[segment];
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

function readFiniteMetricSeries(data: Record<string, unknown>, paths: string[]): number[] {
  const values: number[] = [];
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'number' && Number.isFinite(value)) values.push(value);
  }
  return values;
}

function toneForPlddt(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const normalized = value <= 1 ? value * 100 : value;
  if (normalized >= 90) return 'excellent';
  if (normalized >= 70) return 'good';
  if (normalized >= 50) return 'medium';
  return 'low';
}

function toneForProbability(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const normalized = value <= 1 ? value * 100 : value;
  if (normalized >= 90) return 'excellent';
  if (normalized >= 70) return 'good';
  if (normalized >= 50) return 'medium';
  return 'low';
}

function toneForIc50(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value <= 0.1) return 'excellent';
  if (value <= 1) return 'good';
  if (value <= 10) return 'medium';
  return 'low';
}

function mean(values: number[]): number {
  if (values.length === 0) return Number.NaN;
  return values.reduce((acc, value) => acc + value, 0) / values.length;
}

function std(values: number[]): number {
  if (values.length <= 1) return 0;
  const m = mean(values);
  const variance = values.reduce((acc, value) => acc + (value - m) ** 2, 0) / values.length;
  return Math.sqrt(Math.max(0, variance));
}

function defaultConfigFromProject(project: Project): ProjectInputConfig {
  const config = buildDefaultInputConfig();
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

function normalizeComponents(components: InputComponent[]): InputComponent[] {
  return components.map((comp) => ({
    ...comp,
    sequence: normalizeComponentSequence(comp.type, comp.sequence)
  }));
}

function nonEmptyComponents(components: InputComponent[]): InputComponent[] {
  return components.filter((comp) => Boolean(comp.sequence));
}

function sortProjectTasks(rows: ProjectTask[]): ProjectTask[] {
  return [...rows].sort((a, b) => {
    const at = new Date(a.submitted_at || a.created_at).getTime();
    const bt = new Date(b.submitted_at || b.created_at).getTime();
    return bt - at;
  });
}

function listConstraintResidues(constraint: PredictionConstraint): Array<{ chainId: string; residue: number }> {
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

function allowedConstraintTypesForBackend(backend: string): PredictionConstraintType[] {
  if (String(backend || '').trim().toLowerCase() === 'alphafold3') {
    return AF3_CONSTRAINT_TYPES;
  }
  return ALL_CONSTRAINT_TYPES;
}

function filterConstraintsByBackend(constraints: PredictionConstraint[], backend: string): PredictionConstraint[] {
  const allowedTypeSet = new Set(allowedConstraintTypesForBackend(backend));
  return constraints.filter((item) => allowedTypeSet.has(item.type));
}

function normalizeConfigForBackend(inputConfig: ProjectInputConfig, backend: string): ProjectInputConfig {
  return {
    version: 1,
    components: normalizeComponents(inputConfig.components),
    constraints: filterConstraintsByBackend(inputConfig.constraints, backend),
    properties: inputConfig.properties,
    options: inputConfig.options
  };
}

function createDraftFingerprint(draft: DraftFields): string {
  const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, draft.backend);
  const activeComponents = nonEmptyComponents(normalizedConfig.components);
  const hasMsa = activeComponents.some((comp) => comp.type === 'protein' && comp.useMsa !== false);
  return JSON.stringify({
    name: draft.name.trim(),
    summary: draft.summary.trim(),
    backend: draft.backend,
    use_msa: hasMsa,
    color_mode: draft.color_mode || 'white',
    inputConfig: normalizedConfig
  });
}

export function ProjectDetailPage() {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const { session } = useAuth();

  const [project, setProject] = useState<Project | null>(null);
  const [projectTasks, setProjectTasks] = useState<ProjectTask[]>([]);
  const [draft, setDraft] = useState<DraftFields | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [statusLoading, setStatusLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);
  const [structureText, setStructureText] = useState('');
  const [structureFormat, setStructureFormat] = useState<'cif' | 'pdb'>('cif');
  const [statusInfo, setStatusInfo] = useState<Record<string, unknown> | null>(null);
  const [nowTs, setNowTs] = useState(Date.now());
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('results');
  const [savedDraftFingerprint, setSavedDraftFingerprint] = useState('');
  const [runMenuOpen, setRunMenuOpen] = useState(false);
  const [proteinTemplates, setProteinTemplates] = useState<Record<string, ProteinTemplateUpload>>({});
  const [pickedResidue, setPickedResidue] = useState<ConstraintResiduePick | null>(null);
  const [activeConstraintId, setActiveConstraintId] = useState<string | null>(null);
  const [selectedContactConstraintIds, setSelectedContactConstraintIds] = useState<string[]>([]);
  const [selectedConstraintTemplateComponentId, setSelectedConstraintTemplateComponentId] = useState<string | null>(null);
  const [constraintPickModeEnabled, setConstraintPickModeEnabled] = useState(false);
  const constraintPickSlotRef = useRef<Record<string, 'first' | 'second'>>({});
  const constraintSelectionAnchorRef = useRef<string | null>(null);
  const prevTaskStateRef = useRef<TaskState | null>(null);
  const statusRefreshInFlightRef = useRef<Set<string>>(new Set());
  const runActionRef = useRef<HTMLDivElement | null>(null);
  const [activeComponentId, setActiveComponentId] = useState<string | null>(null);
  const [sidebarTypeOpen, setSidebarTypeOpen] = useState<Record<InputComponent['type'], boolean>>({
    protein: true,
    ligand: false,
    dna: false,
    rna: false
  });
  const [sidebarConstraintsOpen, setSidebarConstraintsOpen] = useState(true);
  const [resultsMainWidth, setResultsMainWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_RESULTS_MAIN_WIDTH;
    const savedRaw = window.localStorage.getItem(RESULTS_MAIN_WIDTH_STORAGE_KEY);
    const saved = savedRaw ? Number.parseFloat(savedRaw) : Number.NaN;
    if (!Number.isFinite(saved)) return DEFAULT_RESULTS_MAIN_WIDTH;
    return clampResultsMainWidth(saved);
  });
  const [isResultsResizing, setIsResultsResizing] = useState(false);
  const resultsGridRef = useRef<HTMLDivElement | null>(null);
  const resultsResizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);
  const [componentsMainWidth, setComponentsMainWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_COMPONENTS_MAIN_WIDTH;
    const savedRaw = window.localStorage.getItem(COMPONENTS_MAIN_WIDTH_STORAGE_KEY);
    const saved = savedRaw ? Number.parseFloat(savedRaw) : Number.NaN;
    if (!Number.isFinite(saved)) return DEFAULT_COMPONENTS_MAIN_WIDTH;
    return clampComponentsMainWidth(saved);
  });
  const [isComponentsResizing, setIsComponentsResizing] = useState(false);
  const componentsWorkspaceRef = useRef<HTMLDivElement | null>(null);
  const componentsResizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);
  const [constraintsMainWidth, setConstraintsMainWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return DEFAULT_CONSTRAINTS_MAIN_WIDTH;
    const savedRaw = window.localStorage.getItem(CONSTRAINTS_MAIN_WIDTH_STORAGE_KEY);
    const saved = savedRaw ? Number.parseFloat(savedRaw) : Number.NaN;
    if (!Number.isFinite(saved)) return DEFAULT_CONSTRAINTS_MAIN_WIDTH;
    return clampConstraintsMainWidth(saved);
  });
  const [isConstraintsResizing, setIsConstraintsResizing] = useState(false);
  const constraintsWorkspaceRef = useRef<HTMLDivElement | null>(null);
  const constraintsResizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);

  useEffect(() => {
    const tab = new URLSearchParams(location.search).get('tab');
    if (tab === 'inputs') {
      setWorkspaceTab('basics');
      return;
    }
    if (tab === 'results' || tab === 'basics' || tab === 'components' || tab === 'constraints') {
      setWorkspaceTab(tab);
    }
  }, [location.search, projectId]);

  useEffect(() => {
    if (!draft) return;
    setProteinTemplates((prev) => {
      const validProteinIds = new Set(
        draft.inputConfig.components.filter((component) => component.type === 'protein').map((component) => component.id)
      );
      let changed = false;
      const next: Record<string, ProteinTemplateUpload> = {};
      for (const [componentId, upload] of Object.entries(prev)) {
        if (validProteinIds.has(componentId)) {
          next[componentId] = upload;
        } else {
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [draft]);

  const canEdit = useMemo(() => {
    if (!project || !session) return false;
    return project.user_id === session.userId;
  }, [project, session]);

  const progressPercent = useMemo(() => {
    if (!project) return 0;
    if (['SUCCESS', 'FAILURE', 'REVOKED'].includes(project.task_state)) return 100;
    const explicit = findProgressPercent(statusInfo);
    if (explicit !== null) return Math.max(0, Math.min(100, explicit));
    if (project.task_state === 'RUNNING') return 62;
    if (project.task_state === 'QUEUED') return 18;
    return 0;
  }, [project, statusInfo]);

  const waitingSeconds = useMemo(() => {
    if (!project?.submitted_at) return null;
    if (!['QUEUED', 'RUNNING'].includes(project.task_state)) return null;
    const duration = Math.floor((nowTs - new Date(project.submitted_at).getTime()) / 1000);
    return Math.max(0, duration);
  }, [project, nowTs]);

  const normalizedDraftComponents = useMemo(() => {
    if (!draft) return [];
    return normalizeComponents(draft.inputConfig.components);
  }, [draft]);
  const allowedConstraintTypes = useMemo(() => {
    return allowedConstraintTypesForBackend(draft?.backend || project?.backend || 'boltz');
  }, [draft?.backend, project?.backend]);
  const isAf3Backend = useMemo(() => {
    return String(draft?.backend || project?.backend || '').toLowerCase() === 'alphafold3';
  }, [draft?.backend, project?.backend]);

  useEffect(() => {
    if (!draft) return;
    const nextConstraints = filterConstraintsByBackend(draft.inputConfig.constraints, draft.backend);
    if (nextConstraints.length === draft.inputConfig.constraints.length) return;

    const keptIds = new Set(nextConstraints.map((item) => item.id));
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            inputConfig: {
              ...prev.inputConfig,
              constraints: nextConstraints
            }
          }
        : prev
    );
    setSelectedContactConstraintIds((prev) => prev.filter((id) => keptIds.has(id)));
    if (activeConstraintId && !keptIds.has(activeConstraintId)) {
      setActiveConstraintId(null);
    }
    if (constraintSelectionAnchorRef.current && !keptIds.has(constraintSelectionAnchorRef.current)) {
      constraintSelectionAnchorRef.current = null;
    }
  }, [draft, activeConstraintId]);

  const componentTypeBuckets = useMemo(() => {
    const buckets: Record<
      InputComponent['type'],
      Array<{ id: string; typeLabel: string; typeOrder: number; globalOrder: number; filled: boolean }>
    > = {
      protein: [],
      ligand: [],
      dna: [],
      rna: []
    };
    const typeCounters: Record<InputComponent['type'], number> = {
      protein: 0,
      ligand: 0,
      dna: 0,
      rna: 0
    };

    normalizedDraftComponents.forEach((component, index) => {
      typeCounters[component.type] += 1;
      buckets[component.type].push({
        id: component.id,
        typeLabel: componentTypeLabel(component.type),
        typeOrder: typeCounters[component.type],
        globalOrder: index + 1,
        filled: Boolean(component.sequence.trim())
      });
    });

    return buckets;
  }, [normalizedDraftComponents]);

  const constraintCount = draft?.inputConfig.constraints.length || 0;
  const snapshotPlddt = useMemo(() => {
    if (!project?.confidence) return null;
    const raw = readFirstFiniteMetric(project.confidence as Record<string, unknown>, [
      'complex_plddt_protein',
      'complex_plddt',
      'plddt'
    ]);
    if (raw === null) return null;
    return raw <= 1 ? raw * 100 : raw;
  }, [project]);
  const snapshotIptm = useMemo(() => {
    if (!project?.confidence) return null;
    return readFirstFiniteMetric(project.confidence as Record<string, unknown>, ['iptm', 'ligand_iptm', 'protein_iptm']);
  }, [project]);
  const primaryLigandValue = useMemo(() => {
    return normalizedDraftComponents.find((comp) => comp.type === 'ligand' && comp.sequence)?.sequence || '';
  }, [normalizedDraftComponents]);
  const primaryLigandIsSmiles = useMemo(() => {
    const ligand = normalizedDraftComponents.find((comp) => comp.type === 'ligand' && comp.sequence);
    if (!ligand) return false;
    return ligand.inputMethod !== 'ccd';
  }, [normalizedDraftComponents]);
  const snapshotBindingValues = useMemo(() => {
    if (!project?.affinity) return null;
    const values = readFiniteMetricSeries(project.affinity as Record<string, unknown>, [
      'affinity_probability_binary',
      'affinity_probability_binary1',
      'affinity_probability_binary2'
    ]).map((value) => (value > 1 && value <= 100 ? value / 100 : value));
    const normalized = values.filter((value) => Number.isFinite(value) && value >= 0 && value <= 1);
    if (normalized.length === 0) return null;
    return normalized;
  }, [project]);
  const snapshotBindingProbability = useMemo(() => {
    if (!snapshotBindingValues?.length) return null;
    return Math.max(0, Math.min(1, mean(snapshotBindingValues)));
  }, [snapshotBindingValues]);
  const snapshotBindingStd = useMemo(() => {
    if (!snapshotBindingValues?.length) return null;
    return std(snapshotBindingValues);
  }, [snapshotBindingValues]);
  const snapshotLogIc50Values = useMemo(() => {
    if (!project?.affinity) return null;
    const logValues = readFiniteMetricSeries(project.affinity as Record<string, unknown>, [
      'affinity_pred_value',
      'affinity_pred_value1',
      'affinity_pred_value2'
    ]);
    if (logValues.length === 0) return null;
    return logValues;
  }, [project]);
  const snapshotIc50Um = useMemo(() => {
    if (!snapshotLogIc50Values?.length) return null;
    return 10 ** mean(snapshotLogIc50Values);
  }, [snapshotLogIc50Values]);
  const snapshotIc50Error = useMemo(() => {
    if (!snapshotLogIc50Values?.length || snapshotLogIc50Values.length <= 1) return null;
    const meanLog = mean(snapshotLogIc50Values);
    const stdLog = std(snapshotLogIc50Values);
    const center = 10 ** meanLog;
    const lower = 10 ** (meanLog - stdLog);
    const upper = 10 ** (meanLog + stdLog);
    return {
      plus: Math.max(0, upper - center),
      minus: Math.max(0, center - lower)
    };
  }, [snapshotLogIc50Values]);
  const snapshotPlddtTone = useMemo(() => toneForPlddt(snapshotPlddt), [snapshotPlddt]);
  const snapshotIptmTone = useMemo(() => toneForProbability(snapshotIptm), [snapshotIptm]);
  const snapshotIc50Tone = useMemo(() => toneForIc50(snapshotIc50Um), [snapshotIc50Um]);
  const snapshotBindingTone = useMemo(
    () => toneForProbability(snapshotBindingProbability),
    [snapshotBindingProbability]
  );
  const activeConstraintIndex = useMemo(() => {
    if (!draft || !activeConstraintId) return -1;
    return draft.inputConfig.constraints.findIndex((item) => item.id === activeConstraintId);
  }, [draft, activeConstraintId]);
  const selectedContactConstraintIdSet = useMemo(() => {
    return new Set(selectedContactConstraintIds);
  }, [selectedContactConstraintIds]);

  const constraintTemplateOptions = useMemo(() => {
    if (!draft) return null;
    let proteinOrder = 0;
    const options: Array<{
      componentId: string;
      label: string;
      fileName: string;
      format: 'cif' | 'pdb';
      chainId: string;
      content: string;
    }> = [];
    for (const component of draft.inputConfig.components) {
      if (component.type !== 'protein') continue;
      proteinOrder += 1;
      const upload = proteinTemplates[component.id];
      if (upload) {
        options.push({
          componentId: component.id,
          label: `Protein ${proteinOrder}`,
          fileName: upload.fileName,
          format: upload.format,
          chainId: upload.chainId,
          content: upload.content
        });
      }
    }
    return options;
  }, [draft, proteinTemplates]);

  const selectedTemplatePreview = useMemo(() => {
    if (!constraintTemplateOptions || constraintTemplateOptions.length === 0) return null;
    if (selectedConstraintTemplateComponentId) {
      const selected = constraintTemplateOptions.find((item) => item.componentId === selectedConstraintTemplateComponentId);
      if (selected) return selected;
    }
    return constraintTemplateOptions[0];
  }, [constraintTemplateOptions, selectedConstraintTemplateComponentId]);

  const selectedTemplateResidueIndexMap = useMemo(() => {
    if (!selectedTemplatePreview) return {};
    try {
      return extractProteinChainResidueIndexMap(selectedTemplatePreview.content, selectedTemplatePreview.format);
    } catch {
      return {};
    }
  }, [selectedTemplatePreview]);
  const selectedTemplateSequenceToStructureResidueMap = useMemo<Record<number, number>>(() => {
    if (!selectedTemplatePreview) return {};
    const templateChainId = String(selectedTemplatePreview.chainId || '').trim();
    if (!templateChainId) return {};
    const forwardMap = selectedTemplateResidueIndexMap[templateChainId] || {};
    const inverseMap: Record<number, number> = {};

    for (const [structureResidueRaw, sequenceResidueRaw] of Object.entries(forwardMap)) {
      const structureResidue = Math.floor(Number(structureResidueRaw));
      const sequenceResidue = Math.floor(Number(sequenceResidueRaw));
      if (!Number.isFinite(structureResidue) || structureResidue <= 0) continue;
      if (!Number.isFinite(sequenceResidue) || sequenceResidue <= 0) continue;
      if (inverseMap[sequenceResidue] === undefined || structureResidue < inverseMap[sequenceResidue]) {
        inverseMap[sequenceResidue] = structureResidue;
      }
    }

    return inverseMap;
  }, [selectedTemplatePreview, selectedTemplateResidueIndexMap]);
  useEffect(() => {
    const ids = (constraintTemplateOptions || []).map((item) => item.componentId);
    if (ids.length === 0) {
      if (selectedConstraintTemplateComponentId !== null) {
        setSelectedConstraintTemplateComponentId(null);
      }
      return;
    }
    if (!selectedConstraintTemplateComponentId || !ids.includes(selectedConstraintTemplateComponentId)) {
      setSelectedConstraintTemplateComponentId(ids[0]);
    }
  }, [constraintTemplateOptions, selectedConstraintTemplateComponentId]);

  useEffect(() => {
    if (!draft) return;
    const ids = draft.inputConfig.constraints.map((item) => item.id);
    if (ids.length === 0) {
      if (activeConstraintId !== null) setActiveConstraintId(null);
      setSelectedContactConstraintIds((prev) => (prev.length > 0 ? [] : prev));
      constraintSelectionAnchorRef.current = null;
      return;
    }
    if (activeConstraintId && !ids.includes(activeConstraintId)) {
      setActiveConstraintId(ids[0]);
    }
    const validContactIds = new Set(
      draft.inputConfig.constraints.filter((item) => item.type === 'contact').map((item) => item.id)
    );
    setSelectedContactConstraintIds((prev) => {
      const filtered = prev.filter((id) => validContactIds.has(id));
      if (filtered.length === prev.length && filtered.every((id, index) => id === prev[index])) {
        return prev;
      }
      return filtered;
    });
    if (constraintSelectionAnchorRef.current && !validContactIds.has(constraintSelectionAnchorRef.current)) {
      constraintSelectionAnchorRef.current = null;
    }
  }, [draft, activeConstraintId]);

  useEffect(() => {
    if (!constraintPickModeEnabled || !activeConstraintId) return;
    constraintPickSlotRef.current[activeConstraintId] = 'first';
  }, [constraintPickModeEnabled, activeConstraintId]);

  useEffect(() => {
    if (!draft) return;
    const ids = draft.inputConfig.components.map((item) => item.id);
    if (ids.length === 0) {
      if (activeComponentId !== null) setActiveComponentId(null);
      return;
    }
    if (!activeComponentId || !ids.includes(activeComponentId)) {
      setActiveComponentId(ids[0]);
    }
  }, [draft, activeComponentId]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(RESULTS_MAIN_WIDTH_STORAGE_KEY, resultsMainWidth.toFixed(2));
  }, [resultsMainWidth]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(COMPONENTS_MAIN_WIDTH_STORAGE_KEY, componentsMainWidth.toFixed(2));
  }, [componentsMainWidth]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(CONSTRAINTS_MAIN_WIDTH_STORAGE_KEY, constraintsMainWidth.toFixed(2));
  }, [constraintsMainWidth]);

  useEffect(() => {
    if (!isResultsResizing) return;

    const onPointerMove = (event: globalThis.PointerEvent) => {
      const state = resultsResizeRef.current;
      const grid = resultsGridRef.current;
      if (!state || !grid) return;

      const containerWidth = grid.getBoundingClientRect().width;
      if (!Number.isFinite(containerWidth) || containerWidth <= 0) return;

      const handleWidth = 10;
      const minAsideWidth = 280;
      const minMainPx = containerWidth * (MIN_RESULTS_MAIN_WIDTH / 100);
      const maxMainPxByPct = containerWidth * (MAX_RESULTS_MAIN_WIDTH / 100);
      const maxMainPxByAside = containerWidth - minAsideWidth - handleWidth;
      const maxMainPx = Math.min(maxMainPxByPct, maxMainPxByAside);
      if (maxMainPx <= minMainPx) return;

      const startMainPx = (state.startWidthPercent / 100) * containerWidth;
      const nextMainPx = startMainPx + (event.clientX - state.startX);
      const clampedMainPx = Math.min(maxMainPx, Math.max(minMainPx, nextMainPx));
      const nextPercent = (clampedMainPx / containerWidth) * 100;
      setResultsMainWidth(clampResultsMainWidth(nextPercent));
    };

    const stopResizing = () => {
      setIsResultsResizing(false);
      resultsResizeRef.current = null;
    };

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', stopResizing);
    window.addEventListener('pointercancel', stopResizing);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', stopResizing);
      window.removeEventListener('pointercancel', stopResizing);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResultsResizing]);

  useEffect(() => {
    if (!isComponentsResizing) return;

    const onPointerMove = (event: globalThis.PointerEvent) => {
      const state = componentsResizeRef.current;
      const grid = componentsWorkspaceRef.current;
      if (!state || !grid) return;

      const containerWidth = grid.getBoundingClientRect().width;
      if (!Number.isFinite(containerWidth) || containerWidth <= 0) return;

      const handleWidth = 10;
      const minAsideWidth = 250;
      const minMainPx = containerWidth * (MIN_COMPONENTS_MAIN_WIDTH / 100);
      const maxMainPxByPct = containerWidth * (MAX_COMPONENTS_MAIN_WIDTH / 100);
      const maxMainPxByAside = containerWidth - minAsideWidth - handleWidth;
      const maxMainPx = Math.min(maxMainPxByPct, maxMainPxByAside);
      if (maxMainPx <= minMainPx) return;

      const startMainPx = (state.startWidthPercent / 100) * containerWidth;
      const nextMainPx = startMainPx + (event.clientX - state.startX);
      const clampedMainPx = Math.min(maxMainPx, Math.max(minMainPx, nextMainPx));
      const nextPercent = (clampedMainPx / containerWidth) * 100;
      setComponentsMainWidth(clampComponentsMainWidth(nextPercent));
    };

    const stopResizing = () => {
      setIsComponentsResizing(false);
      componentsResizeRef.current = null;
    };

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', stopResizing);
    window.addEventListener('pointercancel', stopResizing);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', stopResizing);
      window.removeEventListener('pointercancel', stopResizing);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isComponentsResizing]);

  useEffect(() => {
    if (!isConstraintsResizing) return;

    const onPointerMove = (event: globalThis.PointerEvent) => {
      const state = constraintsResizeRef.current;
      const grid = constraintsWorkspaceRef.current;
      if (!state || !grid) return;

      const containerWidth = grid.getBoundingClientRect().width;
      if (!Number.isFinite(containerWidth) || containerWidth <= 0) return;

      const handleWidth = 10;
      const minAsideWidth = 300;
      const minMainPx = containerWidth * (MIN_CONSTRAINTS_MAIN_WIDTH / 100);
      const maxMainPxByPct = containerWidth * (MAX_CONSTRAINTS_MAIN_WIDTH / 100);
      const maxMainPxByAside = containerWidth - minAsideWidth - handleWidth;
      const maxMainPx = Math.min(maxMainPxByPct, maxMainPxByAside);
      if (maxMainPx <= minMainPx) return;

      const startMainPx = (state.startWidthPercent / 100) * containerWidth;
      const nextMainPx = startMainPx + (event.clientX - state.startX);
      const clampedMainPx = Math.min(maxMainPx, Math.max(minMainPx, nextMainPx));
      const nextPercent = (clampedMainPx / containerWidth) * 100;
      setConstraintsMainWidth(clampConstraintsMainWidth(nextPercent));
    };

    const stopResizing = () => {
      setIsConstraintsResizing(false);
      constraintsResizeRef.current = null;
    };

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', stopResizing);
    window.addEventListener('pointercancel', stopResizing);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', stopResizing);
      window.removeEventListener('pointercancel', stopResizing);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isConstraintsResizing]);

  const handleResultsResizerPointerDown = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      if (window.matchMedia('(max-width: 1100px)').matches) return;
      const grid = resultsGridRef.current;
      if (!grid) return;

      resultsResizeRef.current = {
        startX: event.clientX,
        startWidthPercent: resultsMainWidth
      };
      setIsResultsResizing(true);
      event.preventDefault();
    },
    [resultsMainWidth]
  );

  const handleResultsResizerKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setResultsMainWidth((current) => clampResultsMainWidth(current - 1.5));
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setResultsMainWidth((current) => clampResultsMainWidth(current + 1.5));
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setResultsMainWidth(DEFAULT_RESULTS_MAIN_WIDTH);
    }
  }, []);

  const handleComponentsResizerPointerDown = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      if (window.matchMedia('(max-width: 1100px)').matches) return;
      const grid = componentsWorkspaceRef.current;
      if (!grid) return;

      componentsResizeRef.current = {
        startX: event.clientX,
        startWidthPercent: componentsMainWidth
      };
      setIsComponentsResizing(true);
      event.preventDefault();
    },
    [componentsMainWidth]
  );

  const handleComponentsResizerKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setComponentsMainWidth((current) => clampComponentsMainWidth(current - 1.5));
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setComponentsMainWidth((current) => clampComponentsMainWidth(current + 1.5));
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setComponentsMainWidth(DEFAULT_COMPONENTS_MAIN_WIDTH);
    }
  }, []);

  const handleConstraintsResizerPointerDown = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      if (window.matchMedia('(max-width: 1100px)').matches) return;
      const grid = constraintsWorkspaceRef.current;
      if (!grid) return;

      constraintsResizeRef.current = {
        startX: event.clientX,
        startWidthPercent: constraintsMainWidth
      };
      setIsConstraintsResizing(true);
      event.preventDefault();
    },
    [constraintsMainWidth]
  );

  const handleConstraintsResizerKeyDown = useCallback((event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setConstraintsMainWidth((current) => clampConstraintsMainWidth(current - 1.5));
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setConstraintsMainWidth((current) => clampConstraintsMainWidth(current + 1.5));
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setConstraintsMainWidth(DEFAULT_CONSTRAINTS_MAIN_WIDTH);
    }
  }, []);

  const resultsGridStyle = useMemo<ResultsGridStyle>(
    () => ({
      '--results-main-width': `${resultsMainWidth.toFixed(2)}%`
    }),
    [resultsMainWidth]
  );

  const componentsGridStyle = useMemo<InputsGridStyle>(
    () => ({
      '--inputs-main-width': `${componentsMainWidth.toFixed(2)}%`
    }),
    [componentsMainWidth]
  );

  const constraintsGridStyle = useMemo<ConstraintsGridStyle>(
    () => ({
      '--constraints-main-width': `${constraintsMainWidth.toFixed(2)}%`
    }),
    [constraintsMainWidth]
  );

  const activeChainInfos = useMemo(() => {
    return buildChainInfos(nonEmptyComponents(normalizedDraftComponents));
  }, [normalizedDraftComponents]);

  const activeChainIds = useMemo(() => {
    return activeChainInfos.map((item) => item.id);
  }, [activeChainInfos]);

  const chainInfoById = useMemo(() => {
    const byId = new Map<string, (typeof activeChainInfos)[number]>();
    for (const info of activeChainInfos) {
      byId.set(info.id, info);
    }
    return byId;
  }, [activeChainInfos]);
  const constraintTemplateComponentIdSet = useMemo(() => {
    return new Set((constraintTemplateOptions || []).map((item) => item.componentId));
  }, [constraintTemplateOptions]);
  const resolveTemplateComponentIdForConstraint = useCallback(
    (constraint: PredictionConstraint | null | undefined): string | null => {
      if (!constraint) return null;
      for (const residueRef of listConstraintResidues(constraint)) {
        const chainId = String(residueRef.chainId || '').trim();
        if (!chainId) continue;
        const info = chainInfoById.get(chainId);
        if (!info || info.type !== 'protein') continue;
        if (constraintTemplateComponentIdSet.has(info.componentId)) {
          return info.componentId;
        }
      }
      return null;
    },
    [chainInfoById, constraintTemplateComponentIdSet]
  );
  useEffect(() => {
    if (!draft) return;
    const constraintsById = new Map(draft.inputConfig.constraints.map((item) => [item.id, item] as const));
    const preferredIds: string[] = [];
    if (activeConstraintId) preferredIds.push(activeConstraintId);
    for (let i = selectedContactConstraintIds.length - 1; i >= 0; i -= 1) {
      const id = selectedContactConstraintIds[i];
      if (!preferredIds.includes(id)) {
        preferredIds.push(id);
      }
    }

    let suggested: string | null = null;
    for (const constraintId of preferredIds) {
      const candidate = resolveTemplateComponentIdForConstraint(constraintsById.get(constraintId));
      if (candidate) {
        suggested = candidate;
        break;
      }
    }

    if (suggested && suggested !== selectedConstraintTemplateComponentId) {
      setSelectedConstraintTemplateComponentId(suggested);
    }
  }, [
    draft,
    activeConstraintId,
    selectedContactConstraintIds,
    resolveTemplateComponentIdForConstraint,
    selectedConstraintTemplateComponentId
  ]);

  const ligandChainOptions = useMemo(() => {
    return activeChainInfos.filter((item) => item.type === 'ligand');
  }, [activeChainInfos]);

  const targetChainOptions = useMemo(() => {
    return activeChainInfos.filter((item) => item.type !== 'ligand');
  }, [activeChainInfos]);

  const constraintHighlightResidues = useMemo<MolstarResidueHighlight[]>(() => {
    if (!draft) return [];
    const byKey = new Map<string, MolstarResidueHighlight>();
    const highlightConstraintIds = new Set<string>(selectedContactConstraintIds);
    if (activeConstraintId) {
      highlightConstraintIds.add(activeConstraintId);
    }
    if (highlightConstraintIds.size === 0) return [];

    for (const constraint of draft.inputConfig.constraints) {
      if (!highlightConstraintIds.has(constraint.id)) continue;
      const isActive = constraint.id === activeConstraintId;
      for (const residueRef of listConstraintResidues(constraint)) {
        const chainId = String(residueRef.chainId || '').trim();
        const residue = Math.max(1, Math.floor(Number(residueRef.residue) || 0));
        if (!chainId || !Number.isFinite(residue) || residue <= 0) continue;
        const chainInfo = chainInfoById.get(chainId);
        if (chainInfo && chainInfo.type !== 'protein') continue;
        const key = `${chainId}:${residue}`;
        const existing = byKey.get(key);
        if (!existing) {
          byKey.set(key, { chainId, residue, emphasis: isActive ? 'active' : 'default' });
          continue;
        }
        if (isActive && existing.emphasis !== 'active') {
          byKey.set(key, { ...existing, emphasis: 'active' });
        }
      }
    }

    return Array.from(byKey.values());
  }, [draft, activeConstraintId, chainInfoById, selectedContactConstraintIds]);

  const activeConstraintResidue = useMemo<MolstarResidueHighlight | null>(() => {
    if (!draft || !activeConstraintId) return null;
    const validChains = new Set(activeChainInfos.map((item) => item.id));
    const activeConstraint = draft.inputConfig.constraints.find((item) => item.id === activeConstraintId);
    if (!activeConstraint) return null;
    const first = listConstraintResidues(activeConstraint).find(
      (item) => validChains.has(item.chainId) && Number.isFinite(Number(item.residue)) && Number(item.residue) > 0
    );
    if (!first) return null;
    return {
      chainId: first.chainId,
      residue: Math.max(1, Math.floor(Number(first.residue))),
      emphasis: 'active'
    };
  }, [draft, activeConstraintId, activeChainInfos]);

  const constraintViewerHighlightResidues = useMemo<MolstarResidueHighlight[]>(() => {
    if (!selectedTemplatePreview) return constraintHighlightResidues;
    const ownerProteinChains = activeChainInfos.filter(
      (info) => info.componentId === selectedTemplatePreview.componentId && info.type === 'protein'
    );
    if (ownerProteinChains.length === 0) return [];
    const ownerChainIds = new Set(ownerProteinChains.map((info) => info.id));
    const viewerChainId = String(selectedTemplatePreview.chainId || '').trim() || ownerProteinChains[0]?.id || '';
    if (!viewerChainId) return [];

    const hasSequenceMapping = Object.keys(selectedTemplateSequenceToStructureResidueMap).length > 0;
    const byMappedResidue = new Map<string, MolstarResidueHighlight>();

    for (const item of constraintHighlightResidues) {
      const sourceChainInfo = chainInfoById.get(item.chainId);
      if (sourceChainInfo) {
        if (sourceChainInfo.type !== 'protein') continue;
        if (!ownerChainIds.has(item.chainId)) continue;
      }
      const mappedResidueRaw = selectedTemplateSequenceToStructureResidueMap[item.residue];
      const mappedResidue =
        typeof mappedResidueRaw === 'number' && Number.isFinite(mappedResidueRaw)
          ? Math.floor(mappedResidueRaw)
          : hasSequenceMapping
            ? Number.NaN
            : item.residue;
      if (!Number.isFinite(mappedResidue) || mappedResidue <= 0) continue;

      const key = `${viewerChainId}:${mappedResidue}`;
      const existing = byMappedResidue.get(key);
      if (!existing) {
        byMappedResidue.set(key, {
          chainId: viewerChainId,
          residue: mappedResidue,
          emphasis: item.emphasis
        });
        continue;
      }
      if (item.emphasis === 'active' && existing.emphasis !== 'active') {
        byMappedResidue.set(key, { ...existing, emphasis: 'active' });
      }
    }

    return Array.from(byMappedResidue.values());
  }, [
    selectedTemplatePreview,
    activeChainInfos,
    chainInfoById,
    constraintHighlightResidues,
    selectedTemplateSequenceToStructureResidueMap
  ]);

  const constraintViewerActiveResidue = useMemo<MolstarResidueHighlight | null>(() => {
    if (!selectedTemplatePreview) return activeConstraintResidue;
    return constraintViewerHighlightResidues.find((item) => item.emphasis === 'active') || null;
  }, [selectedTemplatePreview, activeConstraintResidue, constraintViewerHighlightResidues]);

  useEffect(() => {
    if (!draft) return;
    const props = draft.inputConfig.properties;
    const hasLigand = ligandChainOptions.length > 0;
    const hasTarget = targetChainOptions.length > 0;
    const nextLigand =
      props.ligand && ligandChainOptions.some((item) => item.id === props.ligand)
        ? props.ligand
        : ligandChainOptions[0]?.id || null;
    const nextTarget =
      props.target && targetChainOptions.some((item) => item.id === props.target)
        ? props.target
        : targetChainOptions[0]?.id || null;

    const nextAffinity = hasLigand && hasTarget ? props.affinity : false;
    const nextProps = {
      ...props,
      affinity: nextAffinity,
      target: nextAffinity ? nextTarget : null,
      ligand: nextAffinity ? nextLigand : null,
      binder: nextAffinity ? nextLigand : null
    };

    if (
      nextProps.affinity !== props.affinity ||
      nextProps.target !== props.target ||
      nextProps.ligand !== props.ligand ||
      nextProps.binder !== props.binder
    ) {
      setDraft((prev) =>
        prev
          ? {
              ...prev,
              inputConfig: {
                ...prev.inputConfig,
                properties: nextProps
              }
            }
          : prev
      );
    }
  }, [draft, ligandChainOptions, targetChainOptions]);

  useEffect(() => {
    if (!project) return;
    const workflowDef = getWorkflowDefinition(project.task_type);
    if (workflowDef.key !== 'prediction' && (workspaceTab === 'components' || workspaceTab === 'constraints')) {
      setWorkspaceTab('basics');
    }
  }, [project, workspaceTab]);

  useEffect(() => {
    if (!project) return;
    const prev = prevTaskStateRef.current;
    const next = project.task_state;
    if (prev && prev !== next && next === 'SUCCESS') {
      setWorkspaceTab('results');
    }
    prevTaskStateRef.current = next;
  }, [project?.id, project?.task_state]);

  const isActiveRuntime = useMemo(() => {
    if (!project) return false;
    return project.task_state === 'QUEUED' || project.task_state === 'RUNNING';
  }, [project]);

  const totalRuntimeSeconds = useMemo(() => {
    if (!project) return null;
    if (project.duration_seconds !== null && project.duration_seconds !== undefined) {
      return project.duration_seconds;
    }
    if (project.submitted_at && project.completed_at) {
      const duration = (new Date(project.completed_at).getTime() - new Date(project.submitted_at).getTime()) / 1000;
      return Number.isFinite(duration) && duration >= 0 ? duration : null;
    }
    return null;
  }, [project]);

  const loadProjectTasks = useCallback(async (currentProjectId: string, options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent);
    try {
      const rows = await listProjectTasks(currentProjectId);
      setProjectTasks(sortProjectTasks(rows));
    } catch (err) {
      if (!silent) {
        setError(err instanceof Error ? err.message : 'Failed to load task history.');
      }
    }
  }, []);

  const loadProject = async () => {
    setLoading(true);
    setError(null);
    setProjectTasks([]);
    try {
      const next = await getProjectById(projectId);
      if (!next || next.deleted_at) {
        throw new Error('Project not found or already deleted.');
      }
      if (session && next.user_id !== session.userId) {
        throw new Error('You do not have permission to access this project.');
      }

      const savedConfig = loadProjectInputConfig(next.id);
      const resolvedConfig = savedConfig || defaultConfigFromProject(next);
      const backendConstraints = filterConstraintsByBackend(resolvedConfig.constraints, next.backend);

      const savedUiState = loadProjectUiState(next.id);
      const loadedDraft: DraftFields = {
        name: next.name,
        summary: next.summary,
        backend: next.backend,
        use_msa: next.use_msa,
        color_mode: next.color_mode || 'white',
        inputConfig: {
          ...resolvedConfig,
          constraints: backendConstraints
        }
      };
      setDraft(loadedDraft);
      setSavedDraftFingerprint(createDraftFingerprint(loadedDraft));
      setRunMenuOpen(false);
      setProteinTemplates(savedUiState?.proteinTemplates || {});
      setActiveConstraintId(savedUiState?.activeConstraintId || null);
      setSelectedContactConstraintIds([]);
      constraintSelectionAnchorRef.current = null;
      setSelectedConstraintTemplateComponentId(savedUiState?.selectedConstraintTemplateComponentId || null);
      setPickedResidue(null);
      setProject(next);
      await loadProjectTasks(next.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load project.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadProject();
  }, [projectId]);

  useEffect(() => {
    if (!project) return;
    if (!['QUEUED', 'RUNNING'].includes(project.task_state)) return;

    const timer = setInterval(() => {
      setNowTs(Date.now());
    }, 1000);

    return () => clearInterval(timer);
  }, [project?.task_state]);

  useEffect(() => {
    if (!project) return;
    saveProjectUiState(project.id, {
      proteinTemplates,
      activeConstraintId,
      selectedConstraintTemplateComponentId
    });
  }, [project, proteinTemplates, activeConstraintId, selectedConstraintTemplateComponentId]);

  const patch = async (payload: Partial<Project>) => {
    if (!project) return null;
    const next = await updateProject(project.id, payload);
    setProject(next);
    return next;
  };

  const patchTask = async (taskRowId: string, payload: Partial<ProjectTask>) => {
    if (taskRowId.startsWith('local-')) {
      setProjectTasks((prev) =>
        sortProjectTasks(
          prev.map((row) =>
            row.id === taskRowId
              ? {
                  ...row,
                  ...payload,
                  updated_at: new Date().toISOString()
                }
              : row
          )
        )
      );
      return null;
    }
    const next = await updateProjectTask(taskRowId, payload);
    setProjectTasks((prev) => sortProjectTasks(prev.map((row) => (row.id === taskRowId ? next : row))));
    return next;
  };

  const saveDraft = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!project || !draft) return;

    setSaving(true);
    setError(null);
    try {
      const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, draft.backend);
      const activeComponents = nonEmptyComponents(normalizedConfig.components);
      const { proteinSequence, ligandSmiles } = extractPrimaryProteinAndLigand(normalizedConfig);
      const hasMsa = activeComponents.some((comp) => comp.type === 'protein' && comp.useMsa !== false);

      const next = await patch({
        name: draft.name.trim(),
        summary: draft.summary.trim(),
        backend: draft.backend,
        use_msa: hasMsa,
        protein_sequence: proteinSequence,
        ligand_smiles: ligandSmiles,
        color_mode: draft.color_mode,
        status_text: 'Draft saved'
      });

      if (next) {
        saveProjectInputConfig(next.id, normalizedConfig);
        const nextDraft: DraftFields = {
          name: next.name,
          summary: next.summary,
          backend: next.backend,
          use_msa: next.use_msa,
          color_mode: next.color_mode || 'white',
          inputConfig: normalizedConfig
        };
        setDraft(nextDraft);
        setSavedDraftFingerprint(createDraftFingerprint(nextDraft));
        setRunMenuOpen(false);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save draft.');
    } finally {
      setSaving(false);
    }
  };

  const pullResultForViewer = async (taskId: string, options?: { taskRowId?: string; persistProject?: boolean }) => {
    const shouldPersistProject = options?.persistProject !== false;
    setResultError(null);
    try {
      const blob = await downloadResultBlob(taskId);
      const parsed = await parseResultBundle(blob);
      if (!parsed) {
        throw new Error('No structure file was found in the result archive.');
      }

      setStructureText(parsed.structureText);
      setStructureFormat(parsed.structureFormat);

      if (shouldPersistProject) {
        await patch({
          confidence: parsed.confidence,
          affinity: parsed.affinity,
          structure_name: parsed.structureName
        });
      }
      if (options?.taskRowId) {
        await patchTask(options.taskRowId, {
          confidence: parsed.confidence,
          affinity: parsed.affinity,
          structure_name: parsed.structureName
        });
      }
    } catch (err) {
      setResultError(err instanceof Error ? err.message : 'Failed to parse downloaded result.');
    }
  };

  const refreshStatus = async (options?: { silent?: boolean }) => {
    const silent = Boolean(options?.silent);

    if (!project?.task_id) {
      if (!silent) {
        setError('No task ID yet. Submit a prediction first.');
      }
      return;
    }

    const activeTaskId = project.task_id.trim();
    if (!activeTaskId) return;
    if (statusRefreshInFlightRef.current.has(activeTaskId)) return;
    statusRefreshInFlightRef.current.add(activeTaskId);

    if (!silent) {
      setStatusLoading(true);
      setError(null);
    }

    try {
      const status = await getTaskStatus(activeTaskId);
      const taskState = mapTaskState(status.state);
      const statusText = readStatusText(status);
      setStatusInfo(status.info ?? null);
      const nextErrorText = taskState === 'FAILURE' ? statusText : '';
      const runtimeTask = projectTasks.find((item) => item.task_id === activeTaskId) || null;
      const completedAt = taskState === 'SUCCESS' ? new Date().toISOString() : null;
      const submittedAt = runtimeTask?.submitted_at || project.submitted_at;
      const durationSeconds =
        taskState === 'SUCCESS' && submittedAt
          ? (() => {
              const duration = (Date.now() - new Date(submittedAt).getTime()) / 1000;
              return Number.isFinite(duration) ? duration : null;
            })()
          : null;

      const patchData: Partial<Project> = {
        task_state: taskState,
        status_text: statusText,
        error_text: nextErrorText
      };

      if (taskState === 'SUCCESS') {
        patchData.completed_at = completedAt;
        patchData.duration_seconds = durationSeconds;
      }

      const shouldPatch =
        project.task_state !== taskState ||
        (project.status_text || '') !== statusText ||
        (project.error_text || '') !== nextErrorText ||
        (taskState === 'SUCCESS' && (!project.completed_at || project.duration_seconds === null));

      const next = shouldPatch ? await patch(patchData) : project;

      if (runtimeTask) {
        const taskPatch: Partial<ProjectTask> = {
          task_state: taskState,
          status_text: statusText,
          error_text: nextErrorText
        };
        if (taskState === 'SUCCESS') {
          taskPatch.completed_at = completedAt;
          taskPatch.duration_seconds = durationSeconds;
        }
        const shouldPatchTask =
          runtimeTask.task_state !== taskState ||
          (runtimeTask.status_text || '') !== statusText ||
          (runtimeTask.error_text || '') !== nextErrorText ||
          (taskState === 'SUCCESS' && (!runtimeTask.completed_at || runtimeTask.duration_seconds === null));
        if (shouldPatchTask) {
          await patchTask(runtimeTask.id, taskPatch);
        }
      }

      if (taskState === 'SUCCESS' && next?.task_id) {
        await pullResultForViewer(next.task_id, {
          taskRowId: runtimeTask?.id,
          persistProject: true
        });
      }
    } catch (err) {
      if (!silent) {
        setError(err instanceof Error ? err.message : 'Failed to refresh task status.');
      }
    } finally {
      statusRefreshInFlightRef.current.delete(activeTaskId);
      if (!silent) {
        setStatusLoading(false);
      }
    }
  };

  const submitTask = async () => {
    if (!project || !draft) return;
    const workflow = getWorkflowDefinition(project.task_type);
    if (workflow.key !== 'prediction') {
      setError(`${workflow.title} runner is not wired yet in React UI.`);
      return;
    }

    const normalizedConfig = normalizeConfigForBackend(draft.inputConfig, draft.backend);
    const activeComponents = nonEmptyComponents(normalizedConfig.components);
    const validationError = validateComponents(activeComponents);
    if (validationError) {
      setError(validationError);
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      const { proteinSequence, ligandSmiles } = extractPrimaryProteinAndLigand(normalizedConfig);
      const hasMsa = activeComponents.some((comp) => comp.type === 'protein' && comp.useMsa !== false);
      const activeAssignments = assignChainIdsForComponents(activeComponents);
      const templateUploads: NonNullable<Parameters<typeof submitPrediction>[0]['templateUploads']> = [];
      activeComponents.forEach((comp, index) => {
        if (comp.type !== 'protein') return;
        const template = proteinTemplates[comp.id];
        if (!template) return;
        const targetChainIds = activeAssignments[index] || [];
        const suffix = template.format === 'pdb' ? '.pdb' : '.cif';
        templateUploads.push({
          fileName: `template_${comp.id}${suffix}`,
          format: template.format,
          content: template.content,
          templateChainId: template.chainId,
          targetChainIds
        });
      });
      saveProjectInputConfig(project.id, normalizedConfig);

      const taskId = await submitPrediction({
        projectId: project.id,
        projectName: draft.name,
        proteinSequence,
        ligandSmiles,
        components: activeComponents,
        constraints: normalizedConfig.constraints,
        properties: normalizedConfig.properties,
        seed: normalizedConfig.options.seed,
        backend: draft.backend,
        useMsa: hasMsa,
        templateUploads
      });

      const queuedAt = new Date().toISOString();
      const queuedTaskDraft: ProjectTask = {
        id: `local-${taskId}`,
        project_id: project.id,
        task_id: taskId,
        task_state: 'QUEUED',
        status_text: 'Task submitted and waiting in queue',
        error_text: '',
        backend: draft.backend,
        seed: normalizedConfig.options.seed ?? null,
        protein_sequence: proteinSequence,
        ligand_smiles: ligandSmiles,
        components: activeComponents,
        constraints: normalizedConfig.constraints,
        properties: normalizedConfig.properties,
        confidence: {},
        affinity: {},
        structure_name: '',
        submitted_at: queuedAt,
        completed_at: null,
        duration_seconds: null,
        created_at: queuedAt,
        updated_at: queuedAt
      };
      const persistenceWarnings: string[] = [];
      try {
        const taskRow = await insertProjectTask({
          project_id: project.id,
          task_id: taskId,
          task_state: queuedTaskDraft.task_state,
          status_text: queuedTaskDraft.status_text,
          error_text: queuedTaskDraft.error_text,
          backend: queuedTaskDraft.backend,
          seed: queuedTaskDraft.seed,
          protein_sequence: queuedTaskDraft.protein_sequence,
          ligand_smiles: queuedTaskDraft.ligand_smiles,
          components: queuedTaskDraft.components,
          constraints: queuedTaskDraft.constraints,
          properties: queuedTaskDraft.properties,
          confidence: queuedTaskDraft.confidence,
          affinity: queuedTaskDraft.affinity,
          structure_name: queuedTaskDraft.structure_name,
          submitted_at: queuedTaskDraft.submitted_at,
          completed_at: queuedTaskDraft.completed_at,
          duration_seconds: queuedTaskDraft.duration_seconds
        });
        setProjectTasks((prev) => sortProjectTasks([taskRow, ...prev.filter((row) => row.id !== taskRow.id)]));
      } catch (taskPersistError) {
        setProjectTasks((prev) => sortProjectTasks([queuedTaskDraft, ...prev.filter((row) => row.task_id !== taskId)]));
        persistenceWarnings.push(
          `saving task history failed: ${taskPersistError instanceof Error ? taskPersistError.message : 'unknown error'}`
        );
      }

      const dbPayload: Partial<Project> = {
        name: draft.name.trim(),
        summary: draft.summary.trim(),
        backend: draft.backend,
        use_msa: hasMsa,
        protein_sequence: proteinSequence,
        ligand_smiles: ligandSmiles,
        color_mode: draft.color_mode,
        task_id: taskId,
        task_state: 'QUEUED',
        status_text: 'Task submitted and waiting in queue',
        error_text: '',
        submitted_at: queuedAt,
        completed_at: null,
        duration_seconds: null
      };

      try {
        await patch(dbPayload);
      } catch (dbError) {
        // Keep runtime workflow alive even if persistence is temporarily unavailable.
        setProject((prev) =>
          prev
            ? {
                ...prev,
                ...dbPayload
              }
            : prev
        );
        persistenceWarnings.push(
          `saving project state failed: ${dbError instanceof Error ? dbError.message : 'unknown error'}`
        );
      }
      if (persistenceWarnings.length > 0) {
        setError(`Prediction submitted (task: ${taskId}), but ${persistenceWarnings.join('; ')}.`);
      }
      void loadProjectTasks(project.id, { silent: true });
      setStatusInfo(null);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to submit prediction.';
      setError(message);
    } finally {
      setSubmitting(false);
    }
  };

  useEffect(() => {
    if (!project?.task_id) return;
    if (!['QUEUED', 'RUNNING'].includes(project.task_state)) return;

    const timer = setInterval(() => {
      void refreshStatus({ silent: true });
    }, 4000);

    return () => clearInterval(timer);
  }, [project?.task_id, project?.task_state, projectTasks]);

  useEffect(() => {
    if (!project?.task_id) return;
    if (project.task_state !== 'SUCCESS') return;
    if (structureText) return;
    const runtimeTask = projectTasks.find((item) => item.task_id === project.task_id);
    void pullResultForViewer(project.task_id, {
      taskRowId: runtimeTask?.id,
      persistProject: true
    });
  }, [project?.task_id, project?.task_state, projectTasks, structureText]);

  useEffect(() => {
    if (workspaceTab !== 'constraints') return;
    if (!activeConstraintId && selectedContactConstraintIds.length === 0) return;

    const onGlobalPointerDown = (event: globalThis.PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) {
        setActiveConstraintId(null);
        setSelectedContactConstraintIds([]);
        constraintSelectionAnchorRef.current = null;
        return;
      }

      const keepSelection =
        Boolean(target.closest('.constraint-item')) ||
        Boolean(target.closest('.component-sidebar-link-constraint')) ||
        Boolean(target.closest('.molstar-host')) ||
        Boolean(target.closest('button, a, input, select, textarea, label, [role="button"], [contenteditable="true"]'));

      if (!keepSelection) {
        setActiveConstraintId(null);
        setSelectedContactConstraintIds([]);
        constraintSelectionAnchorRef.current = null;
      }
    };

    document.addEventListener('pointerdown', onGlobalPointerDown, true);
    return () => {
      document.removeEventListener('pointerdown', onGlobalPointerDown, true);
    };
  }, [workspaceTab, activeConstraintId, selectedContactConstraintIds.length]);
  const currentDraftFingerprint = useMemo(() => (draft ? createDraftFingerprint(draft) : ''), [draft]);
  const isDraftDirty = useMemo(
    () => Boolean(draft) && Boolean(savedDraftFingerprint) && currentDraftFingerprint !== savedDraftFingerprint,
    [draft, savedDraftFingerprint, currentDraftFingerprint]
  );

  useEffect(() => {
    if (!runMenuOpen) return;
    if (isDraftDirty && !submitting && !saving) return;
    setRunMenuOpen(false);
  }, [runMenuOpen, isDraftDirty, submitting, saving]);

  useEffect(() => {
    if (!runMenuOpen) return;
    const onPointerDown = (event: globalThis.PointerEvent) => {
      if (!runActionRef.current) return;
      if (runActionRef.current.contains(event.target as Node)) return;
      setRunMenuOpen(false);
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') {
        setRunMenuOpen(false);
      }
    };
    document.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [runMenuOpen]);

  if (loading) {
    return <div className="centered-page">Loading project...</div>;
  }

  if (error && !project) {
    return (
      <div className="page-grid">
        <div className="alert error">{error}</div>
        <Link className="btn btn-ghost" to="/projects">
          Back to projects
        </Link>
      </div>
    );
  }

  if (!project || !draft) {
    return null;
  }

  const workflow = getWorkflowDefinition(project.task_type);
  const isPredictionWorkflow = workflow.key === 'prediction';
  const runDisabled = submitting || saving || !isPredictionWorkflow;
  const handleRunAction = () => {
    if (runDisabled) return;
    if (isDraftDirty) {
      setRunMenuOpen((prev) => !prev);
      return;
    }
    void submitTask();
  };
  const handleRunCurrentDraft = () => {
    setRunMenuOpen(false);
    void submitTask();
  };
  const handleRestoreSavedDraft = () => {
    setRunMenuOpen(false);
    void loadProject();
  };
  const sidebarTypeOrder: InputComponent['type'][] = ['protein', 'ligand', 'dna', 'rna'];
  const formatComponentChainLabel = (chainId: string) => {
    if (!chainId) return '-';
    const info = chainInfoById.get(chainId);
    if (!info) return chainId;
    const bucket = componentTypeBuckets[info.type];
    const entry = bucket.find((item) => item.id === info.componentId);
    if (!entry) return chainId;
    return `${entry.typeLabel} ${entry.typeOrder}`;
  };
  const formatConstraintCombo = (constraint: (typeof draft.inputConfig.constraints)[number]) => {
    if (constraint.type === 'contact') {
      return `${formatComponentChainLabel(constraint.token1_chain)} ↔ ${formatComponentChainLabel(constraint.token2_chain)}`;
    }
    if (constraint.type === 'bond') {
      return `${formatComponentChainLabel(constraint.atom1_chain)} ↔ ${formatComponentChainLabel(constraint.atom2_chain)}`;
    }
    const targetChain = constraint.contacts[0]?.[0] || '';
    return `${formatComponentChainLabel(constraint.binder)} ↔ ${formatComponentChainLabel(targetChain)}`;
  };
  const formatConstraintDetail = (constraint: (typeof draft.inputConfig.constraints)[number]) => {
    if (constraint.type === 'contact') {
      return `${constraint.token1_chain}:${constraint.token1_residue} ↔ ${constraint.token2_chain}:${constraint.token2_residue}`;
    }
    if (constraint.type === 'bond') {
      return `${constraint.atom1_chain}:${constraint.atom1_residue}:${constraint.atom1_atom} ↔ ${constraint.atom2_chain}:${constraint.atom2_residue}:${constraint.atom2_atom}`;
    }
    const first = constraint.contacts[0];
    if (first) {
      return `${constraint.binder} ↔ ${first[0]}:${first[1]}`;
    }
    return `${constraint.binder}`;
  };
  const constraintLabel = (type: string) => {
    if (type === 'contact') return 'Contact';
    if (type === 'bond') return 'Bond';
    if (type === 'pocket') return 'Pocket';
    return 'Constraint';
  };
  const buildDefaultConstraint = (
    preferredType?: 'contact' | 'bond' | 'pocket',
    picked?: { chainId: string; residue: number }
  ): PredictionConstraint => {
    const id = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2);
    const chainA = activeChainInfos[0]?.id || 'A';
    const chainB = activeChainInfos.find((item) => item.id !== chainA)?.id || chainA;
    const firstLigandChain = ligandChainOptions[0]?.id || null;
    const resolvedType = preferredType || (isAf3Backend ? 'bond' : firstLigandChain ? 'pocket' : 'contact');

    if (resolvedType === 'bond') {
      return {
        id,
        type: 'bond',
        atom1_chain: chainA,
        atom1_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
        atom1_atom: 'CA',
        atom2_chain: picked?.chainId || chainB,
        atom2_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
        atom2_atom: 'CA'
      };
    }

    if (resolvedType === 'pocket') {
      const binder = firstLigandChain || chainA;
      return {
        id,
        type: 'pocket',
        binder,
        contacts:
          picked?.chainId && Number.isFinite(Number(picked?.residue)) && Number(picked?.residue) > 0
            ? [[picked.chainId, Math.floor(Number(picked.residue))]]
            : [],
        max_distance: 6,
        force: true
      };
    }

    return {
      id,
      type: 'contact',
      token1_chain: chainA,
      token1_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
      token2_chain: picked?.chainId || chainB,
      token2_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
      max_distance: 5,
      force: true
    };
  };
  const addComponentToDraft = (type: InputComponent['type']) => {
    const nextComponent = createInputComponent(type);
    setWorkspaceTab('components');
    setActiveComponentId(nextComponent.id);
    setSidebarTypeOpen((prev) => ({ ...prev, [type]: true }));
    setDraft((d) =>
      d
        ? {
            ...d,
            inputConfig: {
              ...d.inputConfig,
              version: 1,
              components: [...d.inputConfig.components, nextComponent]
            }
          }
        : d
    );
  };
  const addConstraintFromSidebar = () => {
    if (!draft) return;
    const next = buildDefaultConstraint(isAf3Backend ? 'bond' : undefined);
    setWorkspaceTab('constraints');
    setSidebarConstraintsOpen(true);
    setActiveConstraintId(next.id);
    if (next.type === 'contact') {
      setSelectedContactConstraintIds([next.id]);
      constraintSelectionAnchorRef.current = next.id;
    } else {
      setSelectedContactConstraintIds([]);
      constraintSelectionAnchorRef.current = null;
    }
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            inputConfig: {
              ...prev.inputConfig,
              constraints: [...prev.inputConfig.constraints, next]
            }
          }
        : prev
    );
  };
  const setAffinityFromSidebar = (enabled: boolean) => {
    const ligand = ligandChainOptions[0]?.id || null;
    const target = targetChainOptions[0]?.id || null;
    if (enabled && (!ligand || !target)) return;
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            inputConfig: {
              ...prev.inputConfig,
              properties: {
                ...prev.inputConfig.properties,
                affinity: enabled,
                target: enabled ? prev.inputConfig.properties.target || target : null,
                ligand: enabled ? prev.inputConfig.properties.ligand || ligand : null,
                binder: enabled ? prev.inputConfig.properties.ligand || ligand : null
              }
            }
          }
        : prev
    );
  };
  const setAffinityChainFromSidebar = (field: 'target' | 'ligand', value: string | null) => {
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            inputConfig: {
              ...prev.inputConfig,
              properties: {
                ...prev.inputConfig.properties,
                [field]: value,
                binder: field === 'ligand' ? value : prev.inputConfig.properties.ligand
              }
            }
          }
        : prev
    );
  };
  const scrollToEditorBlock = (elementId: string) => {
    window.setTimeout(() => {
      const target = document.getElementById(elementId);
      if (!target) return;
      const targetTop = target.getBoundingClientRect().top + window.scrollY - 132;
      window.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
    }, 40);
  };
  const jumpToComponent = (componentId: string) => {
    setWorkspaceTab('components');
    setActiveComponentId(componentId);
    const targetType = normalizedDraftComponents.find((item) => item.id === componentId)?.type;
    if (targetType) {
      setSidebarTypeOpen((prev) => ({ ...prev, [targetType]: true }));
    }
    scrollToEditorBlock(`component-card-${componentId}`);
  };
  const clearConstraintSelection = () => {
    setActiveConstraintId(null);
    setSelectedContactConstraintIds([]);
    constraintSelectionAnchorRef.current = null;
  };
  const selectConstraint = (constraintId: string, options?: { toggle?: boolean; range?: boolean }) => {
    if (!draft) return;
    const constraints = draft.inputConfig.constraints;
    const target = constraints.find((item) => item.id === constraintId);
    if (!target) {
      setActiveConstraintId(constraintId);
      return;
    }
    const suggestedTemplateComponentId = resolveTemplateComponentIdForConstraint(target);
    if (suggestedTemplateComponentId && suggestedTemplateComponentId !== selectedConstraintTemplateComponentId) {
      setSelectedConstraintTemplateComponentId(suggestedTemplateComponentId);
    }
    if (target.type !== 'contact') {
      setActiveConstraintId(constraintId);
      setSelectedContactConstraintIds([]);
      constraintSelectionAnchorRef.current = null;
      return;
    }

    const contactIds = constraints.filter((item) => item.type === 'contact').map((item) => item.id);
    const targetIndex = contactIds.indexOf(constraintId);
    if (targetIndex < 0) {
      setActiveConstraintId(constraintId);
      setSelectedContactConstraintIds([constraintId]);
      constraintSelectionAnchorRef.current = constraintId;
      return;
    }

    if (options?.range) {
      const anchorId =
        constraintSelectionAnchorRef.current && contactIds.includes(constraintSelectionAnchorRef.current)
          ? constraintSelectionAnchorRef.current
          : constraintId;
      const anchorIndex = contactIds.indexOf(anchorId);
      const start = Math.min(anchorIndex, targetIndex);
      const end = Math.max(anchorIndex, targetIndex);
      const rangeIds = contactIds.slice(start, end + 1);
      if (options.toggle) {
        setSelectedContactConstraintIds((prev) => Array.from(new Set([...prev, ...rangeIds])));
      } else {
        setSelectedContactConstraintIds(rangeIds);
      }
      setActiveConstraintId(constraintId);
      constraintSelectionAnchorRef.current = constraintId;
      return;
    }

    if (options?.toggle) {
      setSelectedContactConstraintIds((prev) => {
        const wasSelected = prev.includes(constraintId);
        const nextSelected = wasSelected ? prev.filter((id) => id !== constraintId) : [...prev, constraintId];
        if (nextSelected.length === 0) {
          setActiveConstraintId(null);
        } else if (!wasSelected) {
          setActiveConstraintId(constraintId);
        } else {
          setActiveConstraintId(nextSelected[nextSelected.length - 1] || null);
        }
        return nextSelected;
      });
      constraintSelectionAnchorRef.current = constraintId;
      return;
    }

    setActiveConstraintId(constraintId);
    setSelectedContactConstraintIds([constraintId]);
    constraintSelectionAnchorRef.current = constraintId;
  };
  const jumpToConstraint = (constraintId: string, options?: { toggle?: boolean; range?: boolean }) => {
    setWorkspaceTab('constraints');
    selectConstraint(constraintId, options);
    setSidebarConstraintsOpen(true);
    scrollToEditorBlock(`constraint-card-${constraintId}`);
  };
  const navigateConstraint = (step: -1 | 1) => {
    if (!draft || draft.inputConfig.constraints.length === 0) return;
    const currentIndex = draft.inputConfig.constraints.findIndex((item) => item.id === activeConstraintId);
    const safeIndex = currentIndex >= 0 ? currentIndex : 0;
    const nextIndex =
      (safeIndex + step + draft.inputConfig.constraints.length) % draft.inputConfig.constraints.length;
    selectConstraint(draft.inputConfig.constraints[nextIndex].id);
  };
  const applyPickToSelectedConstraint = (pick: MolstarResiduePick) => {
    const chainExists = activeChainInfos.some((item) => item.id === pick.chainId);
    const selectedTemplateOwnerChain = selectedTemplatePreview
      ? activeChainInfos.find((item) => item.componentId === selectedTemplatePreview.componentId)?.id || null
      : null;
    const fallbackProteinChain =
      selectedTemplateOwnerChain ||
      activeChainInfos.find((item) => item.type === 'protein')?.id ||
      activeChainInfos[0]?.id ||
      pick.chainId ||
      'A';
    // In constraint template viewer, picked chain IDs come from the template file (often "A").
    // Always map them back to the selected protein component chain to avoid cross-protein jumps.
    const resolvedChainId = selectedTemplateOwnerChain || (chainExists ? pick.chainId : fallbackProteinChain);
    const residueLimit = activeChainInfos.find((item) => item.id === resolvedChainId)?.residueCount || 0;
    const pickedResidueNumber = Number(pick.residue);
    if (!Number.isFinite(pickedResidueNumber) || pickedResidueNumber <= 0) {
      return;
    }
    let normalizedResidue = Math.floor(pickedResidueNumber);
    const pickedTemplateChain = String(pick.chainId || '').trim();
    const selectedTemplateChain = String(selectedTemplatePreview?.chainId || '').trim();
    const pickedTemplateMap = pickedTemplateChain ? selectedTemplateResidueIndexMap[pickedTemplateChain] : undefined;
    const selectedTemplateMap = selectedTemplateChain ? selectedTemplateResidueIndexMap[selectedTemplateChain] : undefined;
    const mappedFromPickedChain = pickedTemplateMap?.[normalizedResidue];
    const mappedFromSelectedChain = selectedTemplateMap?.[normalizedResidue];
    const mappedResidueCandidate = [mappedFromPickedChain, mappedFromSelectedChain].find(
      (value) => typeof value === 'number' && Number.isFinite(value) && value > 0
    );
    const mappedResidue =
      typeof mappedResidueCandidate === 'number' && Number.isFinite(mappedResidueCandidate) && mappedResidueCandidate > 0
        ? mappedResidueCandidate
        : normalizedResidue;

    if (selectedTemplatePreview) {
      // Constraint picking is sequence-based; ignore non-protein picks (e.g. ligand/hetero atoms)
      // that cannot be mapped to 1-based sequence indices.
      if (!(typeof mappedResidue === 'number' && Number.isFinite(mappedResidue) && mappedResidue > 0)) {
        return;
      }
      normalizedResidue = Math.floor(mappedResidue);
    } else if (typeof mappedResidue === 'number' && Number.isFinite(mappedResidue) && mappedResidue > 0) {
      normalizedResidue = Math.floor(mappedResidue);
    }
    if (!selectedTemplatePreview && residueLimit > 0 && normalizedResidue > residueLimit) {
      return;
    }
    const nextPicked = {
      chainId: resolvedChainId,
      residue: normalizedResidue,
      atomName: pick.atomName
    };
    setPickedResidue((prev) => {
      if (
        prev &&
        prev.chainId === nextPicked.chainId &&
        prev.residue === nextPicked.residue &&
        (prev.atomName || '') === (nextPicked.atomName || '')
      ) {
        return prev;
      }
      return nextPicked;
    });
    if (!canEdit) return;

    const currentConstraints = draft?.inputConfig.constraints || [];
    if (currentConstraints.length === 0) {
      const created = buildDefaultConstraint(undefined, { chainId: resolvedChainId, residue: normalizedResidue });
      if (created.type === 'contact') {
        constraintPickSlotRef.current[created.id] = 'second';
        setSelectedContactConstraintIds([created.id]);
        constraintSelectionAnchorRef.current = created.id;
      }
      setActiveConstraintId(created.id);
      setDraft((prev) =>
        prev
          ? {
              ...prev,
              inputConfig: {
                ...prev.inputConfig,
                constraints: [...prev.inputConfig.constraints, created]
              }
            }
          : prev
      );
      return;
    }

    setDraft((prev) => {
      if (!prev) return prev;
      const constraints = prev.inputConfig.constraints;
      if (!constraints.length) return prev;
      const activeIndex = activeConstraintId ? constraints.findIndex((item) => item.id === activeConstraintId) : -1;
      const activeConstraint = activeIndex >= 0 ? constraints[activeIndex] : null;
      const selectedContactIdSet = new Set(selectedContactConstraintIds);
      const selectedContactIndexes = constraints.reduce<number[]>((acc, item, index) => {
        if (item.type === 'contact' && selectedContactIdSet.has(item.id)) {
          acc.push(index);
        }
        return acc;
      }, []);

      let targetIndexes: number[] = [];
      if (activeConstraint?.type === 'contact') {
        targetIndexes = selectedContactIndexes.length > 0 ? selectedContactIndexes : activeIndex >= 0 ? [activeIndex] : [];
      } else if (activeIndex >= 0) {
        targetIndexes = [activeIndex];
      } else if (selectedContactIndexes.length > 0) {
        targetIndexes = selectedContactIndexes;
      } else if (constraints.length > 0) {
        targetIndexes = [0];
      }
      if (targetIndexes.length === 0) return prev;
      const targetIndexSet = new Set(targetIndexes);

      let hasChanges = false;
      const nextConstraints = constraints.map((item, index) => {
        if (!targetIndexSet.has(index)) return item;

        if (item.type === 'contact') {
          const token1Matches = item.token1_chain === resolvedChainId;
          const token2Matches = item.token2_chain === resolvedChainId;
          const slot: 'first' | 'second' =
            token1Matches && !token2Matches
              ? 'first'
              : token2Matches && !token1Matches
                ? 'second'
                : constraintPickSlotRef.current[item.id] || 'first';
          const updated =
            slot === 'first'
              ? { ...item, token1_chain: resolvedChainId, token1_residue: normalizedResidue }
              : { ...item, token2_chain: resolvedChainId, token2_residue: normalizedResidue };
          constraintPickSlotRef.current[item.id] = slot === 'first' ? 'second' : 'first';
          if (
            updated.token1_chain !== item.token1_chain ||
            updated.token1_residue !== item.token1_residue ||
            updated.token2_chain !== item.token2_chain ||
            updated.token2_residue !== item.token2_residue
          ) {
            hasChanges = true;
            return updated;
          }
          return item;
        }

        if (item.type === 'bond') {
          const atom1Matches = item.atom1_chain === resolvedChainId;
          const atom2Matches = item.atom2_chain === resolvedChainId;
          const slot: 'first' | 'second' =
            atom1Matches && !atom2Matches
              ? 'first'
              : atom2Matches && !atom1Matches
                ? 'second'
                : constraintPickSlotRef.current[item.id] || 'first';
          const atomName = (pick.atomName || (slot === 'first' ? item.atom1_atom : item.atom2_atom) || 'CA').toUpperCase();
          const updated =
            slot === 'first'
              ? {
                  ...item,
                  atom1_chain: resolvedChainId,
                  atom1_residue: normalizedResidue,
                  atom1_atom: atomName
                }
              : {
                  ...item,
                  atom2_chain: resolvedChainId,
                  atom2_residue: normalizedResidue,
                  atom2_atom: atomName
                };
          constraintPickSlotRef.current[item.id] = slot === 'first' ? 'second' : 'first';
          if (
            updated.atom1_chain !== item.atom1_chain ||
            updated.atom1_residue !== item.atom1_residue ||
            updated.atom1_atom !== item.atom1_atom ||
            updated.atom2_chain !== item.atom2_chain ||
            updated.atom2_residue !== item.atom2_residue ||
            updated.atom2_atom !== item.atom2_atom
          ) {
            hasChanges = true;
            return updated;
          }
          return item;
        }

        const exists = item.contacts.some((contact) => contact[0] === resolvedChainId && contact[1] === normalizedResidue);
        if (exists) return item;
        hasChanges = true;
        return {
          ...item,
          contacts: [...item.contacts, [resolvedChainId, normalizedResidue] as [string, number]]
        };
      });

      if (!hasChanges) return prev;
      return {
        ...prev,
        inputConfig: {
          ...prev.inputConfig,
          constraints: nextConstraints
        }
      };
    });
  };
  const displayStructureText = structureText;
  const displayStructureFormat: 'cif' | 'pdb' = structureFormat;
  const displayStructureName = project.structure_name || '-';
  const confidenceBackend =
    project.confidence && typeof project.confidence.backend === 'string' ? String(project.confidence.backend).toLowerCase() : '';
  const hasAf3ConfidenceSignals = Boolean(
    readFirstFiniteMetric(project.confidence || {}, ['ranking_score', 'fraction_disordered']) !== null ||
      readObjectPath(project.confidence || {}, 'chain_pair_iptm') !== undefined
  );
  const displayStructureColorMode: 'white' | 'alphafold' =
    project.backend === 'alphafold3' ||
    draft.color_mode === 'alphafold' ||
    confidenceBackend === 'alphafold3' ||
    hasAf3ConfidenceSignals
      ? 'alphafold'
      : 'white';
  const constraintStructureText = selectedTemplatePreview?.content || '';
  const constraintStructureFormat: 'cif' | 'pdb' = selectedTemplatePreview?.format || 'pdb';
  const hasConstraintStructure = Boolean(constraintStructureText.trim());
  const affinityTargetLabel = formatComponentChainLabel(draft.inputConfig.properties.target || '');
  const affinityLigandLabel = formatComponentChainLabel(draft.inputConfig.properties.ligand || '');
  const affinitySummary =
    draft.inputConfig.properties.affinity && draft.inputConfig.properties.target && draft.inputConfig.properties.ligand
      ? `${affinityTargetLabel} ↔ ${affinityLigandLabel}`
      : 'off';
  const snapshotCards: Array<{ key: string; label: string; value: string; detail: string; tone: MetricTone }> = [
    {
      key: 'plddt',
      label: 'pLDDT',
      value: snapshotPlddt === null ? '-' : snapshotPlddt.toFixed(2),
      detail: 'Complex confidence',
      tone: snapshotPlddtTone
    },
    {
      key: 'iptm',
      label: 'ipTM',
      value: snapshotIptm === null ? '-' : snapshotIptm.toFixed(4),
      detail: 'Interface confidence',
      tone: snapshotIptmTone
    },
    {
      key: 'ic50',
      label: 'IC50 (uM)',
      value: snapshotIc50Um === null ? '-' : snapshotIc50Um >= 10 ? snapshotIc50Um.toFixed(1) : snapshotIc50Um.toFixed(2),
      detail: snapshotIc50Error
        ? `+${snapshotIc50Error.plus >= 1 ? snapshotIc50Error.plus.toFixed(2) : snapshotIc50Error.plus.toFixed(3)} / -${
            snapshotIc50Error.minus >= 1 ? snapshotIc50Error.minus.toFixed(2) : snapshotIc50Error.minus.toFixed(3)
          }`
        : 'uncertainty: -',
      tone: snapshotIc50Tone
    },
    {
      key: 'binding',
      label: 'Binding',
      value: snapshotBindingProbability === null ? '-' : `${(snapshotBindingProbability * 100).toFixed(1)}%`,
      detail: snapshotBindingStd === null ? 'uncertainty: -' : `± ${(snapshotBindingStd * 100).toFixed(1)}%`,
      tone: snapshotBindingTone
    }
  ];

  return (
    <div className="page-grid project-detail">
      <section className="page-header">
        <div className="page-header-left">
          <h1>{project.name}</h1>
          <div className="project-compact-meta">
            <span className={`badge state-${project.task_state.toLowerCase()}`}>{project.task_state}</span>
            <span className="meta-chip">{workflow.shortTitle}</span>
            {isActiveRuntime ? (
              <>
                <span
                  className={`meta-chip meta-chip-live meta-chip-live-progress ${
                    project.task_state === 'RUNNING' ? 'meta-chip-live-running' : 'meta-chip-live-queued'
                  }`}
                >
                  {Math.round(progressPercent)}%
                </span>
                {waitingSeconds !== null && (
                  <span
                    className={`meta-chip meta-chip-live meta-chip-live-elapsed ${
                      project.task_state === 'RUNNING' ? 'meta-chip-live-running' : 'meta-chip-live-queued'
                    }`}
                  >
                    {formatDuration(waitingSeconds)} elapsed
                  </span>
                )}
              </>
            ) : (
              project.task_state === 'SUCCESS' &&
              totalRuntimeSeconds !== null && <span className="meta-chip">Completed in {formatDuration(totalRuntimeSeconds)}</span>
            )}
          </div>
        </div>

        <div className="row gap-8 page-header-actions">
          <div className="detail-tabs detail-tabs-inline">
            <button
              type="button"
              className={`detail-tab ${workspaceTab === 'basics' ? 'active' : ''}`}
              onClick={() => setWorkspaceTab('basics')}
              aria-label="Edit basics"
            >
              <SlidersHorizontal size={13} />
              Basics
            </button>
            {isPredictionWorkflow && (
              <>
                <button
                  type="button"
                  className={`detail-tab ${workspaceTab === 'components' ? 'active' : ''}`}
                  onClick={() => setWorkspaceTab('components')}
                  aria-label="Edit components"
                >
                  <Dna size={13} />
                  Components
                </button>
                <button
                  type="button"
                  className={`detail-tab ${workspaceTab === 'constraints' ? 'active' : ''}`}
                  onClick={() => setWorkspaceTab('constraints')}
                  aria-label="Edit constraints"
                >
                  <Target size={13} />
                  Constraints
                </button>
              </>
            )}
            <button
              type="button"
              className={`detail-tab ${workspaceTab === 'results' ? 'active' : ''}`}
              onClick={() => setWorkspaceTab('results')}
              aria-label="View current result"
            >
              <Eye size={13} />
              Current Result
            </button>
          </div>
          <div className="toolbar-divider" />
          <Link className="btn btn-ghost btn-compact" to={`/projects/${project.id}/tasks`} title="Open task history">
            <Clock3 size={14} />
            Tasks
          </Link>
          <button
            className="btn btn-ghost btn-compact btn-square"
            onClick={() => void refreshStatus()}
            disabled={statusLoading}
            title="Refresh status"
            aria-label="Refresh status"
          >
            <RefreshCcw size={15} className={statusLoading ? 'spin' : undefined} />
          </button>
          <button
            className="btn btn-ghost btn-compact btn-square"
            onClick={() => project.task_id && void downloadResultFile(project.task_id)}
            disabled={!project.task_id}
            title="Download result"
            aria-label="Download result"
          >
            <Download size={15} />
          </button>
          <button
            className="btn btn-ghost btn-compact btn-square"
            type="button"
            onClick={() => void saveDraft()}
            disabled={!canEdit || saving || !isDraftDirty}
            title={saving ? 'Saving draft' : isDraftDirty ? 'Save draft' : 'Draft saved'}
            aria-label={saving ? 'Saving draft' : isDraftDirty ? 'Save draft' : 'Draft saved'}
          >
            {saving ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}
          </button>
          <div className="run-action" ref={runActionRef}>
            <button
              className="btn btn-primary btn-compact btn-square run-btn-primary"
              type="button"
              onClick={handleRunAction}
              disabled={runDisabled}
              title={
                submitting
                  ? 'Submitting'
                  : isDraftDirty
                    ? `${workflow.runLabel} (has unsaved changes)`
                    : isPredictionWorkflow
                      ? workflow.runLabel
                      : 'Runner UI for this workflow is being integrated.'
              }
              aria-label={
                submitting
                  ? 'Submitting'
                  : isPredictionWorkflow
                    ? workflow.runLabel
                    : 'Run'
              }
              aria-haspopup={isDraftDirty ? 'menu' : undefined}
              aria-expanded={isDraftDirty ? runMenuOpen : undefined}
            >
              {submitting ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}
            </button>
            {runMenuOpen && isDraftDirty && (
              <div className="run-action-menu" role="menu" aria-label="Run options">
                <button
                  type="button"
                  className="run-action-item"
                  onClick={handleRestoreSavedDraft}
                  disabled={loading || saving || submitting}
                >
                  Restore Saved
                </button>
                <button
                  type="button"
                  className="run-action-item primary"
                  onClick={handleRunCurrentDraft}
                  disabled={loading || saving || submitting}
                >
                  Run Current
                </button>
              </div>
            )}
          </div>
        </div>
      </section>

      {(error || resultError) && <div className="alert error">{error || resultError}</div>}

      {workspaceTab === 'results' && isPredictionWorkflow && (
        <>
          <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
            <section className="panel structure-panel">
              <h2>Structure Viewer</h2>

              <MolstarViewer structureText={displayStructureText} format={displayStructureFormat} colorMode={displayStructureColorMode} />

              <span className="muted small">Current structure file: {displayStructureName}</span>
            </section>

            <div
              className={`results-resizer ${isResultsResizing ? 'dragging' : ''}`}
              role="separator"
              aria-orientation="vertical"
              aria-label="Resize structure and overview panels"
              tabIndex={0}
              onPointerDown={handleResultsResizerPointerDown}
              onKeyDown={handleResultsResizerKeyDown}
            />

            <aside className="panel info-panel">
              <h2>Overview</h2>

              <section className="result-aside-block result-aside-block-ligand">
                <div className="result-aside-title">Ligand</div>
                <div className="ligand-preview-panel">
                  {primaryLigandValue && primaryLigandIsSmiles ? (
                    <Ligand2DPreview smiles={primaryLigandValue} />
                  ) : (
                    <div className="ligand-preview-empty">
                      {primaryLigandValue ? '2D preview requires SMILES input.' : 'No ligand input.'}
                    </div>
                  )}
                </div>
                <LigandPropertyGrid smiles={primaryLigandIsSmiles ? primaryLigandValue : ''} />
              </section>

              <section className="result-aside-block">
                <div className="result-aside-title">Model Signals</div>
                <div className="overview-signal-list">
                  {snapshotCards.map((card) => (
                    <div key={card.key} className={`overview-signal-row tone-${card.tone}`}>
                      <div className="overview-signal-main">
                        <span className="overview-signal-label">{card.label}</span>
                        <span className="overview-signal-detail">{card.detail}</span>
                      </div>
                      <strong className={`overview-signal-value metric-value-${card.tone}`}>{card.value}</strong>
                    </div>
                  ))}
                </div>
              </section>
            </aside>
          </div>

          <div className="results-bottom">
            <MetricsPanel title="Confidence" data={project.confidence || {}} chainIds={activeChainIds} />
          </div>
        </>
      )}

      {workspaceTab === 'results' && !isPredictionWorkflow && (
        <section className="panel">
          <h2>{workflow.title}</h2>
          <p className="muted">
            This project is set to <strong>{workflow.shortTitle}</strong>. Configure workflow-specific parameters in Basics.
          </p>
          <div className="status-stats">
            <div className="status-stat">
              <span className="muted small">Workflow</span>
              <strong>{workflow.title}</strong>
            </div>
            <div className="status-stat">
              <span className="muted small">Current State</span>
              <strong>{project.task_state}</strong>
            </div>
            <div className="status-stat">
              <span className="muted small">Task ID</span>
              <strong>{project.task_id || '-'}</strong>
            </div>
          </div>
        </section>
      )}

      {workspaceTab !== 'results' && (
        <section className="panel inputs-panel">
          <h2>{workspaceTab === 'constraints' ? 'Constraints' : workspaceTab === 'components' ? 'Components' : 'Basics'}</h2>

          <form className="form-grid" onSubmit={saveDraft}>
            {workspaceTab === 'basics' && (
              <section className="panel subtle basics-panel">
                <label className="field">
                  <span>
                    Project Name <span className="required-mark">*</span>
                  </span>
                  <input
                    required
                    value={draft.name}
                    onChange={(e) => setDraft((d) => (d ? { ...d, name: e.target.value } : d))}
                    disabled={!canEdit}
                  />
                </label>

                <label className="field">
                  <span>Summary</span>
                  <textarea
                    value={draft.summary}
                    rows={3}
                    onChange={(e) => setDraft((d) => (d ? { ...d, summary: e.target.value } : d))}
                    disabled={!canEdit}
                  />
                </label>

                <label className="field">
                  <span>
                    Backend <span className="required-mark">*</span>
                  </span>
                  <select
                    required
                    value={draft.backend}
                    onChange={(e) =>
                      setDraft((d) =>
                        d
                          ? {
                              ...d,
                              backend: e.target.value,
                              inputConfig: {
                                ...d.inputConfig,
                                constraints: filterConstraintsByBackend(d.inputConfig.constraints, e.target.value)
                              }
                            }
                          : d
                      )
                    }
                    disabled={!canEdit}
                  >
                    <option value="boltz">Boltz-2</option>
                    <option value="alphafold3">AlphaFold3</option>
                  </select>
                </label>

                {isPredictionWorkflow && (
                  <label className="field">
                    <span>Random Seed (optional)</span>
                    <input
                      type="number"
                      min={0}
                      value={draft.inputConfig.options.seed ?? ''}
                      onChange={(e) =>
                        setDraft((d) =>
                          d
                            ? {
                                ...d,
                                inputConfig: {
                                  ...d.inputConfig,
                                  options: {
                                    ...d.inputConfig.options,
                                    seed: e.target.value === '' ? null : Math.max(0, Math.floor(Number(e.target.value) || 0))
                                  }
                                }
                              }
                            : d
                        )
                      }
                      disabled={!canEdit}
                      placeholder="Default: 42"
                    />
                  </label>
                )}
              </section>
            )}

            {isPredictionWorkflow ? (
              <>
                {workspaceTab !== 'basics' && (
                  <div
                    ref={workspaceTab === 'components' ? componentsWorkspaceRef : null}
                    className={`inputs-workspace ${workspaceTab === 'constraints' ? 'constraints-focus' : ''} ${
                      workspaceTab === 'components' ? `components-resizable ${isComponentsResizing ? 'is-resizing' : ''}` : ''
                    }`}
                    style={workspaceTab === 'components' ? componentsGridStyle : undefined}
                  >
                  <div className="inputs-main">
                    {workspaceTab === 'components' && (
                      <>
                        <ComponentInputEditor
                          components={draft.inputConfig.components}
                          onChange={(components) =>
                            setDraft((d) =>
                              d
                                ? {
                                    ...d,
                                    inputConfig: {
                                      ...d.inputConfig,
                                      version: 1,
                                      components
                                    }
                                  }
                                : d
                            )
                          }
                          proteinTemplates={proteinTemplates}
                          onProteinTemplateChange={(componentId, upload) => {
                            setPickedResidue(null);
                            setProteinTemplates((prev) => {
                              const next = { ...prev };
                              if (upload) {
                                next[componentId] = upload;
                              } else {
                                delete next[componentId];
                              }
                              return next;
                            });
                          }}
                          selectedComponentId={activeComponentId}
                          onSelectedComponentIdChange={setActiveComponentId}
                          showQuickAdd={false}
                          compact
                          renderProteinTemplateViewer={({ upload }) => (
                            <section className="component-template-inline">
                              <MolstarViewer
                                structureText={upload.content}
                                format={upload.format}
                                colorMode="white"
                                showSequence={false}
                                pickMode="alt-left"
                                onResiduePick={(pick: MolstarResiduePick) =>
                                  setPickedResidue({
                                    chainId: pick.chainId,
                                    residue: pick.residue,
                                    atomName: pick.atomName
                                  })
                                }
                              />
                            </section>
                          )}
                          disabled={!canEdit}
                        />
                      </>
                    )}

                    {workspaceTab === 'constraints' && (
                      <div
                        ref={constraintsWorkspaceRef}
                        className={`constraint-workspace resizable ${isConstraintsResizing ? 'is-resizing' : ''}`}
                        style={constraintsGridStyle}
                      >
                        <section className="panel subtle constraint-viewer-panel">
                          <div className="constraint-nav-bar">
                            <div className="constraint-nav-title-row">
                              <h3>Constraint Picker</h3>
                              <span className="muted small constraint-nav-counter">
                                {constraintCount === 0
                                  ? 'No constraints'
                                  : `${activeConstraintIndex >= 0 ? activeConstraintIndex + 1 : 0}/${constraintCount}`}
                              </span>
                            </div>
                            <div className="constraint-nav-controls">
                              {constraintTemplateOptions && constraintTemplateOptions.length > 0 && (
                                <label className="constraint-template-switch">
                                  <select
                                    aria-label="Select protein template for constraint viewer"
                                    value={selectedTemplatePreview?.componentId || ''}
                                    onChange={(e) => setSelectedConstraintTemplateComponentId(e.target.value || null)}
                                  >
                                    {constraintTemplateOptions.map((item) => (
                                      <option key={`constraint-template-${item.componentId}`} value={item.componentId}>
                                        {item.label} - {item.fileName} (chain {item.chainId})
                                      </option>
                                    ))}
                                  </select>
                                </label>
                              )}
                              <div className="constraint-nav-actions">
                                <button
                                  type="button"
                                  className={`btn btn-compact ${constraintPickModeEnabled ? 'btn-primary' : 'btn-ghost'}`}
                                  onClick={() => setConstraintPickModeEnabled((prev) => !prev)}
                                  disabled={!canEdit}
                                >
                                  {constraintPickModeEnabled ? 'Pick: On' : 'Pick: Off'}
                                </button>
                                <button
                                  type="button"
                                  className="btn btn-ghost btn-compact"
                                  onClick={() => setWorkspaceTab('components')}
                                >
                                  <ArrowLeft size={14} />
                                  Components
                                </button>
                                <button
                                  type="button"
                                  className="btn btn-ghost btn-compact"
                                  onClick={() => navigateConstraint(-1)}
                                  disabled={constraintCount <= 1}
                                >
                                  Prev
                                </button>
                                <button
                                  type="button"
                                  className="btn btn-ghost btn-compact"
                                  onClick={() => navigateConstraint(1)}
                                  disabled={constraintCount <= 1}
                                >
                                  Next
                                </button>
                              </div>
                            </div>
                          </div>
                          <div className="row between">
                            <span className="muted small">
                              {constraintPickModeEnabled
                                ? 'Pick Mode is on: left click in Mol* to auto-fill the selected constraint.'
                                : 'Enable Pick Mode to start selecting residues from Mol*.'}
                            </span>
                            {pickedResidue && <span className="muted small">Picked: {pickedResidue.chainId}:{pickedResidue.residue}</span>}
                          </div>
                          {hasConstraintStructure ? (
                            <MolstarViewer
                              key={`constraint-viewer-${selectedTemplatePreview?.componentId || 'none'}-${selectedTemplatePreview?.chainId || 'none'}`}
                              structureText={constraintStructureText}
                              format={constraintStructureFormat}
                              colorMode="white"
                              pickMode="click"
                              highlightResidues={constraintViewerHighlightResidues}
                              activeResidue={constraintViewerActiveResidue}
                              lockView={constraintPickModeEnabled}
                              suppressAutoFocus={constraintPickModeEnabled}
                              onResiduePick={
                                constraintPickModeEnabled
                                  ? (pick: MolstarResiduePick) => {
                                      applyPickToSelectedConstraint(pick);
                                    }
                                  : undefined
                              }
                            />
                          ) : (
                            <div className="constraint-viewer-empty muted small">
                              Upload a protein template in Components to enable Mol* picking for constraints.
                            </div>
                          )}
                        </section>

                        <div
                          className={`panel-resizer ${isConstraintsResizing ? 'dragging' : ''}`}
                          role="separator"
                          aria-orientation="vertical"
                          aria-label="Resize constraint picker and constraints panels"
                          tabIndex={0}
                          onPointerDown={handleConstraintsResizerPointerDown}
                          onKeyDown={handleConstraintsResizerKeyDown}
                        />

                        <section
                          className="panel subtle constraint-editor-panel"
                          onClick={(event) => {
                            if (event.target === event.currentTarget) {
                              clearConstraintSelection();
                            }
                          }}
                        >
                          <ConstraintEditor
                            components={draft.inputConfig.components}
                            constraints={draft.inputConfig.constraints}
                            properties={draft.inputConfig.properties}
                            pickedResidue={pickedResidue}
                            selectedConstraintId={activeConstraintId}
                            selectedConstraintIds={selectedContactConstraintIds}
                            onSelectedConstraintIdChange={(id) => {
                              if (id) {
                                selectConstraint(id);
                              } else {
                                clearConstraintSelection();
                              }
                            }}
                            onConstraintClick={(id, options) =>
                              selectConstraint(id, {
                                toggle: Boolean(options?.toggle),
                                range: Boolean(options?.range)
                              })
                            }
                            onClearSelection={clearConstraintSelection}
                            showAffinitySection={false}
                            allowedConstraintTypes={allowedConstraintTypes}
                            compatibilityHint={
                              isAf3Backend ? 'AlphaFold3 backend currently supports Bond constraints only.' : undefined
                            }
                            onConstraintsChange={(constraints) =>
                              setDraft((d) =>
                                d
                                  ? {
                                      ...d,
                                      inputConfig: {
                                        ...d.inputConfig,
                                        constraints: filterConstraintsByBackend(constraints, d.backend)
                                      }
                                    }
                                  : d
                              )
                            }
                            onPropertiesChange={(properties) =>
                              setDraft((d) =>
                                d
                                  ? {
                                      ...d,
                                      inputConfig: {
                                        ...d.inputConfig,
                                        properties
                                      }
                                    }
                                  : d
                              )
                            }
                            disabled={!canEdit}
                          />
                        </section>
                      </div>
                    )}
                  </div>

                  {workspaceTab === 'components' && (
                    <div
                      className={`panel-resizer ${isComponentsResizing ? 'dragging' : ''}`}
                      role="separator"
                      aria-orientation="vertical"
                      aria-label="Resize components and workspace panels"
                      tabIndex={0}
                      onPointerDown={handleComponentsResizerPointerDown}
                      onKeyDown={handleComponentsResizerKeyDown}
                    />
                  )}

                  {workspaceTab === 'components' && (
                    <aside className="panel subtle component-sidebar">
                      <div className="component-sidebar-head">
                        <h3>Workspace</h3>
                        <span className="muted small">{draft.inputConfig.components.length} items</span>
                      </div>
                      {sidebarTypeOrder.map((type) => {
                        const bucket = componentTypeBuckets[type];
                        return (
                          <section className="component-sidebar-section" key={`sidebar-type-${type}`}>
                            <div className="component-tree-row">
                              <button
                                type="button"
                                className="component-sidebar-toggle"
                                onClick={() => setSidebarTypeOpen((prev) => ({ ...prev, [type]: !prev[type] }))}
                              >
                                <span className="component-tree-label">
                                  {sidebarTypeOpen[type] ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                  {type === 'protein' ? (
                                    <Dna size={13} />
                                  ) : type === 'ligand' ? (
                                    <FlaskConical size={13} />
                                  ) : (
                                    <Dna size={13} />
                                  )}
                                  <strong>{componentTypeLabel(type)}</strong>
                                </span>
                                <span className="muted small">{bucket.length}</span>
                              </button>
                              <button
                                type="button"
                                className="icon-btn component-tree-add"
                                onClick={() => addComponentToDraft(type)}
                                disabled={!canEdit}
                                title={`Add ${componentTypeLabel(type)}`}
                              >
                                <Plus size={14} />
                              </button>
                            </div>

                            {sidebarTypeOpen[type] && (
                              <div className="component-sidebar-list component-sidebar-list-components">
                                {bucket.length === 0 ? (
                                  <div className="component-sidebar-empty muted small">No {componentTypeLabel(type).toLowerCase()} yet.</div>
                                ) : (
                                  bucket.map((entry) => (
                                    <button
                                      key={entry.id}
                                      type="button"
                                      className={`component-sidebar-link ${activeComponentId === entry.id ? 'active' : ''}`}
                                      onClick={() => jumpToComponent(entry.id)}
                                    >
                                      <span>
                                        {entry.typeLabel} {entry.typeOrder}
                                      </span>
                                      <span className="muted small component-sidebar-link-meta">
                                        Comp {entry.globalOrder} {entry.filled ? '' : '· empty'}
                                      </span>
                                    </button>
                                  ))
                                )}
                              </div>
                            )}
                          </section>
                        );
                      })}

                      <section className="component-sidebar-section">
                        <div className="component-tree-row">
                          <button
                            type="button"
                            className="component-sidebar-toggle"
                            onClick={() => setSidebarConstraintsOpen((v) => !v)}
                          >
                            <span className="component-tree-label">
                              {sidebarConstraintsOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                              <strong>Constraints</strong>
                            </span>
                            <span className="muted small">{constraintCount}</span>
                          </button>
                          <button
                            type="button"
                            className="icon-btn component-tree-add"
                            onClick={addConstraintFromSidebar}
                            disabled={!canEdit || activeChainInfos.length === 0}
                            title="Add constraint"
                          >
                            <Plus size={14} />
                          </button>
                        </div>
                        {sidebarConstraintsOpen && (
                          <div className="component-sidebar-list component-sidebar-list-nested">
                            {draft.inputConfig.constraints.length === 0 ? (
                              <div className="component-sidebar-empty muted small">No constraints yet.</div>
                            ) : (
                              draft.inputConfig.constraints.map((constraint, index) => (
                                <button
                                  key={constraint.id}
                                  type="button"
                                  className={`component-sidebar-link component-sidebar-link-constraint ${
                                    activeConstraintId === constraint.id || selectedContactConstraintIdSet.has(constraint.id)
                                      ? 'active'
                                      : ''
                                  }`}
                                  onClick={(event) =>
                                    jumpToConstraint(constraint.id, {
                                      toggle: event.metaKey || event.ctrlKey,
                                      range: event.shiftKey
                                    })
                                  }
                                >
                                  <span>{`${index + 1}. ${constraintLabel(constraint.type)} · ${formatConstraintCombo(constraint)}`}</span>
                                  <span className="muted small">{formatConstraintDetail(constraint)}</span>
                                </button>
                              ))
                            )}
                          </div>
                        )}
                      </section>

                      <section className="component-sidebar-section">
                        <div className="component-tree-row">
                          <button type="button" className="component-sidebar-toggle" onClick={() => setWorkspaceTab('constraints')}>
                            <span className="component-tree-label">
                              <Target size={13} />
                              <strong>Binding</strong>
                            </span>
                            <span className="muted small">{affinitySummary}</span>
                          </button>
                          <button
                            type="button"
                            className="icon-btn component-tree-add"
                            onClick={() => setAffinityFromSidebar(!draft.inputConfig.properties.affinity)}
                            disabled={!canEdit || ligandChainOptions.length === 0 || targetChainOptions.length === 0}
                            title={
                              ligandChainOptions.length === 0
                                ? 'Need at least one ligand component'
                                : draft.inputConfig.properties.affinity
                                  ? 'Disable binding objective'
                                  : 'Enable binding objective'
                            }
                          >
                            {draft.inputConfig.properties.affinity ? <ChevronDown size={14} /> : <Plus size={14} />}
                          </button>
                        </div>
                        {draft.inputConfig.properties.affinity ? (
                          <div className="component-sidebar-list component-sidebar-list-nested affinity-sidebar-list">
                            <label className="field affinity-field">
                              <span className="affinity-key">Target</span>
                              <select
                                value={draft.inputConfig.properties.target || ''}
                                disabled={!canEdit}
                                onChange={(e) => setAffinityChainFromSidebar('target', e.target.value || null)}
                              >
                                {targetChainOptions.map((item) => (
                                  <option key={`affinity-target-${item.id}`} value={item.id}>
                                    {item.id} · {componentTypeLabel(item.type)}
                                  </option>
                                ))}
                              </select>
                            </label>
                            <label className="field affinity-field">
                              <span className="affinity-key">Ligand</span>
                              <select
                                value={draft.inputConfig.properties.ligand || ''}
                                disabled={!canEdit || ligandChainOptions.length === 0}
                                onChange={(e) => setAffinityChainFromSidebar('ligand', e.target.value || null)}
                              >
                                {ligandChainOptions.map((item) => (
                                  <option key={`affinity-ligand-${item.id}`} value={item.id}>
                                    {item.id} · Ligand
                                  </option>
                                ))}
                              </select>
                            </label>
                          </div>
                        ) : (
                          <div className="component-sidebar-empty muted small">
                            {ligandChainOptions.length === 0
                              ? 'Add at least one ligand component to enable binding objective.'
                              : 'Click + to enable binding objective.'}
                          </div>
                        )}
                      </section>

                      <div className="component-sidebar-note muted small">Click items to jump to the target editor block.</div>
                    </aside>
                  )}
                  </div>
                )}
              </>
            ) : (
              <div className="workflow-note">
                {workflow.description}
              </div>
            )}
          </form>
        </section>
      )}
    </div>
  );
}
