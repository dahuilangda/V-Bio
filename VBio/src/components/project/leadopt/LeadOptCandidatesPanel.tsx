import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Clock3,
  Loader2,
  Play,
  SlidersHorizontal,
  X
} from 'lucide-react';
import { MemoLigand2DPreview } from '../Ligand2DPreview';
import { JSMEEditor } from '../JSMEEditor';
import { loadRDKitModule } from '../../../utils/rdkit';
import type { LeadOptPredictionRecord } from './hooks/useLeadOptMmpQueryMachine';

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function readNumberOrNull(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function formatMetric(value: unknown, digits = 2): string {
  const numeric = readNumberOrNull(value);
  if (numeric === null) return '-';
  return numeric.toFixed(digits);
}

function formatDelta(value: unknown, digits = 2): string {
  const numeric = readNumberOrNull(value);
  if (numeric === null) return '-';
  const sign = numeric > 0 ? '+' : '';
  return `${sign}${numeric.toFixed(digits)}`;
}

function parseOptionalNumber(value: string): number | null {
  const token = String(value || '').trim();
  if (!token) return null;
  const numeric = Number(token);
  return Number.isFinite(numeric) ? numeric : null;
}

function normalizeSmilesForSearch(value: string): string {
  return value.trim().replace(/\s+/g, '');
}

function hasSubstructureMatchPayload(value: unknown): boolean {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed || trimmed === '[]' || trimmed === '{}' || trimmed === 'null') return false;
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (Array.isArray(parsed)) return parsed.length > 0;
      if (parsed && typeof parsed === 'object') return Object.keys(parsed as Record<string, unknown>).length > 0;
      return Boolean(parsed);
    } catch {
      return trimmed !== '-1';
    }
  }
  if (Array.isArray(value)) return value.length > 0;
  if (value && typeof value === 'object') return Object.keys(value as Record<string, unknown>).length > 0;
  return Boolean(value);
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

function deriveReferenceProperty(row: Record<string, unknown>, key: 'mw' | 'logp' | 'tpsa'): number | null {
  const props = (row.properties as Record<string, unknown>) || {};
  const deltas = (row.property_deltas as Record<string, unknown>) || {};
  const propKey = key === 'mw' ? 'molecular_weight' : key;
  const current = readNumberOrNull(props[propKey]);
  const delta = readNumberOrNull(deltas[key]);
  if (current === null || delta === null) return null;
  return current - delta;
}

type ConfidenceTone = 'vhigh' | 'high' | 'low' | 'vlow' | 'na';
type PhyschemTone = 'vhigh' | 'high' | 'low' | 'vlow' | 'na';

function resolveConfidenceTone(score: number | null): ConfidenceTone {
  if (score === null || !Number.isFinite(score)) return 'na';
  if (score >= 90) return 'vhigh';
  if (score >= 70) return 'high';
  if (score >= 50) return 'low';
  return 'vlow';
}

function resolveDeltaTone(delta: number | null): 'up' | 'down' | 'flat' | 'na' {
  if (delta === null || !Number.isFinite(delta)) return 'na';
  if (delta > 0.000001) return 'up';
  if (delta < -0.000001) return 'down';
  return 'flat';
}

function resolvePaeTone(value: number | null): ConfidenceTone {
  if (value === null || !Number.isFinite(value)) return 'na';
  if (value <= 5) return 'vhigh';
  if (value <= 10) return 'high';
  if (value <= 20) return 'low';
  return 'vlow';
}

function resolvePhyschemTone(key: 'mw' | 'logp' | 'tpsa', value: number | null): PhyschemTone {
  if (value === null || !Number.isFinite(value)) return 'na';
  if (key === 'mw') {
    if (value >= 250 && value <= 500) return 'vhigh';
    if (value >= 180 && value <= 550) return 'high';
    if (value >= 120 && value <= 650) return 'low';
    return 'vlow';
  }
  if (key === 'logp') {
    if (value >= 1 && value <= 3) return 'vhigh';
    if (value >= 0 && value <= 5) return 'high';
    if (value >= -0.5 && value <= 6) return 'low';
    return 'vlow';
  }
  if (value >= 40 && value <= 120) return 'vhigh';
  if (value >= 20 && value <= 140) return 'high';
  if (value >= 10 && value <= 170) return 'low';
  return 'vlow';
}

function DeltaSpark({ tone }: { tone: 'up' | 'down' | 'flat' | 'na' }) {
  const linePoints =
    tone === 'up' ? '1,10 9,7 17,5 27,2' : tone === 'down' ? '1,2 9,5 17,7 27,10' : '1,6 9,6 17,6 27,6';
  const dot = tone === 'up' ? { cx: 27, cy: 2 } : tone === 'down' ? { cx: 27, cy: 10 } : { cx: 27, cy: 6 };
  return (
    <svg className={`lead-opt-delta-spark ${tone}`} viewBox="0 0 28 12" aria-hidden="true">
      <polyline points={linePoints} />
      <circle cx={dot.cx} cy={dot.cy} r="1.4" />
    </svg>
  );
}

function normalizeState(state: unknown): 'SUCCESS' | 'RUNNING' | 'FAILURE' | 'QUEUED' | 'UNSCORED' {
  const token = String(state || '').toUpperCase();
  if (token === 'SUCCESS' || token === 'RUNNING' || token === 'FAILURE' || token === 'QUEUED') return token;
  return 'UNSCORED';
}

function stateLabel(state: 'SUCCESS' | 'RUNNING' | 'FAILURE' | 'QUEUED' | 'UNSCORED'): string {
  if (state === 'SUCCESS') return 'Scored';
  if (state === 'RUNNING') return 'Running';
  if (state === 'FAILURE') return 'Failed';
  if (state === 'QUEUED') return 'Queued';
  return 'Pending';
}

function stateClass(state: 'SUCCESS' | 'RUNNING' | 'FAILURE' | 'QUEUED' | 'UNSCORED'): string {
  if (state === 'SUCCESS') return 'tone-success';
  if (state === 'RUNNING') return 'tone-running';
  if (state === 'FAILURE') return 'tone-failure';
  if (state === 'QUEUED') return 'tone-queued';
  return 'tone-unscored';
}

const BACKEND_OPTIONS: Array<{ key: string; label: string }> = [
  { key: 'boltz', label: 'Boltz2' },
  { key: 'protenix', label: 'Protenix' },
  { key: 'alphafold3', label: 'AF3' }
];

function normalizeBackend(value: string): string {
  const token = String(value || '').trim().toLowerCase();
  return BACKEND_OPTIONS.some((item) => item.key === token) ? token : 'boltz';
}

type CandidateStateFilter = 'all' | 'pending' | 'queued' | 'running' | 'success' | 'failure';
type CandidateStructureSearchMode = 'exact' | 'substructure';
export type CandidatePreviewRenderMode = 'confidence' | 'fragment';

export interface LeadOptCandidatesUiState {
  selectedBackend: string;
  stateFilter: CandidateStateFilter;
  showAdvanced: boolean;
  mwMin: string;
  mwMax: string;
  logpMin: string;
  logpMax: string;
  tpsaMin: string;
  tpsaMax: string;
  structureSearchMode: CandidateStructureSearchMode;
  structureSearchQuery: string;
  previewRenderMode: CandidatePreviewRenderMode;
}

