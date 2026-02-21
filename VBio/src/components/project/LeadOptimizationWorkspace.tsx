import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import { LoaderCircle } from 'lucide-react';
import { type LigandFragmentItem } from './LigandFragmentSketcher';
import { type MolstarAtomHighlight, type MolstarResidueHighlight } from './MolstarViewer';
import {
  LeadOptCandidatesPanel,
  normalizeLeadOptCandidatesUiState,
  type CandidatePreviewRenderMode,
  type LeadOptCandidatesUiState
} from './leadopt/LeadOptCandidatesPanel';
import { LeadOptFragmentPanel } from './leadopt/LeadOptFragmentPanel';
import { LeadOptMolstarViewer } from './leadopt/LeadOptMolstarViewer';
import { LeadOptReferencePanel } from './leadopt/LeadOptReferencePanel';
import {
  useLeadOptMmpQueryMachine,
  type LeadOptMmpPersistedSnapshot,
  type LeadOptPredictionRecord
} from './leadopt/hooks/useLeadOptMmpQueryMachine';
import {
  fetchLeadOptimizationMmpDatabases,
  type LeadOptMmpDatabaseItem
} from '../../api/backendApi';
import { useLeadOptMmpQueryForm } from './leadopt/hooks/useLeadOptMmpQueryForm';
import { inferQueryModeFromSelection } from './leadopt/hooks/fragmentVariableSelection';
import {
  useLeadOptReferenceFragment,
  type LeadOptPersistedUploads,
  type PocketResidue
} from './leadopt/hooks/useLeadOptReferenceFragment';

interface LeadOptimizationWorkspaceProps {
  viewMode?: 'reference' | 'design';
  canEdit: boolean;
  submitting: boolean;
  backend: string;
  onNavigateToResults?: () => void;
  onRegisterHeaderRunAction?: (action: (() => void) | null) => void;
  proteinSequence: string;
  ligandSmiles: string;
  targetChain: string;
  ligandChain: string;
  onLigandSmilesChange: (value: string) => void;
  referenceScopeKey?: string;
  persistedReferenceUploads?: LeadOptPersistedUploads;
  onReferenceUploadsChange?: (uploads: LeadOptPersistedUploads) => void;
  onMmpTaskQueued?: (payload: {
    taskId: string;
    requestPayload: Record<string, unknown>;
    querySmiles: string;
    referenceUploads: LeadOptPersistedUploads;
  }) => void | Promise<void>;
  onMmpTaskCompleted?: (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => void | Promise<void>;
  onMmpTaskFailed?: (payload: { taskId: string; error: string }) => void | Promise<void>;
  initialMmpSnapshot?: LeadOptMmpPersistedSnapshot | null;
  onMmpUiStateChange?: (payload: { uiState: LeadOptCandidatesUiState }) => void | Promise<void>;
  onPredictionQueued?: (payload: { taskId: string; backend: string; candidateSmiles: string }) => void | Promise<void>;
  onPredictionStateChange?: (payload: {
    records: Record<string, LeadOptPredictionRecord>;
    referenceRecords: Record<string, LeadOptPredictionRecord>;
    summary: {
      total: number;
      queued: number;
      running: number;
      success: number;
      failure: number;
      latestTaskId: string;
    };
  }) => void | Promise<void>;
}

const LEFT_PANEL_MIN = 320;
const RIGHT_PANEL_MIN = 300;
const RESIZER_WIDTH = 10;
const LEFT_PANEL_KEY_STEP = 24;
const LEFT_PANEL_DEFAULT = 760;
const RESULT_VIEWER_MIN = 340;
const RESULT_MAIN_MIN = 612;
const RESULT_VIEWER_DEFAULT = 780;

interface LeadOptPropertyOption {
  value: string;
  label: string;
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function normalizeAtomIndices(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map((item) => Number(item))
        .filter((item) => Number.isFinite(item) && item >= 0)
        .map((item) => Math.floor(item))
    )
  );
}