function normalizeStateFilter(value: unknown): CandidateStateFilter {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'pending' || token === 'queued' || token === 'running' || token === 'success' || token === 'failure') {
    return token;
  }
  return 'all';
}

function normalizeStructureSearchMode(value: unknown): CandidateStructureSearchMode {
  return String(value || '').trim().toLowerCase() === 'substructure' ? 'substructure' : 'exact';
}

function normalizePreviewRenderMode(value: unknown): CandidatePreviewRenderMode {
  return String(value || '').trim().toLowerCase() === 'fragment' ? 'fragment' : 'confidence';
}

function normalizeBoolean(value: unknown, defaultValue = false): boolean {
  if (typeof value === 'boolean') return value;
  const token = String(value || '').trim().toLowerCase();
  if (token === 'true' || token === '1' || token === 'yes') return true;
  if (token === 'false' || token === '0' || token === 'no') return false;
  return defaultValue;
}

export function normalizeLeadOptCandidatesUiState(
  value: unknown,
  defaultPredictionBackend: string
): LeadOptCandidatesUiState {
  const payload =
    value && typeof value === 'object' && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : {};
  return {
    selectedBackend: normalizeBackend(readText(payload.selectedBackend ?? payload.selected_backend) || defaultPredictionBackend),
    stateFilter: normalizeStateFilter(payload.stateFilter ?? payload.state_filter),
    showAdvanced: normalizeBoolean(payload.showAdvanced ?? payload.show_advanced, false),
    mwMin: readText(payload.mwMin ?? payload.mw_min).trim(),
    mwMax: readText(payload.mwMax ?? payload.mw_max).trim(),
    logpMin: readText(payload.logpMin ?? payload.logp_min).trim(),
    logpMax: readText(payload.logpMax ?? payload.logp_max).trim(),
    tpsaMin: readText(payload.tpsaMin ?? payload.tpsa_min).trim(),
    tpsaMax: readText(payload.tpsaMax ?? payload.tpsa_max).trim(),
    structureSearchMode: normalizeStructureSearchMode(payload.structureSearchMode ?? payload.structure_search_mode),
    structureSearchQuery: readText(payload.structureSearchQuery ?? payload.structure_search_query).trim(),
    previewRenderMode: normalizePreviewRenderMode(payload.previewRenderMode ?? payload.preview_render_mode)
  };
}

export function buildLeadOptCandidatesUiStateSignature(value: LeadOptCandidatesUiState): string {
  return [
    normalizeBackend(value.selectedBackend),
    normalizeStateFilter(value.stateFilter),
    value.showAdvanced ? '1' : '0',
    readText(value.mwMin).trim(),
    readText(value.mwMax).trim(),
    readText(value.logpMin).trim(),
    readText(value.logpMax).trim(),
    readText(value.tpsaMin).trim(),
    readText(value.tpsaMax).trim(),
    normalizeStructureSearchMode(value.structureSearchMode),
    readText(value.structureSearchQuery).trim(),
    normalizePreviewRenderMode(value.previewRenderMode)
  ].join('|');
}

const TABLE_CANDIDATE_2D_WIDTH = 288;
const CARD_CANDIDATE_2D_WIDTH = 190;
const CARD_PREVIEW_2D_HEIGHT = 166;
const PREVIEW_2D_HEIGHT = 138;
const CONFIDENCE_HYDRATION_MAX_PER_TICK = 4;
const CONFIDENCE_HYDRATION_COOLDOWN_MS = 1800;

interface LeadOptCandidatesPanelProps {
  sectionId?: string;
  cardMode?: boolean;
  enumeratedCandidates: Array<Record<string, unknown>>;
  loading: boolean;
  referenceReady: boolean;
  referenceSmiles: string;
  predictionBySmiles: Record<string, LeadOptPredictionRecord>;
  referencePredictionByBackend?: Record<string, LeadOptPredictionRecord>;
  defaultPredictionBackend: string;
  initialUiState?: LeadOptCandidatesUiState | null;
  activeSmiles: string;
  onActiveSmilesChange: (smiles: string) => void;
  onRunPredictCandidate: (candidateSmiles: string, backend: string) => void;
  onOpenPredictionResult: (candidateSmiles: string, highlightAtomIndices?: number[]) => void;
  onEnsurePredictionResult?: (candidateSmiles: string) => Promise<LeadOptPredictionRecord | null>;
  onUiStateChange?: (state: LeadOptCandidatesUiState) => void;
  onPreviewRenderModeChange?: (mode: CandidatePreviewRenderMode) => void;
  onExitCardMode?: () => void;
}

export function LeadOptCandidatesPanel({
  sectionId,
  cardMode = false,
  enumeratedCandidates,
  loading,
  referenceReady,
  referenceSmiles,
  predictionBySmiles,
  referencePredictionByBackend = {},
  defaultPredictionBackend,
  initialUiState,
  activeSmiles,
  onActiveSmilesChange,
  onRunPredictCandidate,
  onOpenPredictionResult,
  onEnsurePredictionResult,
  onUiStateChange,
  onPreviewRenderModeChange,
  onExitCardMode
}: LeadOptCandidatesPanelProps) {
  const PAGE_SIZE = 12;
  const CARD_BATCH_SIZE = 24;
  const normalizedInitialUiState = useMemo(
    () => normalizeLeadOptCandidatesUiState(initialUiState, defaultPredictionBackend),
    [defaultPredictionBackend, initialUiState]
  );
  const [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState('1');
  const [visibleCardCount, setVisibleCardCount] = useState(CARD_BATCH_SIZE);
  const [selectedBackend, setSelectedBackend] = useState<string>(normalizedInitialUiState.selectedBackend);
  const [stateFilter, setStateFilter] = useState<CandidateStateFilter>(normalizedInitialUiState.stateFilter);
  const [showAdvanced, setShowAdvanced] = useState(normalizedInitialUiState.showAdvanced);
  const [mwMin, setMwMin] = useState(normalizedInitialUiState.mwMin);
  const [mwMax, setMwMax] = useState(normalizedInitialUiState.mwMax);
  const [logpMin, setLogpMin] = useState(normalizedInitialUiState.logpMin);
  const [logpMax, setLogpMax] = useState(normalizedInitialUiState.logpMax);
  const [tpsaMin, setTpsaMin] = useState(normalizedInitialUiState.tpsaMin);
  const [tpsaMax, setTpsaMax] = useState(normalizedInitialUiState.tpsaMax);
  const [structureSearchMode, setStructureSearchMode] = useState<CandidateStructureSearchMode>(
    normalizedInitialUiState.structureSearchMode
  );
  const [structureSearchQuery, setStructureSearchQuery] = useState(normalizedInitialUiState.structureSearchQuery);
  const [previewRenderMode, setPreviewRenderMode] = useState<CandidatePreviewRenderMode>(
    normalizedInitialUiState.previewRenderMode
  );
  const [debouncedStructureSearchQuery, setDebouncedStructureSearchQuery] = useState('');
  const [structureSearchMatches, setStructureSearchMatches] = useState<Record<string, boolean>>({});
  const [structureSearchLoading, setStructureSearchLoading] = useState(false);
  const [structureSearchError, setStructureSearchError] = useState<string | null>(null);
  const cardLoadMoreRef = useRef<HTMLDivElement | null>(null);
  const confidenceHydrationInFlightRef = useRef<Record<string, boolean>>({});
  const confidenceHydrationAttemptAtRef = useRef<Record<string, number>>({});
  const uiStateHydrationSignatureRef = useRef('');
  const uiStateEmitSignatureRef = useRef('');
  const selectedBackendKey = normalizeBackend(selectedBackend);

  const buildUiState = (
    overrides?: Partial<LeadOptCandidatesUiState>
  ): LeadOptCandidatesUiState => ({
    selectedBackend: normalizeBackend(overrides?.selectedBackend ?? selectedBackend),
    stateFilter: normalizeStateFilter(overrides?.stateFilter ?? stateFilter),
    showAdvanced: overrides?.showAdvanced ?? showAdvanced,
    mwMin: readText(overrides?.mwMin ?? mwMin).trim(),
    mwMax: readText(overrides?.mwMax ?? mwMax).trim(),
    logpMin: readText(overrides?.logpMin ?? logpMin).trim(),
    logpMax: readText(overrides?.logpMax ?? logpMax).trim(),
    tpsaMin: readText(overrides?.tpsaMin ?? tpsaMin).trim(),
    tpsaMax: readText(overrides?.tpsaMax ?? tpsaMax).trim(),
    structureSearchMode: normalizeStructureSearchMode(overrides?.structureSearchMode ?? structureSearchMode),
    structureSearchQuery: readText(overrides?.structureSearchQuery ?? structureSearchQuery).trim(),
    previewRenderMode: normalizePreviewRenderMode(overrides?.previewRenderMode ?? previewRenderMode)
  });

  const emitUiStateNow = (overrides?: Partial<LeadOptCandidatesUiState>) => {
    if (typeof onUiStateChange !== 'function') return;
    const nextState = buildUiState(overrides);
    const signature = buildLeadOptCandidatesUiStateSignature(nextState);
    if (uiStateEmitSignatureRef.current === signature) return;
    uiStateEmitSignatureRef.current = signature;
    onUiStateChange(nextState);
  };

  const predictionForBackend = (smiles: string): LeadOptPredictionRecord | null => {
    const record = predictionBySmiles[smiles];
    if (!record) return null;
    const recordBackend = normalizeBackend(readText(record.backend));
    return recordBackend === selectedBackendKey ? record : null;
  };

  useEffect(() => {
    const signature = buildLeadOptCandidatesUiStateSignature(normalizedInitialUiState);
    if (uiStateHydrationSignatureRef.current === signature) return;
    uiStateHydrationSignatureRef.current = signature;
    setSelectedBackend(normalizedInitialUiState.selectedBackend);
    setStateFilter(normalizedInitialUiState.stateFilter);
    setShowAdvanced(normalizedInitialUiState.showAdvanced);
    setMwMin(normalizedInitialUiState.mwMin);
    setMwMax(normalizedInitialUiState.mwMax);
    setLogpMin(normalizedInitialUiState.logpMin);
    setLogpMax(normalizedInitialUiState.logpMax);
    setTpsaMin(normalizedInitialUiState.tpsaMin);
    setTpsaMax(normalizedInitialUiState.tpsaMax);
    setStructureSearchMode(normalizedInitialUiState.structureSearchMode);
    setStructureSearchQuery(normalizedInitialUiState.structureSearchQuery);
    setPreviewRenderMode(normalizedInitialUiState.previewRenderMode);
    setDebouncedStructureSearchQuery(normalizedInitialUiState.structureSearchQuery);
    setPage(1);
    setPageInput('1');
  }, [normalizedInitialUiState]);

  useEffect(() => {
    setPage(1);
    setPageInput('1');
  }, [enumeratedCandidates]);

  useEffect(() => {
    setSelectedBackend((prev) => (prev ? prev : normalizeBackend(defaultPredictionBackend)));
  }, [defaultPredictionBackend]);

  useEffect(() => {
    if (typeof onUiStateChange !== 'function') return;
    const nextState = buildUiState();
    const signature = buildLeadOptCandidatesUiStateSignature(nextState);
    const timer = window.setTimeout(() => {
      if (uiStateEmitSignatureRef.current === signature) return;
      uiStateEmitSignatureRef.current = signature;
      onUiStateChange(nextState);
    }, 180);
    return () => window.clearTimeout(timer);
  }, [
    logpMax,
    logpMin,
    mwMax,
    mwMin,
    onUiStateChange,
    selectedBackend,
    showAdvanced,
    stateFilter,
    previewRenderMode,
    structureSearchMode,
    structureSearchQuery,
    tpsaMax,
    tpsaMin
  ]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebouncedStructureSearchQuery(structureSearchQuery);
    }, 180);
    return () => window.clearTimeout(timer);
  }, [structureSearchQuery]);

  useEffect(() => {
    const query = debouncedStructureSearchQuery.trim();
    if (!query) {
      setStructureSearchLoading(false);
      setStructureSearchError(null);
      setStructureSearchMatches({});
      return;
    }

    let cancelled = false;
    const run = async () => {
      setStructureSearchLoading(true);
      setStructureSearchError(null);
      try {
        if (structureSearchMode === 'exact') {
          const normalizedQuery = normalizeSmilesForSearch(query);
          const nextMatches: Record<string, boolean> = {};
          for (let idx = 0; idx < enumeratedCandidates.length; idx += 1) {
            if (idx > 0 && idx % 32 === 0) {
              await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
              if (cancelled) return;
            }
            const row = enumeratedCandidates[idx];
            const smiles = readText(row.smiles).trim();
            nextMatches[smiles] = normalizeSmilesForSearch(smiles) === normalizedQuery;
          }
          if (!cancelled) setStructureSearchMatches(nextMatches);
          return;
        }

        const rdkit = await loadRDKitModule();
        if (cancelled) return;
        const queryMol = (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(query) : null) || rdkit.get_mol(query);
        if (!queryMol) throw new Error('Invalid SMARTS/SMILES query.');

        const nextMatches: Record<string, boolean> = {};
        try {
          for (let idx = 0; idx < enumeratedCandidates.length; idx += 1) {
            if (idx > 0 && idx % 16 === 0) {
              await new Promise<void>((resolve) => window.setTimeout(resolve, 0));
              if (cancelled) return;
            }
            const row = enumeratedCandidates[idx];
            const smiles = readText(row.smiles).trim();
            if (!smiles) {
              nextMatches[smiles] = false;
              continue;
            }
            const mol = rdkit.get_mol(smiles);
            if (!mol) {
              nextMatches[smiles] = false;
              continue;
            }
            try {
              let matched = false;
              if (typeof mol.get_substruct_match === 'function') {
                matched = hasSubstructureMatchPayload(mol.get_substruct_match(queryMol));
              }
              if (!matched && typeof mol.get_substruct_matches === 'function') {
                matched = hasSubstructureMatchPayload(mol.get_substruct_matches(queryMol));
              }
              nextMatches[smiles] = matched;
            } finally {
              mol.delete();
            }
          }
        } finally {
          queryMol.delete();
        }

        if (!cancelled) setStructureSearchMatches(nextMatches);
      } catch (error) {
        if (!cancelled) {
          setStructureSearchError(error instanceof Error ? error.message : 'Structure search failed.');
          setStructureSearchMatches({});
        }
      } finally {
        if (!cancelled) setStructureSearchLoading(false);
      }
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [debouncedStructureSearchQuery, enumeratedCandidates, structureSearchMode]);

  const baseRows = useMemo<Array<Record<string, unknown>>>(() => {
    return [...enumeratedCandidates].sort((a, b) => {
      const bn = readNumberOrNull(b.n_pairs) ?? 0;
      const an = readNumberOrNull(a.n_pairs) ?? 0;
      if (bn !== an) return bn - an;
      return Math.abs(readNumberOrNull(b.median_delta) ?? 0) - Math.abs(readNumberOrNull(a.median_delta) ?? 0);
    });
  }, [enumeratedCandidates]);

  const rows = useMemo(() => {
    let filtered = baseRows;
    if (stateFilter !== 'all' && !cardMode) {
      filtered = filtered.filter((row) => {
        const smiles = readText(row.smiles).trim();
        const predictionState = normalizeState(predictionForBackend(smiles)?.state);
        if (stateFilter === 'pending') return predictionState === 'UNSCORED';
        if (stateFilter === 'queued') return predictionState === 'QUEUED';
        if (stateFilter === 'running') return predictionState === 'RUNNING';
        if (stateFilter === 'success') return predictionState === 'SUCCESS';
        if (stateFilter === 'failure') return predictionState === 'FAILURE';
        return true;
      });
    }
    const applyStructureFilter = Boolean(structureSearchQuery.trim()) && !structureSearchLoading && !structureSearchError;
    if (applyStructureFilter) {
      filtered = filtered.filter((row) => {
        const smiles = readText(row.smiles).trim();
        return Boolean(structureSearchMatches[smiles]);
      });
    }

    const mwMinValue = parseOptionalNumber(mwMin);
    const mwMaxValue = parseOptionalNumber(mwMax);
    const logpMinValue = parseOptionalNumber(logpMin);
    const logpMaxValue = parseOptionalNumber(logpMax);
    const tpsaMinValue = parseOptionalNumber(tpsaMin);
    const tpsaMaxValue = parseOptionalNumber(tpsaMax);
    if (
      mwMinValue !== null ||
      mwMaxValue !== null ||
      logpMinValue !== null ||
      logpMaxValue !== null ||
      tpsaMinValue !== null ||
      tpsaMaxValue !== null
    ) {
      filtered = filtered.filter((row) => {
        const properties = (row.properties as Record<string, unknown>) || {};
        const mw = readNumberOrNull(properties.molecular_weight);
        const logp = readNumberOrNull(properties.logp);
        const tpsa = readNumberOrNull(properties.tpsa);
        if (mwMinValue !== null && (mw === null || mw < mwMinValue)) return false;
        if (mwMaxValue !== null && (mw === null || mw > mwMaxValue)) return false;
        if (logpMinValue !== null && (logp === null || logp < logpMinValue)) return false;
        if (logpMaxValue !== null && (logp === null || logp > logpMaxValue)) return false;
        if (tpsaMinValue !== null && (tpsa === null || tpsa < tpsaMinValue)) return false;
        if (tpsaMaxValue !== null && (tpsa === null || tpsa > tpsaMaxValue)) return false;
        return true;
      });
    }
    return filtered;
  }, [
    baseRows,
    logpMax,
    logpMin,
    mwMax,
    mwMin,
    cardMode,
    predictionBySmiles,
    selectedBackendKey,
    stateFilter,
    structureSearchError,
    structureSearchLoading,
    structureSearchMatches,
    structureSearchQuery,
    tpsaMax,
    tpsaMin
  ]);

  const renderedRows = useMemo(() => {
    if (!cardMode) return rows;
    return rows.filter((row) => {
      const smiles = readText(row.smiles).trim();
      return normalizeState(predictionForBackend(smiles)?.state) === 'SUCCESS';
    });
  }, [cardMode, predictionBySmiles, rows, selectedBackendKey]);

  const totalPages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  const clampedPage = Math.max(1, Math.min(totalPages, page));
  const pageRows = useMemo(
    () => rows.slice((clampedPage - 1) * PAGE_SIZE, clampedPage * PAGE_SIZE),
    [rows, clampedPage]
  );
  const cardRows = useMemo(() => renderedRows.slice(0, visibleCardCount), [renderedRows, visibleCardCount]);
  const hasMoreCardRows = cardMode && visibleCardCount < renderedRows.length;
  const matchedCount = useMemo(() => rows.filter((row) => structureSearchMatches[readText(row.smiles).trim()]).length, [rows, structureSearchMatches]);
  const referencePrediction = referencePredictionByBackend[selectedBackendKey];
  const referencePredictionState = normalizeState(referencePrediction?.state);
  const referencePlddt = referencePredictionState === 'SUCCESS' ? referencePrediction?.ligandPlddt ?? null : null;
  const referenceIptm = referencePredictionState === 'SUCCESS' ? referencePrediction?.pairIptm ?? null : null;
  const referencePae = referencePredictionState === 'SUCCESS' ? referencePrediction?.pairPae ?? null : null;
  const sliderMwMin = Math.max(100, Math.min(800, parseOptionalNumber(mwMin) ?? 100));
  const sliderMwMax = Math.max(sliderMwMin, Math.min(800, parseOptionalNumber(mwMax) ?? 800));
  const sliderLogpMin = Math.max(-2, Math.min(8, parseOptionalNumber(logpMin) ?? -2));
  const sliderLogpMax = Math.max(sliderLogpMin, Math.min(8, parseOptionalNumber(logpMax) ?? 8));
  const sliderTpsaMin = Math.max(0, Math.min(220, parseOptionalNumber(tpsaMin) ?? 0));
  const sliderTpsaMax = Math.max(sliderTpsaMin, Math.min(220, parseOptionalNumber(tpsaMax) ?? 220));

  useEffect(() => {
    setPageInput(String(clampedPage));
  }, [clampedPage]);

  useEffect(() => {
    if (!cardMode) return;
    setVisibleCardCount(CARD_BATCH_SIZE);
  }, [CARD_BATCH_SIZE, cardMode, rows]);

  useEffect(() => {
    if (!cardMode || !hasMoreCardRows) return;
    const node = cardLoadMoreRef.current;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    const root = node.closest('.lead-opt-mmp-main--viewer-open') as Element | null;
    const observer = new IntersectionObserver(
      (entries) => {
        if (!entries.some((entry) => entry.isIntersecting)) return;
        setVisibleCardCount((current) => Math.min(renderedRows.length, current + CARD_BATCH_SIZE));
      },
      {
        root,
        rootMargin: '280px 0px',
        threshold: 0
      }
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [CARD_BATCH_SIZE, cardMode, hasMoreCardRows, renderedRows.length]);

  useEffect(() => {
    if (previewRenderMode !== 'confidence') return;
    if (typeof onEnsurePredictionResult !== 'function') return;
    const visibleRows = cardMode ? cardRows : pageRows;
    if (visibleRows.length === 0) return;
    const now = Date.now();
    const pendingHydrationSmiles: string[] = [];
    for (const row of visibleRows) {
      const smiles = readText(row.smiles).trim();
      if (!smiles) continue;
      const record = predictionForBackend(smiles);
      if (!record || normalizeState(record.state) !== 'SUCCESS') continue;
      if ((record.ligandAtomPlddts || []).length > 0) continue;
      if (confidenceHydrationInFlightRef.current[smiles]) continue;
      const lastAttempt = Number(confidenceHydrationAttemptAtRef.current[smiles] || 0);
      if (now - lastAttempt < CONFIDENCE_HYDRATION_COOLDOWN_MS) continue;
      pendingHydrationSmiles.push(smiles);
      if (pendingHydrationSmiles.length >= CONFIDENCE_HYDRATION_MAX_PER_TICK) break;
    }
    if (pendingHydrationSmiles.length === 0) return;
    for (const smiles of pendingHydrationSmiles) {
      confidenceHydrationInFlightRef.current[smiles] = true;
      confidenceHydrationAttemptAtRef.current[smiles] = now;
      void onEnsurePredictionResult(smiles)
        .catch(() => null)
        .finally(() => {
          delete confidenceHydrationInFlightRef.current[smiles];
          confidenceHydrationAttemptAtRef.current[smiles] = Date.now();
        });
    }
  }, [cardMode, cardRows, onEnsurePredictionResult, pageRows, predictionBySmiles, previewRenderMode, selectedBackendKey]);

  const renderPredictAction = (
    smiles: string,
    predictionState: 'SUCCESS' | 'RUNNING' | 'FAILURE' | 'QUEUED' | 'UNSCORED'
  ) => {
    const isRunning = predictionState === 'RUNNING';
    const isQueued = predictionState === 'QUEUED';
    const isPending = isRunning || isQueued;
    const isActionDisabled = isPending || loading || !referenceReady || !smiles;
    const buttonClass = [
      'lead-opt-row-action-btn',
      !isPending ? 'lead-opt-row-action-btn-primary' : '',
      isRunning ? 'lead-opt-row-action-btn--running' : '',
      isQueued ? 'lead-opt-row-action-btn--queued' : ''
    ]
      .filter(Boolean)
      .join(' ');
    const title = isRunning
      ? 'Running'
      : isQueued
        ? 'Queued'
        : referenceReady
          ? 'Run structure prediction'
          : 'Upload reference first';
    const ariaLabel = isRunning ? 'Running' : isQueued ? 'Queued' : 'Run structure prediction';
    return (
      <button
        type="button"
        className={buttonClass}
        onClick={isPending ? undefined : () => onRunPredictCandidate(smiles, selectedBackendKey)}
        disabled={isActionDisabled}
        title={title}
        aria-label={ariaLabel}
      >
        {isRunning ? <Loader2 size={14} className="spinning" /> : isQueued ? <Clock3 size={14} /> : <Play size={14} />}
      </button>
    );
  };

  return (
    <section
      id={sectionId}
      className={`lead-opt-candidates-panel${cardMode ? ' lead-opt-candidates-panel--card-mode' : ''}`}
    >
      <div className="lead-opt-panel-head">
        <div className="lead-opt-query-toolbar lead-opt-query-toolbar--single-row">
          {cardMode ? (
            typeof onExitCardMode === 'function' ? (
              <button
                type="button"
                className="lead-opt-row-action-btn lead-opt-card-exit-btn"
                onClick={onExitCardMode}
                aria-label="Exit cards"
                title="Exit cards"
              >
                <X size={14} />
              </button>
            ) : null
          ) : (
            <select
              className="lead-opt-backend-select lead-opt-state-filter-select"
              value={stateFilter}
              onChange={(event) => {
                setStateFilter(
                  event.target.value as 'all' | 'pending' | 'queued' | 'running' | 'success' | 'failure'
                );
                setPage(1);
              }}
            >
              <option value="all">All states</option>
              <option value="pending">Pending</option>
              <option value="queued">Queued</option>
              <option value="running">Running</option>
              <option value="success">Success</option>
              <option value="failure">Failure</option>
            </select>
          )}
          <span className="lead-opt-query-toolbar-spacer" />
          <div className="lead-opt-query-toolbar-right">
            {!cardMode ? (
              <select
                className="lead-opt-backend-select lead-opt-backend-select--engine"
                value={selectedBackend}
                onChange={(event) => setSelectedBackend(normalizeBackend(event.target.value))}
              >
                {BACKEND_OPTIONS.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label}
                  </option>
                ))}
              </select>
            ) : null}
            <div className="lead-opt-render-mode-switch" role="tablist" aria-label="2D preview render mode">
              <button
                type="button"
                role="tab"
                aria-selected={previewRenderMode === 'confidence'}
                className={`lead-opt-render-mode-btn ${previewRenderMode === 'confidence' ? 'active' : ''}`}
                onClick={() => {
                  setPreviewRenderMode('confidence');
                  onPreviewRenderModeChange?.('confidence');
                  emitUiStateNow({ previewRenderMode: 'confidence' });
                }}
                title="Color by model confidence"
              >
                Confidence
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={previewRenderMode === 'fragment'}
                className={`lead-opt-render-mode-btn ${previewRenderMode === 'fragment' ? 'active' : ''}`}
                onClick={() => {
                  setPreviewRenderMode('fragment');
                  onPreviewRenderModeChange?.('fragment');
                  emitUiStateNow({ previewRenderMode: 'fragment' });
                }}
                title="Highlight modified fragment"
              >
                Fragment
              </button>
            </div>
            <button
              type="button"
              className="btn btn-ghost btn-compact"
              onClick={() => setShowAdvanced((prev) => !prev)}
              title={showAdvanced ? 'Hide advanced filters' : 'Show advanced filters'}
            >
              <SlidersHorizontal size={14} />
              Advanced
            </button>
          </div>
        </div>
      </div>

      {showAdvanced ? (
        <div className="lead-opt-candidate-advanced">
          <div className="lead-opt-candidate-advanced-head">
            <span>Structure Query</span>
            <div className="task-structure-mode-switch" role="tablist" aria-label="Structure search mode">
              <button
                type="button"
                role="tab"
                aria-selected={structureSearchMode === 'exact'}
                className={`task-structure-mode-btn ${structureSearchMode === 'exact' ? 'active' : ''}`}
                onClick={() => setStructureSearchMode('exact')}
              >
                Exact
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={structureSearchMode === 'substructure'}
                className={`task-structure-mode-btn ${structureSearchMode === 'substructure' ? 'active' : ''}`}
                onClick={() => setStructureSearchMode('substructure')}
              >
                Substructure
              </button>
            </div>
          </div>
          <div className="jsme-editor-container task-structure-jsme-shell">
            <JSMEEditor smiles={structureSearchQuery} onSmilesChange={setStructureSearchQuery} height={360} />
          </div>
          <div className={`task-structure-query-status ${structureSearchError ? 'is-error' : ''}`}>
            {structureSearchLoading
              ? 'Searching...'
              : structureSearchError
                ? 'Invalid query'
                : structureSearchQuery.trim()
                  ? `Matched ${matchedCount}`
                  : 'Draw query'}
          </div>
          <div className="lead-opt-slider-grid">
            <div className="lead-opt-slider-card">
              <div className="lead-opt-slider-head">
                <span>MW</span>
                <span>{sliderMwMin.toFixed(0)} - {sliderMwMax.toFixed(0)}</span>
              </div>
              <div className="lead-opt-slider-row">
                <input
                  type="range"
                  min={100}
                  max={800}
                  step={1}
                  value={sliderMwMin}
                  onChange={(event) => {
                    const next = Number(event.target.value);
                    setMwMin(String(Math.min(next, sliderMwMax)));
                  }}
                />
                <input
                  type="range"
                  min={100}
                  max={800}
                  step={1}
                  value={sliderMwMax}
                  onChange={(event) => {
                    const next = Number(event.target.value);
                    setMwMax(String(Math.max(next, sliderMwMin)));
                  }}
                />
              </div>
            </div>
            <div className="lead-opt-slider-card">
              <div className="lead-opt-slider-head">
                <span>LogP</span>
                <span>{sliderLogpMin.toFixed(1)} - {sliderLogpMax.toFixed(1)}</span>
              </div>
              <div className="lead-opt-slider-row">
                <input
                  type="range"
                  min={-2}
                  max={8}
                  step={0.1}
                  value={sliderLogpMin}
                  onChange={(event) => {
                    const next = Number(event.target.value);
                    setLogpMin(String(Math.min(next, sliderLogpMax).toFixed(1)));
                  }}
                />
                <input
                  type="range"
                  min={-2}
                  max={8}
                  step={0.1}
                  value={sliderLogpMax}
                  onChange={(event) => {
                    const next = Number(event.target.value);
                    setLogpMax(String(Math.max(next, sliderLogpMin).toFixed(1)));
                  }}
                />
              </div>
            </div>
            <div className="lead-opt-slider-card">
              <div className="lead-opt-slider-head">
                <span>TPSA</span>
                <span>{sliderTpsaMin.toFixed(0)} - {sliderTpsaMax.toFixed(0)}</span>
              </div>
              <div className="lead-opt-slider-row">
                <input
                  type="range"
                  min={0}
                  max={220}
                  step={1}
                  value={sliderTpsaMin}
                  onChange={(event) => {
                    const next = Number(event.target.value);
                    setTpsaMin(String(Math.min(next, sliderTpsaMax)));
                  }}
                />
                <input
                  type="range"
                  min={0}
                  max={220}
                  step={1}
                  value={sliderTpsaMax}
                  onChange={(event) => {
                    const next = Number(event.target.value);
                    setTpsaMax(String(Math.max(next, sliderTpsaMin)));
                  }}
                />
              </div>
            </div>
          </div>
          <div className="lead-opt-range-grid">
            <label>
              MW min
              <input value={mwMin} onChange={(event) => setMwMin(event.target.value)} placeholder="280" />
            </label>
            <label>
              MW max
              <input value={mwMax} onChange={(event) => setMwMax(event.target.value)} placeholder="520" />
            </label>
            <label>
              LogP min
              <input value={logpMin} onChange={(event) => setLogpMin(event.target.value)} placeholder="1.0" />
            </label>
            <label>
              LogP max
              <input value={logpMax} onChange={(event) => setLogpMax(event.target.value)} placeholder="4.5" />
            </label>
            <label>
              TPSA min
              <input value={tpsaMin} onChange={(event) => setTpsaMin(event.target.value)} placeholder="40" />
            </label>
            <label>
              TPSA max
              <input value={tpsaMax} onChange={(event) => setTpsaMax(event.target.value)} placeholder="120" />
            </label>
          </div>
        </div>
      ) : null}

      {renderedRows.length === 0 ? (
        loading ? (
          <div className="lead-opt-loading-state" role="status" aria-live="polite">
            <span className="lead-opt-loading-spinner" />
            <span>Building rows...</span>
          </div>
        ) : (
          <p className="muted small">{cardMode ? 'No scored candidates yet.' : 'Run MMP to build results.'}</p>
        )
      ) : (
        cardMode ? (
          <div className="lead-opt-card-list">
            {cardRows.map((row, index) => {
              const smiles = readText(row.smiles).trim();
              const properties = (row.properties as Record<string, unknown>) || {};
              const highlightAtomIndices = normalizeAtomIndices(row.final_highlight_atom_indices);
              const prediction = predictionForBackend(smiles);
              const predictionState = normalizeState(prediction?.state);
              const mw = readNumberOrNull(properties.molecular_weight);
              const logp = readNumberOrNull(properties.logp);
              const tpsa = readNumberOrNull(properties.tpsa);
              const plddtValue = prediction?.ligandPlddt ?? null;
              const iptmValue = prediction?.pairIptm ?? null;
              const paeValue = prediction?.pairPae ?? null;
              const plddtTone = resolveConfidenceTone(plddtValue);
              const useConfidenceRender = previewRenderMode === 'confidence';
              const isActive = activeSmiles === smiles;
              const rowDelta = readNumberOrNull(row.median_delta);
              const rowDeltaTone = resolveDeltaTone(rowDelta);
              const cardOpenable = Boolean(smiles);
              return (
                <article
                  key={smiles || `${clampedPage}-${index}`}
                  className={`lead-opt-result-card${isActive ? ' selected' : ''}${cardOpenable ? ' is-clickable' : ''}`}
                  onClick={() => {
                    if (!smiles) return;
                    onActiveSmilesChange(smiles);
                    if (cardOpenable) onOpenPredictionResult(smiles, highlightAtomIndices);
                  }}
                  onKeyDown={(event) => {
                    if (!cardOpenable || (event.key !== 'Enter' && event.key !== ' ')) return;
                    event.preventDefault();
                    onActiveSmilesChange(smiles);
                    onOpenPredictionResult(smiles, highlightAtomIndices);
                  }}
                  role={cardOpenable ? 'button' : undefined}
                  tabIndex={cardOpenable ? 0 : undefined}
                  aria-label={cardOpenable ? `Open 3D for candidate ${index + 1}` : undefined}
                >
                  <div className="lead-opt-result-card-head">
                    <strong>#{index + 1}</strong>
                    <span className="muted small">Pairs {readText(row.n_pairs) || '-'}</span>
                    <span className={`lead-opt-delta-indicator ${rowDeltaTone}`}>
                      <DeltaSpark tone={rowDeltaTone} />
                      <span className="lead-opt-delta-value">{rowDelta !== null ? formatDelta(rowDelta, 2) : '-'}</span>
                    </span>
                  </div>
                  <div className="lead-opt-result-card-media">
                    <div
                      className={`lead-opt-structure-hit ${
                        predictionState === 'SUCCESS' ? `has-prediction conf-${plddtTone}` : ''
                      }`}
                      title="Click card to open 3D model"
                    >
                      <MemoLigand2DPreview
                        smiles={smiles}
                        width={CARD_CANDIDATE_2D_WIDTH}
                        height={CARD_PREVIEW_2D_HEIGHT}
                        highlightAtomIndices={useConfidenceRender ? null : highlightAtomIndices}
                        templateSmiles={referenceSmiles}
                        strictTemplateAlignment
                        atomConfidences={useConfidenceRender ? prediction?.ligandAtomPlddts || null : null}
                        confidenceHint={useConfidenceRender ? prediction?.ligandPlddt ?? null : null}
                      />
                    </div>
                  </div>
                  <div className="lead-opt-card-metric-strip">
                    <span className={`lead-opt-card-pill physchem-${resolvePhyschemTone('mw', mw)}`}>
                      <span className="lead-opt-card-pill-key">MW</span>
                      <strong>{formatMetric(mw, 1)}</strong>
                    </span>
                    <span className={`lead-opt-card-pill physchem-${resolvePhyschemTone('logp', logp)}`}>
                      <span className="lead-opt-card-pill-key">LogP</span>
                      <strong>{formatMetric(logp, 2)}</strong>
                    </span>
                    <span className={`lead-opt-card-pill physchem-${resolvePhyschemTone('tpsa', tpsa)}`}>
                      <span className="lead-opt-card-pill-key">TPSA</span>
                      <strong>{formatMetric(tpsa, 1)}</strong>
                    </span>
                    <span className={`lead-opt-card-pill conf-tone-${plddtTone}`}>
                      <span className="lead-opt-card-pill-key">pLDDT</span>
                      <strong>{plddtValue !== null ? formatMetric(plddtValue, 1) : '-'}</strong>
                    </span>
                    <span
                      className={`lead-opt-card-pill conf-tone-${resolveConfidenceTone(
                        iptmValue !== null && Number.isFinite(iptmValue) ? iptmValue * 100 : null
                      )}`}
                    >
                      <span className="lead-opt-card-pill-key">iPTM</span>
                      <strong>{iptmValue !== null ? formatMetric(iptmValue, 3) : '-'}</strong>
                    </span>
                    <span className={`lead-opt-card-pill conf-tone-${resolvePaeTone(paeValue)}`}>
                      <span className="lead-opt-card-pill-key">PAE</span>
                      <strong>{paeValue !== null ? formatMetric(paeValue, 2) : '-'}</strong>
                    </span>
                  </div>
                </article>
              );
            })}
            {hasMoreCardRows ? <div ref={cardLoadMoreRef} className="lead-opt-card-load-sentinel" aria-hidden="true" /> : null}
          </div>
        ) : (
          <div className="lead-opt-result-table-wrap">
            <table className="lead-opt-candidate-table lead-opt-result-table">
              <thead>
                <tr>
                  <th className="col-rank">#</th>
                  <th className="col-structure">2D</th>
                  <th className="col-n">Pairs</th>
                  <th className="col-delta">Median Δ</th>
                  <th className="col-insights col-insights-physchem">PhysChem</th>
                  <th className="col-insights col-insights-model">Model Profile</th>
                  <th className="col-state">State</th>
                  <th className="col-actions">Run</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((row, index) => {
                  const smiles = readText(row.smiles).trim();
                  const properties = (row.properties as Record<string, unknown>) || {};
                  const deltas = (row.property_deltas as Record<string, unknown>) || {};
                  const highlightAtomIndices = normalizeAtomIndices(row.final_highlight_atom_indices);
                  const prediction = predictionForBackend(smiles);
                  const predictionState = normalizeState(prediction?.state);
                  const rowTone = stateClass(predictionState);
                  const mw = readNumberOrNull(properties.molecular_weight);
                  const logp = readNumberOrNull(properties.logp);
                  const tpsa = readNumberOrNull(properties.tpsa);
                  const refMw = deriveReferenceProperty(row, 'mw');
                  const refLogp = deriveReferenceProperty(row, 'logp');
                  const refTpsa = deriveReferenceProperty(row, 'tpsa');
                  const plddtValue = prediction?.ligandPlddt ?? null;
                  const iptmValue = prediction?.pairIptm ?? null;
                  const paeValue = prediction?.pairPae ?? null;
                  const plddtDelta = plddtValue !== null && referencePlddt !== null ? plddtValue - referencePlddt : null;
                  const iptmDelta = iptmValue !== null && referenceIptm !== null ? iptmValue - referenceIptm : null;
                  const paeDelta = paeValue !== null && referencePae !== null ? paeValue - referencePae : null;
                  const plddtTone = resolveConfidenceTone(plddtValue);
                  const iptmTone = resolveConfidenceTone(
                    iptmValue !== null && Number.isFinite(iptmValue) ? iptmValue * 100 : null
                  );
                  const paeTone = resolvePaeTone(paeValue);
                  const plddtDeltaTone = resolveDeltaTone(plddtDelta);
                  const iptmDeltaTone = resolveDeltaTone(iptmDelta);
                  const paeDeltaTone = resolveDeltaTone(paeDelta === null ? null : -paeDelta);
                  const isActive = activeSmiles === smiles;
                  const useConfidenceRender = previewRenderMode === 'confidence';
                  const mwDelta = readNumberOrNull(deltas.mw);
                  const logpDelta = readNumberOrNull(deltas.logp);
                  const tpsaDelta = readNumberOrNull(deltas.tpsa);
                  const rowDelta = readNumberOrNull(row.median_delta);
                  const mwDeltaTone = resolveDeltaTone(mwDelta);
                  const logpDeltaTone = resolveDeltaTone(logpDelta);
                  const tpsaDeltaTone = resolveDeltaTone(tpsaDelta);
                  const rowDeltaTone = resolveDeltaTone(rowDelta);
                  return (
                    <tr key={smiles || `${clampedPage}-${index}`} className={isActive ? 'selected' : ''}>
                      <td className="col-rank">{(clampedPage - 1) * PAGE_SIZE + index + 1}</td>
                      <td className="col-structure">
                        <button
                          type="button"
                          className={`lead-opt-structure-hit ${
                            predictionState === 'SUCCESS' ? `has-prediction conf-${plddtTone}` : ''
                          }`}
                          onClick={() => {
                            if (!smiles) return;
                            onActiveSmilesChange(smiles);
                            if (predictionState === 'SUCCESS') onOpenPredictionResult(smiles, highlightAtomIndices);
                          }}
                          title={predictionState === 'SUCCESS' ? 'Open 3D model' : 'Select candidate'}
                        >
                          <MemoLigand2DPreview
                            smiles={smiles}
                            width={TABLE_CANDIDATE_2D_WIDTH}
                            height={PREVIEW_2D_HEIGHT}
                            highlightAtomIndices={useConfidenceRender ? null : highlightAtomIndices}
                            templateSmiles={referenceSmiles}
                            strictTemplateAlignment
                            atomConfidences={useConfidenceRender ? prediction?.ligandAtomPlddts || null : null}
                            confidenceHint={useConfidenceRender ? prediction?.ligandPlddt ?? null : null}
                          />
                        </button>
                      </td>
                      <td className="col-n">{readText(row.n_pairs) || '-'}</td>
                      <td className="col-delta">
                        <span className={`lead-opt-delta-indicator ${rowDeltaTone}`}>
                          <DeltaSpark tone={rowDeltaTone} />
                          <span className="lead-opt-delta-value">{rowDelta !== null ? formatDelta(rowDelta, 2) : '-'}</span>
                        </span>
                      </td>
                      <td className="col-insights col-insights-physchem">
                        <div className="lead-opt-info-col">
                          <div className="lead-opt-insight-item">
                            <span className="lead-opt-insight-main">
                              <span className="lead-opt-insight-label">MW</span>
                              <span className={`lead-opt-insight-value physchem-${resolvePhyschemTone('mw', mw)}`}>{formatMetric(mw, 1)}</span>
                            </span>
                            <span className={`lead-opt-delta-indicator ${mwDeltaTone}`}>
                              <DeltaSpark tone={mwDeltaTone} />
                              <span className="lead-opt-delta-value">{refMw !== null ? formatDelta(mwDelta, 2) : '-'}</span>
                            </span>
                          </div>
                          <div className="lead-opt-insight-item">
                            <span className="lead-opt-insight-main">
                              <span className="lead-opt-insight-label">LogP</span>
                              <span className={`lead-opt-insight-value physchem-${resolvePhyschemTone('logp', logp)}`}>{formatMetric(logp, 2)}</span>
                            </span>
                            <span className={`lead-opt-delta-indicator ${logpDeltaTone}`}>
                              <DeltaSpark tone={logpDeltaTone} />
                              <span className="lead-opt-delta-value">{refLogp !== null ? formatDelta(logpDelta, 2) : '-'}</span>
                            </span>
                          </div>
                          <div className="lead-opt-insight-item">
                            <span className="lead-opt-insight-main">
                              <span className="lead-opt-insight-label">TPSA</span>
                              <span className={`lead-opt-insight-value physchem-${resolvePhyschemTone('tpsa', tpsa)}`}>{formatMetric(tpsa, 1)}</span>
                            </span>
                            <span className={`lead-opt-delta-indicator ${tpsaDeltaTone}`}>
                              <DeltaSpark tone={tpsaDeltaTone} />
                              <span className="lead-opt-delta-value">{refTpsa !== null ? formatDelta(tpsaDelta, 2) : '-'}</span>
                            </span>
                          </div>
                        </div>
                      </td>
                      <td className="col-insights col-insights-model">
                        <div className="lead-opt-info-col lead-opt-info-col--profile">
                          <div className={`lead-opt-confidence-row conf-tone-${plddtTone}`}>
                            <span className="lead-opt-confidence-label">pLDDT</span>
                            <strong className="lead-opt-confidence-value">{plddtValue !== null ? formatMetric(plddtValue, 1) : '-'}</strong>
                            <span className={`lead-opt-delta-indicator ${plddtDeltaTone}`}>
                              <DeltaSpark tone={plddtDeltaTone} />
                              <span className="lead-opt-delta-value">{plddtDelta !== null ? formatDelta(plddtDelta, 2) : '-'}</span>
                            </span>
                          </div>
                          <div className={`lead-opt-confidence-row conf-tone-${iptmTone}`}>
                            <span className="lead-opt-confidence-label">iPTM</span>
                            <strong className="lead-opt-confidence-value">{iptmValue !== null ? formatMetric(iptmValue, 3) : '-'}</strong>
                            <span className={`lead-opt-delta-indicator ${iptmDeltaTone}`}>
                              <DeltaSpark tone={iptmDeltaTone} />
                              <span className="lead-opt-delta-value">{iptmDelta !== null ? formatDelta(iptmDelta, 3) : '-'}</span>
                            </span>
                          </div>
                          <div className={`lead-opt-confidence-row conf-tone-${paeTone}`}>
                            <span className="lead-opt-confidence-label">PAE</span>
                            <strong className="lead-opt-confidence-value">{paeValue !== null ? formatMetric(paeValue, 2) : '-'}</strong>
                            <span className={`lead-opt-delta-indicator ${paeDeltaTone}`}>
                              <DeltaSpark tone={paeDeltaTone} />
                              <span className="lead-opt-delta-value">{paeDelta !== null ? formatDelta(paeDelta, 2) : '-'}</span>
                            </span>
                          </div>
                        </div>
                      </td>
                      <td className="col-state">
                        <span className={`lead-opt-state-pill ${rowTone}`}>
                          {predictionState === 'SUCCESS' ? <CheckCircle2 size={13} /> : null}
                          {predictionState === 'RUNNING' ? <Loader2 size={13} className="spinning" /> : null}
                          {predictionState === 'FAILURE' ? <AlertTriangle size={13} /> : null}
                          {predictionState === 'QUEUED' || predictionState === 'UNSCORED' ? <Clock3 size={13} /> : null}
                          {stateLabel(predictionState)}
                        </span>
                      </td>
                      <td className="col-actions" onClick={(event) => event.stopPropagation()}>
                        {renderPredictAction(smiles, predictionState)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      )}

      {!cardMode && rows.length > 0 ? (
        <div className="lead-opt-page-row">
          <span className="badge">Page {clampedPage}/{totalPages}</span>
          <button
            type="button"
            className="lead-opt-row-action-btn"
            onClick={() => setPage(1)}
            disabled={clampedPage <= 1}
            aria-label="First page"
            title="First page"
          >
            <ChevronsLeft size={14} />
          </button>
          <button
            type="button"
            className="lead-opt-row-action-btn"
            onClick={() => setPage((prev) => Math.max(1, prev - 1))}
            disabled={clampedPage <= 1}
            aria-label="Previous page"
            title="Previous page"
          >
            <ChevronLeft size={14} />
          </button>
          <button
            type="button"
            className="lead-opt-row-action-btn"
            onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
            disabled={clampedPage >= totalPages}
            aria-label="Next page"
            title="Next page"
          >
            <ChevronRight size={14} />
          </button>
          <button
            type="button"
            className="lead-opt-row-action-btn"
            onClick={() => setPage(totalPages)}
            disabled={clampedPage >= totalPages}
            aria-label="Last page"
            title="Last page"
          >
            <ChevronsRight size={14} />
          </button>
          <label className="project-page-size">
            <span className="muted small">Go to</span>
            <input
              type="number"
              min={1}
              max={totalPages}
              value={pageInput}
              onChange={(event) => {
                const nextRaw = event.target.value;
                setPageInput(nextRaw);
                const parsed = Number(nextRaw);
                if (!Number.isFinite(parsed)) return;
                setPage(Math.max(1, Math.min(totalPages, Math.floor(parsed))));
              }}
              aria-label="Go to candidate page"
            />
          </label>
        </div>
      ) : null}
    </section>
  );
}