function parseCifTokens(line: string): string[] {
  const tokens = line.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
  return tokens.map((token) => token.replace(/^['"]|['"]$/g, ''));
}

function inferLigandAnchorFromPdb(
  pdbText: string,
  preferredChain: string
): { chainId: string; residue: number } | null {
  const preferred = String(preferredChain || '').trim();
  const lines = String(pdbText || '').split(/\r?\n/);
  let fallback: { chainId: string; residue: number } | null = null;
  for (const line of lines) {
    if (!line.startsWith('HETATM')) continue;
    const residueName = line.slice(17, 20).trim().toUpperCase();
    if (!residueName || residueName === 'HOH' || residueName === 'WAT') continue;
    const chainId = line.slice(21, 22).trim();
    const residue = Number.parseInt(line.slice(22, 26).trim(), 10);
    if (!chainId || !Number.isFinite(residue) || residue <= 0) continue;
    const current = { chainId, residue };
    if (preferred && chainId === preferred) return current;
    if (!fallback) fallback = current;
  }
  return fallback;
}

function inferLigandAnchorFromCif(
  cifText: string,
  preferredChain: string
): { chainId: string; residue: number } | null {
  const preferred = String(preferredChain || '').trim();
  const lines = String(cifText || '').split(/\r?\n/);
  let fallback: { chainId: string; residue: number } | null = null;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;
    const headers: string[] = [];
    let j = i + 1;
    while (j < lines.length && lines[j].trim().startsWith('_')) {
      headers.push(lines[j].trim());
      j += 1;
    }
    if (headers.length === 0 || !headers.some((item) => item.startsWith('_atom_site.'))) continue;
    const idx = (name: string) => headers.findIndex((item) => item === name);
    const groupIdx = idx('_atom_site.group_PDB');
    const authCompIdx = idx('_atom_site.auth_comp_id');
    const labelCompIdx = idx('_atom_site.label_comp_id');
    const authAsymIdx = idx('_atom_site.auth_asym_id');
    const labelAsymIdx = idx('_atom_site.label_asym_id');
    const authSeqIdx = idx('_atom_site.auth_seq_id');
    const labelSeqIdx = idx('_atom_site.label_seq_id');

    for (; j < lines.length; j += 1) {
      const raw = lines[j].trim();
      if (!raw) continue;
      if (raw === '#' || raw === 'loop_' || raw.startsWith('data_') || raw.startsWith('_')) break;
      const tokens = parseCifTokens(raw);
      if (tokens.length < headers.length) continue;
      const group = (groupIdx >= 0 ? tokens[groupIdx] : 'HETATM').trim().toUpperCase();
      if (group && group !== 'HETATM') continue;
      const residueName = String((authCompIdx >= 0 ? tokens[authCompIdx] : tokens[labelCompIdx] || '') || '')
        .trim()
        .toUpperCase();
      if (!residueName || residueName === 'HOH' || residueName === 'WAT') continue;
      const chainId = String((authAsymIdx >= 0 ? tokens[authAsymIdx] : tokens[labelAsymIdx] || '') || '').trim();
      const residueToken = String((authSeqIdx >= 0 ? tokens[authSeqIdx] : tokens[labelSeqIdx] || '') || '').trim();
      const residue = Number.parseInt(residueToken, 10);
      if (!chainId || !Number.isFinite(residue) || residue <= 0) continue;
      const current = { chainId, residue };
      if (preferred && chainId === preferred) return current;
      if (!fallback) fallback = current;
    }
  }
  return fallback;
}

function inferLigandAnchor(
  structureText: string,
  format: 'cif' | 'pdb',
  preferredChain: string
): { chainId: string; residue: number } | null {
  if (!String(structureText || '').trim()) return null;
  if (format === 'pdb') return inferLigandAnchorFromPdb(structureText, preferredChain);
  return inferLigandAnchorFromCif(structureText, preferredChain);
}

function toPocketPayload(rows: PocketResidue[]): Array<Record<string, unknown>> {
  return rows.map((item) => ({
    chain_id: item.chain_id,
    residue_name: item.residue_name,
    residue_number: item.residue_number,
    min_distance: item.min_distance || 0,
    interaction_types: item.interaction_types || []
  }));
}

export function LeadOptimizationWorkspace({
  viewMode = 'design',
  canEdit,
  submitting,
  backend,
  onRegisterHeaderRunAction,
  proteinSequence,
  ligandSmiles,
  targetChain,
  ligandChain,
  onLigandSmilesChange,
  referenceScopeKey,
  persistedReferenceUploads,
  onReferenceUploadsChange,
  onMmpTaskQueued,
  onMmpTaskCompleted,
  onMmpTaskFailed,
  initialMmpSnapshot,
  onMmpUiStateChange,
  onPredictionQueued,
  onPredictionStateChange
}: LeadOptimizationWorkspaceProps) {
  const [error, setError] = useState<string | null>(null);
  const queryForm = useLeadOptMmpQueryForm({ onError: setError });
  const [databaseOptions, setDatabaseOptions] = useState<LeadOptMmpDatabaseItem[]>([]);
  const [selectedDatabaseId, setSelectedDatabaseId] = useState('');
  const [databaseHint, setDatabaseHint] = useState('');

  const [leftPanelWidth, setLeftPanelWidth] = useState(LEFT_PANEL_DEFAULT);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const layoutRef = useRef<HTMLDivElement | null>(null);
  const [isResizing, setIsResizing] = useState(false);
  const [resultViewerWidth, setResultViewerWidth] = useState(RESULT_VIEWER_DEFAULT);
  const resultResizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const resultLayoutRef = useRef<HTMLDivElement | null>(null);
  const [isResultResizing, setIsResultResizing] = useState(false);
  const runMmpQueryRef = useRef<(() => Promise<void>) | null>(null);
  const [activeSmiles, setActiveSmiles] = useState('');
  const [openedResultSmiles, setOpenedResultSmiles] = useState('');
  const [openedResultHighlightAtomIndices, setOpenedResultHighlightAtomIndices] = useState<number[]>([]);
  const [resultViewerOpen, setResultViewerOpen] = useState(false);
  const [showMmpRunTransition, setShowMmpRunTransition] = useState(false);
  const snapshotUiState = useMemo(() => {
    const payload = asRecord(initialMmpSnapshot || null);
    return normalizeLeadOptCandidatesUiState(payload.ui_state, backend);
  }, [backend, initialMmpSnapshot]);
  const [resultsUiState, setResultsUiState] = useState<LeadOptCandidatesUiState>(snapshotUiState);
  const [viewerPreviewRenderMode, setViewerPreviewRenderMode] = useState<CandidatePreviewRenderMode>(
    snapshotUiState.previewRenderMode
  );
  const resultsUiStateRef = useRef<LeadOptCandidatesUiState>(snapshotUiState);
  const hydratedUiStateKeyRef = useRef('');

  const computeLeftBounds = (containerWidth: number): { min: number; max: number } => {
    const minWidth = LEFT_PANEL_MIN;
    const maxWidth = containerWidth - RIGHT_PANEL_MIN - RESIZER_WIDTH - 12;
    return {
      min: minWidth,
      max: Math.max(minWidth, maxWidth)
    };
  };

  const computeResultBounds = (containerWidth: number): { min: number; max: number } => {
    const minWidth = RESULT_VIEWER_MIN;
    const maxWidth = containerWidth - RESULT_MAIN_MIN - RESIZER_WIDTH - 12;
    return {
      min: minWidth,
      max: Math.max(minWidth, maxWidth)
    };
  };

  useEffect(() => {
    resultsUiStateRef.current = resultsUiState;
  }, [resultsUiState]);

  useEffect(() => {
    setViewerPreviewRenderMode(resultsUiState.previewRenderMode);
  }, [resultsUiState.previewRenderMode]);

  useEffect(() => {
    const snapshot = asRecord(initialMmpSnapshot || null);
    const queryResult = asRecord(snapshot.query_result);
    const queryId = readText(queryResult.query_id).trim();
    const taskId = readText(queryResult.task_id || snapshot.task_id).trim();
    const key = queryId || taskId || '__initial__';
    if (hydratedUiStateKeyRef.current === key) return;
    hydratedUiStateKeyRef.current = key;
    setResultsUiState(snapshotUiState);
    setViewerPreviewRenderMode(snapshotUiState.previewRenderMode);
  }, [initialMmpSnapshot, snapshotUiState]);

  const initialFragmentSelection = useMemo(() => {
    const snapshot = (initialMmpSnapshot || null) as Record<string, unknown> | null;
    if (!snapshot) return null;
    const selection =
      snapshot.selection && typeof snapshot.selection === 'object' && !Array.isArray(snapshot.selection)
        ? (snapshot.selection as Record<string, unknown>)
        : {};
    const queryResult =
      snapshot.query_result && typeof snapshot.query_result === 'object' && !Array.isArray(snapshot.query_result)
        ? (snapshot.query_result as Record<string, unknown>)
        : {};
    const variableSpec =
      queryResult.variable_spec && typeof queryResult.variable_spec === 'object' && !Array.isArray(queryResult.variable_spec)
        ? (queryResult.variable_spec as Record<string, unknown>)
        : {};
    const variableItems = Array.isArray(selection.variable_items)
      ? selection.variable_items
      : Array.isArray(variableSpec.items)
        ? variableSpec.items
        : [];
    const fragmentIds = Array.isArray(selection.selected_fragment_ids)
      ? selection.selected_fragment_ids
      : variableItems
        .map((item) => readText((item as Record<string, unknown>).fragment_id).trim())
        .filter(Boolean);
    const atomIndices = Array.isArray(selection.selected_fragment_atom_indices)
      ? selection.selected_fragment_atom_indices
      : variableItems.flatMap((item) => {
          const atomValues = (item as Record<string, unknown>).atom_indices;
          return Array.isArray(atomValues) ? atomValues : [];
        });
    const variableQueries = Array.isArray(selection.variable_queries)
      ? selection.variable_queries
      : variableItems
        .map((item) => readText((item as Record<string, unknown>).query).trim())
        .filter(Boolean);
    return {
      fragmentIds: fragmentIds.map((value) => readText(value).trim()).filter(Boolean),
      atomIndices: atomIndices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value)),
      variableQueries: variableQueries.map((value) => readText(value).trim()).filter(Boolean)
    };
  }, [initialMmpSnapshot]);

  const reference = useLeadOptReferenceFragment({
    ligandSmiles,
    onLigandSmilesChange,
    currentVariableQuery: queryForm.variableQuery,
    onAutoVariableQuery: (value) => {
      queryForm.setVariableQuery((prev) => (prev === value ? prev : value));
    },
    onError: setError,
    scopeKey: referenceScopeKey || `${targetChain}:${ligandChain}`,
    persistedUploads: persistedReferenceUploads,
    onPersistedUploadsChange: onReferenceUploadsChange,
    initialSelection: initialFragmentSelection
  });

  const effectiveTargetChain = useMemo(
    () => readText(reference.referenceTargetChainId).trim() || readText(targetChain).trim(),
    [reference.referenceTargetChainId, targetChain]
  );
  const effectiveLigandChain = useMemo(
    () => readText(reference.referenceLigandChainId).trim() || readText(ligandChain).trim(),
    [reference.referenceLigandChainId, ligandChain]
  );

  const mmp = useLeadOptMmpQueryMachine({
    proteinSequence,
    targetChain: effectiveTargetChain,
    ligandChain: effectiveLigandChain,
    backend,
    onError: setError,
    onPredictionQueued,
    onPredictionStateChange
  });

  const hydratedSnapshotKeyRef = useRef('');
  const snapshotDatabaseIdRef = useRef('');

  useEffect(() => {
    const snapshot = (initialMmpSnapshot || null) as Record<string, unknown> | null;
    const queryResult = snapshot && typeof snapshot.query_result === 'object'
      ? (snapshot.query_result as Record<string, unknown>)
      : null;
    snapshotDatabaseIdRef.current = readText(queryResult?.mmp_database_id).trim();
    const normalizedQueryId = readText(queryResult?.query_id);
    if (!normalizedQueryId) return;
    if (hydratedSnapshotKeyRef.current === normalizedQueryId) return;
    hydratedSnapshotKeyRef.current = normalizedQueryId;
    mmp.hydrateFromSnapshot(initialMmpSnapshot || null);
  }, [initialMmpSnapshot, mmp]);

  useEffect(() => {
    if (!initialMmpSnapshot) return;
    const snapshot = initialMmpSnapshot as Record<string, unknown>;
    const queryResult = (snapshot.query_result && typeof snapshot.query_result === 'object')
      ? (snapshot.query_result as Record<string, unknown>)
      : null;
    const snapshotDbId = readText(queryResult?.mmp_database_id).trim();
    if (!snapshotDbId) return;
    if (!databaseOptions.some((item) => readText(item.id).trim() === snapshotDbId)) return;
    setSelectedDatabaseId(snapshotDbId);
  }, [databaseOptions, initialMmpSnapshot]);

  const applyDatabaseCatalog = useCallback(
    (catalog: { default_database_id?: string; databases?: LeadOptMmpDatabaseItem[] }) => {
      const visibleOnly = Array.isArray(catalog.databases)
        ? catalog.databases.filter((item) => Boolean(item.visible ?? true))
        : [];
      setDatabaseOptions(visibleOnly);
      const snapshotDatabaseId = snapshotDatabaseIdRef.current;
      const catalogDefault = readText(catalog.default_database_id).trim();
      const firstVisible = readText(visibleOnly[0]?.id).trim();
      setSelectedDatabaseId((prev) => {
        const previous = readText(prev).trim();
        if (previous && visibleOnly.some((item) => readText(item.id).trim() === previous)) {
          return previous;
        }
        const fallback = snapshotDatabaseId || catalogDefault || firstVisible;
        return fallback || '';
      });
      if (visibleOnly.length === 0) {
        setDatabaseHint('No visible MMP database. Please contact an admin to enable one.');
      } else {
        setDatabaseHint('');
      }
    },
    []
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const catalog = await fetchLeadOptimizationMmpDatabases();
        if (cancelled) return;
        applyDatabaseCatalog(catalog);
      } catch (err) {
        if (cancelled) return;
        setDatabaseHint(err instanceof Error ? err.message : 'Failed to load MMP database catalog.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [applyDatabaseCatalog]);

  const loading = reference.busy || mmp.loading;
  const canQuery = canEdit && Boolean(reference.effectiveLigandSmiles);
  const fragmentSketchSmiles = reference.fragmentSourceSmiles.trim() || reference.effectiveLigandSmiles.trim();

  const fragmentById = useMemo(() => {
    const map = new Map<string, LigandFragmentItem>();
    reference.fragments.forEach((item) => map.set(item.fragment_id, item));
    return map;
  }, [reference.fragments]);

  const selectedFragmentItems = useMemo(
    () =>
      reference.selectedFragmentIds
        .map((fragmentId) => fragmentById.get(fragmentId))
        .filter((item): item is LigandFragmentItem => Boolean(item)),
    [fragmentById, reference.selectedFragmentIds]
  );

  const inferredQueryMode = useMemo(
    () => inferQueryModeFromSelection(selectedFragmentItems, reference.fragments),
    [reference.fragments, selectedFragmentItems]
  );

  useEffect(() => {
    if (selectedFragmentItems.length === 0) return;
    if (queryForm.queryMode !== inferredQueryMode) {
      queryForm.setQueryMode(inferredQueryMode);
    }
  }, [inferredQueryMode, queryForm.queryMode, queryForm.setQueryMode, selectedFragmentItems.length]);

  const selectedDatabase = useMemo(
    () => databaseOptions.find((item) => readText(item.id).trim() === selectedDatabaseId) || null,
    [databaseOptions, selectedDatabaseId]
  );

  const dbSelectOptions = useMemo(
    () =>
      databaseOptions.map((item) => ({
        id: readText(item.id).trim(),
        label: readText(item.label).trim() || readText(item.id).trim()
      })),
    [databaseOptions]
  );

  const propertyOptions = useMemo<LeadOptPropertyOption[]>(() => {
    const base: LeadOptPropertyOption[] = [];
    const seen = new Set<string>();
    const dbProperties = Array.isArray(selectedDatabase?.properties) ? selectedDatabase?.properties : [];
    for (const item of dbProperties) {
      const value = readText(item.name).trim();
      if (!value || seen.has(value)) continue;
      seen.add(value);
      const label = readText(item.display_name).trim() || readText(item.label).trim() || value;
      base.push({ value, label });
    }
    const descriptorDefaults: LeadOptPropertyOption[] = [
      { value: 'mw', label: 'MW' },
      { value: 'logp', label: 'LogP' },
      { value: 'tpsa', label: 'TPSA' }
    ];
    for (const item of descriptorDefaults) {
      if (seen.has(item.value)) continue;
      seen.add(item.value);
      base.push(item);
    }
    if (base.length === 0) {
      base.push({ value: 'mw', label: 'MW' });
    }
    return base;
  }, [selectedDatabase?.properties]);

  useEffect(() => {
    const active = readText(queryForm.queryProperty).trim();
    if (!active) return;
    if (propertyOptions.some((item) => item.value === active)) return;
    queryForm.setQueryProperty('');
    queryForm.setDirection('');
  }, [propertyOptions, queryForm.queryProperty, queryForm.setDirection, queryForm.setQueryProperty]);

  useEffect(() => {
    if (readText(queryForm.queryProperty).trim()) return;
    if (!readText(queryForm.direction).trim()) return;
    queryForm.setDirection('');
  }, [queryForm.direction, queryForm.queryProperty, queryForm.setDirection]);

  const effectiveViewMode = viewMode;

  const pocketPayload = useMemo(() => toPocketPayload(reference.pocketResidues), [reference.pocketResidues]);
  const candidateSmilesList = useMemo(
    () =>
      mmp.enumeratedCandidates
        .map((row) => readText(row.smiles).trim())
        .filter((value): value is string => Boolean(value)),
    [mmp.enumeratedCandidates]
  );
  const openedPredictionRecord = openedResultSmiles ? mmp.predictionBySmiles[openedResultSmiles] : null;
  const openedStructureText = readText(openedPredictionRecord?.structureText).trim();
  const openedStructureFormat: 'cif' | 'pdb' =
    readText(openedPredictionRecord?.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif';
  const openedStructureTaskId = readText(openedPredictionRecord?.taskId).trim();
  const openedResultViewerKey = `${openedResultSmiles}:${openedStructureTaskId}:${openedStructureFormat}:${viewerPreviewRenderMode}`;
  const openedResultLigandAnchor = useMemo(
    () => inferLigandAnchor(openedStructureText, openedStructureFormat, effectiveLigandChain),
    [effectiveLigandChain, openedStructureFormat, openedStructureText]
  );
  const openedResultHighlightAtoms = useMemo<MolstarAtomHighlight[]>(() => {
    if (!resultViewerOpen) return [];
    if (!openedResultLigandAnchor || openedResultHighlightAtomIndices.length === 0) return [];
    return openedResultHighlightAtomIndices.map((atomIndex) => ({
      chainId: openedResultLigandAnchor.chainId,
      residue: openedResultLigandAnchor.residue,
      atomName: '',
      atomIndex,
      emphasis: 'default'
    }));
  }, [openedResultHighlightAtomIndices, openedResultLigandAnchor, resultViewerOpen]);
  const openedResultActiveAtom: MolstarAtomHighlight | null = null;
  const openedResultActiveResidue = useMemo<MolstarResidueHighlight | null>(() => {
    if (!resultViewerOpen || !openedResultLigandAnchor) return null;
    return {
      chainId: openedResultLigandAnchor.chainId,
      residue: openedResultLigandAnchor.residue,
      emphasis: 'active'
    };
  }, [openedResultLigandAnchor, resultViewerOpen]);
  const referenceProteinSequence = useMemo(() => {
    const map = reference.targetChainSequences || {};
    const direct = readText((map as Record<string, unknown>)[effectiveTargetChain]).replace(/\s+/g, '').trim();
    if (direct) return direct;
    for (const value of Object.values(map as Record<string, unknown>)) {
      const sequence = readText(value).replace(/\s+/g, '').trim();
      if (sequence) return sequence;
    }
    return '';
  }, [effectiveTargetChain, reference.targetChainSequences]);

  useEffect(() => {
    if (candidateSmilesList.length === 0) {
      setActiveSmiles('');
      setOpenedResultSmiles('');
      setOpenedResultHighlightAtomIndices([]);
      setResultViewerOpen(false);
      return;
    }
    if (!activeSmiles || !candidateSmilesList.includes(activeSmiles)) {
      setActiveSmiles(candidateSmilesList[0]);
    }
    if (openedResultSmiles && !candidateSmilesList.includes(openedResultSmiles)) {
      setOpenedResultSmiles('');
      setOpenedResultHighlightAtomIndices([]);
      setResultViewerOpen(false);
    }
  }, [activeSmiles, candidateSmilesList, openedResultSmiles]);

  useEffect(() => {
    const notice = readText(mmp.queryNotice).trim();
    const isMmpRunning = mmp.loading && notice.toLowerCase().includes('running mmp query');
    if (isMmpRunning) {
      setShowMmpRunTransition(true);
      return;
    }
    if (!showMmpRunTransition) return;
    const timer = window.setTimeout(() => setShowMmpRunTransition(false), 360);
    return () => window.clearTimeout(timer);
  }, [mmp.loading, mmp.queryNotice, showMmpRunTransition]);

  const handleOpenPredictionResult = useCallback(
    async (candidateSmiles: string, highlightAtomIndices?: number[]) => {
      const normalizedSmiles = readText(candidateSmiles).trim();
      if (!normalizedSmiles) return;
      setActiveSmiles(normalizedSmiles);
      setOpenedResultSmiles(normalizedSmiles);
      setResultViewerOpen(true);
      setError(null);
      const fallbackHighlight = normalizeAtomIndices(
        mmp.enumeratedCandidates.find((row) => readText(row.smiles).trim() === normalizedSmiles)?.final_highlight_atom_indices
      );
      setOpenedResultHighlightAtomIndices(
        normalizeAtomIndices(highlightAtomIndices).length > 0
          ? normalizeAtomIndices(highlightAtomIndices)
          : fallbackHighlight
      );
      const record = await mmp.ensurePredictionResult(normalizedSmiles);
      if (!record || String(record.state || '').toUpperCase() !== 'SUCCESS') return;
      if (!readText(record.structureText).trim()) {
        setError('Prediction completed, but result bundle has no readable structure.');
        return;
      }
      setError(null);
    },
    [mmp.ensurePredictionResult, mmp.enumeratedCandidates]
  );

  const handleCandidatesUiStateChange = useCallback(
    (nextUiState: LeadOptCandidatesUiState) => {
      setResultsUiState(nextUiState);
      if (typeof onMmpUiStateChange === 'function') {
        void onMmpUiStateChange({ uiState: nextUiState });
      }
    },
    [onMmpUiStateChange]
  );

  const handlePreviewRenderModeChange = useCallback((mode: CandidatePreviewRenderMode) => {
    setViewerPreviewRenderMode(mode);
  }, []);

  useEffect(() => {
    if (effectiveViewMode !== 'design') return;
    const backendKey = readText(resultsUiState.selectedBackend).trim().toLowerCase();
    if (!backendKey) return;
    const record = mmp.referencePredictionByBackend[backendKey];
    if (!record) return;
    if (String(record.state || '').toUpperCase() !== 'SUCCESS') return;
    if (readText(record.structureText).trim() && record.pairIptmResolved === true) return;

    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (cancelled) return;
      void mmp.ensureReferencePredictionResult(backendKey);
    }, 220);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    effectiveViewMode,
    mmp.ensureReferencePredictionResult,
    mmp.referencePredictionByBackend,
    resultsUiState.selectedBackend
  ]);

  const runMmpQuery = useCallback(async () => {
    const variableItems = queryForm.buildVariableItems(selectedFragmentItems, reference.fragments);
    const querySmiles = fragmentSketchSmiles;
    await mmp.runMmpQuery({
      canQuery,
      effectiveLigandSmiles: querySmiles,
      variableItems,
      constantQuery: queryForm.constantQuery,
      direction: queryForm.direction,
      queryProperty: queryForm.queryProperty,
      mmpDatabaseId: selectedDatabaseId,
      queryMode: queryForm.queryMode,
      minPairs: queryForm.minPairs,
      envRadius: queryForm.envRadius,
      onTaskQueued:
        typeof onMmpTaskQueued === 'function'
          ? async ({ taskId, requestPayload }) => {
              await onMmpTaskQueued({
                taskId,
                requestPayload,
                querySmiles,
                referenceUploads: reference.persistedUploads
              });
            }
          : undefined,
      onTaskCompleted:
        typeof onMmpTaskCompleted === 'function'
          ? async (payload) => {
              const baseSnapshot = asRecord(payload.resultSnapshot);
              await onMmpTaskCompleted({
                ...payload,
                resultSnapshot: {
                  ...baseSnapshot,
                  ui_state: { ...resultsUiStateRef.current }
                }
              });
            }
          : undefined,
      onTaskFailed: onMmpTaskFailed
    });
  }, [
    mmp.runMmpQuery,
    queryForm.buildVariableItems,
    queryForm.constantQuery,
    queryForm.direction,
    queryForm.envRadius,
    queryForm.minPairs,
    queryForm.queryMode,
    queryForm.queryProperty,
    selectedDatabaseId,
    fragmentSketchSmiles,
    reference.fragments,
    reference.persistedUploads,
    selectedFragmentItems,
    canQuery,
    onMmpTaskQueued,
    onMmpTaskCompleted,
    onMmpTaskFailed,
    pocketPayload
  ]);
  runMmpQueryRef.current = runMmpQuery;

  const triggerHeaderRun = useCallback(() => {
    const action = runMmpQueryRef.current;
    if (!action) return;
    void action();
  }, []);

  const handleResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if (typeof window !== 'undefined' && window.matchMedia('(max-width: 1100px)').matches) return;
    if (!layoutRef.current) return;
    resizeStateRef.current = { startX: event.clientX, startWidth: leftPanelWidth };
    setIsResizing(true);
    event.preventDefault();
  };

  const handleResizerKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const container = layoutRef.current;
    if (!container) return;
    const bounds = computeLeftBounds(container.clientWidth);
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setLeftPanelWidth((current) => Math.max(bounds.min, current - LEFT_PANEL_KEY_STEP));
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setLeftPanelWidth((current) => Math.min(bounds.max, current + LEFT_PANEL_KEY_STEP));
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setLeftPanelWidth(LEFT_PANEL_DEFAULT);
    }
  };

  const handleResultResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if (typeof window !== 'undefined' && window.matchMedia('(max-width: 1100px)').matches) return;
    if (!resultLayoutRef.current) return;
    resultResizeStateRef.current = { startX: event.clientX, startWidth: resultViewerWidth };
    setIsResultResizing(true);
    event.preventDefault();
  };

  const handleResultResizerKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const container = resultLayoutRef.current;
    if (!container) return;
    const bounds = computeResultBounds(container.clientWidth);
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setResultViewerWidth((current) => Math.max(bounds.min, current - LEFT_PANEL_KEY_STEP));
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setResultViewerWidth((current) => Math.min(bounds.max, current + LEFT_PANEL_KEY_STEP));
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setResultViewerWidth(RESULT_VIEWER_DEFAULT);
    }
  };

  useEffect(() => {
    if (!isResizing) return;
    const handleMove = (event: PointerEvent) => {
      const state = resizeStateRef.current;
      const container = layoutRef.current;
      if (!state || !container) return;
      const delta = event.clientX - state.startX;
      const total = container.clientWidth;
      const bounds = computeLeftBounds(total);
      const next = Math.max(bounds.min, Math.min(bounds.max, state.startWidth + delta));
      setLeftPanelWidth(next);
    };
    const handleUp = () => {
      setIsResizing(false);
      resizeStateRef.current = null;
    };
    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    window.addEventListener('pointercancel', handleUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResizing]);

  useEffect(() => {
    if (!isResultResizing) return;
    const handleMove = (event: PointerEvent) => {
      const state = resultResizeStateRef.current;
      const container = resultLayoutRef.current;
      if (!state || !container) return;
      const delta = event.clientX - state.startX;
      const total = container.clientWidth;
      const bounds = computeResultBounds(total);
      const next = Math.max(bounds.min, Math.min(bounds.max, state.startWidth + delta));
      setResultViewerWidth(next);
    };
    const handleUp = () => {
      setIsResultResizing(false);
      resultResizeStateRef.current = null;
    };
    window.addEventListener('pointermove', handleMove);
    window.addEventListener('pointerup', handleUp);
    window.addEventListener('pointercancel', handleUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    return () => {
      window.removeEventListener('pointermove', handleMove);
      window.removeEventListener('pointerup', handleUp);
      window.removeEventListener('pointercancel', handleUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResultResizing]);

  useLayoutEffect(() => {
    if (typeof onRegisterHeaderRunAction !== 'function') return;
    if (effectiveViewMode !== 'reference') {
      onRegisterHeaderRunAction(null);
      return;
    }
    onRegisterHeaderRunAction(triggerHeaderRun);
    return () => onRegisterHeaderRunAction(null);
  }, [effectiveViewMode, onRegisterHeaderRunAction, triggerHeaderRun]);

  const layoutStyle = useMemo(
    () =>
      ({
        '--lead-opt-left-width': `${leftPanelWidth}px`,
        '--lead-opt-result-left-width': `${resultViewerWidth}px`,
        '--lead-opt-result-main-min-width': `${RESULT_MAIN_MIN}px`
      }) as CSSProperties,
    [leftPanelWidth, resultViewerWidth]
  );

  return (
    <div className="lead-opt-workspace">
      {effectiveViewMode === 'reference' ? (
        <div ref={layoutRef} className="lead-opt-layout lead-opt-layout--resizable" style={layoutStyle}>
          <LeadOptReferencePanel
            sectionId="leadopt-reference"
            canEdit={canEdit}
            loading={loading}
            submitting={submitting}
            referenceReady={reference.referenceReady}
            previewStructureText={reference.previewStructureText}
            previewStructureFormat={reference.previewStructureFormat}
            previewOverlayStructureText={reference.previewOverlayStructureText}
            previewOverlayStructureFormat={reference.previewOverlayStructureFormat}
            ligandChain={effectiveLigandChain}
            highlightedLigandAtoms={reference.highlightedLigandAtoms}
            highlightedPocketResidues={reference.highlightedPocketResidues}
            activeMolstarAtom={reference.activeMolstarAtom}
            onResiduePick={reference.handleMolstarResiduePick}
            onTargetFileChange={reference.handleTargetFileChange}
            onLigandFileChange={reference.handleLigandFileChange}
          />

          <div
            className={`panel-resizer lead-opt-layout-resizer ${isResizing ? 'dragging' : ''}`}
            onPointerDown={handleResizeStart}
            onKeyDown={handleResizerKeyDown}
            role="separator"
            aria-label="Resize 3D and fragments panels"
            aria-orientation="vertical"
            tabIndex={0}
          />

          <LeadOptFragmentPanel
            sectionId="leadopt-fragment"
            effectiveLigandSmiles={fragmentSketchSmiles}
            fragments={reference.fragments}
            selectedFragmentIds={reference.selectedFragmentIds}
            activeFragmentId={reference.activeFragmentId}
            onAtomClick={reference.handleFragmentAtomClick}
            onToggleFragmentSelection={reference.toggleFragmentSelection}
            onClearFragmentSelection={reference.clearFragmentSelection}
            direction={queryForm.direction}
            queryProperty={queryForm.queryProperty}
            selectedDatabaseId={selectedDatabaseId}
            databaseOptions={dbSelectOptions}
            propertyOptions={propertyOptions}
            envRadius={queryForm.envRadius}
            minPairs={queryForm.minPairs}
            onDirectionChange={queryForm.setDirection}
            onQueryPropertyChange={queryForm.setQueryProperty}
            onDatabaseIdChange={setSelectedDatabaseId}
            onEnvRadiusChange={queryForm.setEnvRadius}
            onMinPairsChange={queryForm.setMinPairs}
          />
        </div>
      ) : (
        <div
          ref={resultLayoutRef}
          className={`lead-opt-mmp-layout${
            resultViewerOpen ? ' lead-opt-mmp-layout--viewer-open lead-opt-mmp-layout--resizable' : ''
          }`}
          style={layoutStyle}
        >
          {resultViewerOpen ? (
            <section className="lead-opt-mmp-viewer">
              {openedStructureText ? (
                <LeadOptMolstarViewer
                  key={openedResultViewerKey}
                  structureText={openedStructureText}
                  format={openedStructureFormat}
                  colorMode={viewerPreviewRenderMode === 'confidence' ? 'alphafold' : 'default'}
                  confidenceBackend={resultsUiState.selectedBackend}
                  styleVariant="results"
                  ligandFocusChainId={effectiveLigandChain}
                  highlightAtoms={openedResultHighlightAtoms}
                  activeResidue={openedResultActiveResidue}
                  activeAtom={openedResultActiveAtom}
                  showSequence={false}
                  suppressResidueSelection
                  interactionGranularity="element"
                  suppressAutoFocus={false}
                />
              ) : (
                <div className="ligand-preview-empty">Result not ready yet.</div>
              )}
            </section>
          ) : null}
          {resultViewerOpen ? (
            <div
              className={`panel-resizer lead-opt-layout-resizer ${isResultResizing ? 'dragging' : ''}`}
              onPointerDown={handleResultResizeStart}
              onKeyDown={handleResultResizerKeyDown}
              role="separator"
              aria-label="Resize 3D and candidate panels"
              aria-orientation="vertical"
              tabIndex={0}
            />
          ) : null}
          <div className={`lead-opt-mmp-main${resultViewerOpen ? ' lead-opt-mmp-main--viewer-open' : ''}`}>
            <LeadOptCandidatesPanel
              sectionId="leadopt-candidates"
              cardMode={resultViewerOpen}
              enumeratedCandidates={mmp.enumeratedCandidates}
              loading={loading}
              referenceReady={reference.referenceReady}
              referenceSmiles={reference.effectiveLigandSmiles}
              predictionBySmiles={mmp.predictionBySmiles}
              referencePredictionByBackend={mmp.referencePredictionByBackend}
              defaultPredictionBackend={backend}
              initialUiState={resultsUiState}
              activeSmiles={activeSmiles}
              onActiveSmilesChange={setActiveSmiles}
              onRunPredictCandidate={(candidateSmiles, predictionBackend) => {
                const referenceCandidateSmiles = readText(reference.effectiveLigandSmiles).trim();
                if (referenceCandidateSmiles) {
                  void mmp.runPredictReferenceForBackend({
                    candidateSmiles: referenceCandidateSmiles,
                    referenceReady: reference.referenceReady,
                    referenceProteinSequence,
                    referenceTemplateStructureText: reference.previewStructureText,
                    referenceTemplateFormat: reference.previewStructureFormat,
                    backend: predictionBackend,
                    pocketResidues: pocketPayload
                  });
                }
                void mmp.runPredictCandidate({
                  candidateSmiles,
                  referenceReady: reference.referenceReady,
                  referenceProteinSequence,
                  referenceTemplateStructureText: reference.previewStructureText,
                  referenceTemplateFormat: reference.previewStructureFormat,
                  backend: predictionBackend,
                  pocketResidues: pocketPayload
                });
              }}
              onOpenPredictionResult={(candidateSmiles, highlightAtomIndices) => {
                void handleOpenPredictionResult(candidateSmiles, highlightAtomIndices);
              }}
              onUiStateChange={handleCandidatesUiStateChange}
              onPreviewRenderModeChange={handlePreviewRenderModeChange}
              onExitCardMode={() => setResultViewerOpen(false)}
            />
          </div>
        </div>
      )}

      {showMmpRunTransition ? (
        <div className="run-submit-transition" role="status" aria-live="polite" aria-label="MMP query running">
          <div className="run-submit-transition-card">
            <span className="run-submit-transition-icon" aria-hidden="true">
              <LoaderCircle size={14} className="spin" />
            </span>
            <span className="run-submit-transition-title">Running MMP query...</span>
          </div>
        </div>
      ) : null}

      {databaseHint ? <p className="lead-opt-error">{readText(databaseHint)}</p> : null}
      {error ? <p className="lead-opt-error">{readText(error)}</p> : null}
    </div>
  );
}
