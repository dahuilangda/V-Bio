import {
  AlertTriangle,
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  ChevronsLeft,
  ChevronsRight,
  Clock3,
  Download,
  Loader2,
  Play,
  Search,
  X
} from 'lucide-react';
import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import type {
  InputComponent,
  VirtualScreeningPredictionRecord,
  VirtualScreeningStructureBackend,
  VirtualScreeningStructureState
} from '../../types/models';
import { MemoLigand2DPreview } from './Ligand2DPreview';
import { MolstarViewer } from './MolstarViewer';
import { useVirtualScreeningPredictions } from './useVirtualScreeningPredictions';
import { asRecord } from '../../pages/projectTasks/recordReaders';

interface VirtualScreeningResultsSectionProps {
  screening: Record<string, unknown>;
  projectTaskId: string;
  projectTaskState: string;
  progressPercent: number;
  canPredictStructures: boolean;
  components: InputComponent[];
  predictionRecords?: Record<string, VirtualScreeningPredictionRecord>;
  onPredictionRecordsChange?: (records: Record<string, VirtualScreeningPredictionRecord>) => void;
}

type SortKey = 'rank' | 'name' | 'ic50' | 'pic50' | 'probability';
type SortDirection = 'asc' | 'desc';
type StateFilter = 'all' | 'unscored' | 'queued' | 'running' | 'success' | 'failure';
type DisplayState = VirtualScreeningStructureState | 'UNSCORED';

interface ScreeningRow {
  rank: number;
  id: string;
  name: string;
  smiles: string;
  ic50Um: number | null;
  pic50: number | null;
  probability: number | null;
  spread: number | null;
  entropy: number | null;
}

const PAGE_SIZE = 12;
const TABLE_PREVIEW_WIDTH = 288;
const TABLE_PREVIEW_HEIGHT = 138;
const CARD_PREVIEW_WIDTH = 190;
const CARD_PREVIEW_HEIGHT = 166;
const VIEWER_WIDTH_MIN = 380;
const RESULTS_WIDTH_MIN = 612;
const VIEWER_WIDTH_DEFAULT = 780;
const VIEWER_WIDTH_STEP = 24;
const RESIZER_WIDTH = 10;
const BACKEND_OPTIONS: Array<{ key: VirtualScreeningStructureBackend; label: string }> = [
  { key: 'boltz', label: 'Boltz2' },
  { key: 'protenix', label: 'Protenix' },
  { key: 'alphafold3', label: 'AF3' }
];

function readText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : value == null ? '' : String(value).trim();
}

function readFinite(value: unknown): number | null {
  if (typeof value === 'boolean' || value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}


function parseRows(screening: Record<string, unknown>): ScreeningRow[] {
  const compounds = Array.isArray(screening.compounds) ? screening.compounds : [];
  return compounds
    .map((value, index) => {
      const record = asRecord(value);
      const affinity = readFinite(record.affinity_pred_value);
      const explicitIc50 = readFinite(record.ic50_um);
      const ic50Um = explicitIc50 ?? (affinity === null ? null : Math.pow(10, affinity));
      const explicitPic50 = readFinite(record.pic50);
      return {
        rank: Math.max(1, Math.floor(readFinite(record.rank) ?? index + 1)),
        id: readText(record.id || record.record_id) || `compound-${index + 1}`,
        name: readText(record.name) || readText(record.id || record.record_id) || `Compound ${index + 1}`,
        smiles: readText(record.smiles || record.canonical_smiles),
        ic50Um,
        pic50: explicitPic50 ?? (affinity === null ? null : 6 - affinity),
        probability: readFinite(record.affinity_probability_binary),
        spread: readFinite(record.ensemble_spread),
        entropy: readFinite(record.entropy_crop_pl) ?? readFinite(record.entropy_pl)
      };
    })
    .filter((row) => Boolean(row.smiles || row.name));
}

function rowIdentity(row: ScreeningRow): string {
  return `${row.id}\n${row.smiles}`;
}

function formatNumber(value: number | null, digits = 2): string {
  if (value === null) return '-';
  return value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function formatIc50(value: number | null): string {
  if (value === null) return '-';
  if (value < 0.001 || value >= 10000) return `${value.toExponential(2)} μM`;
  return `${formatNumber(value, value < 1 ? 3 : 2)} μM`;
}

function formatProbability(value: number | null): string {
  return value === null ? '-' : `${formatNumber(value * 100, 1)}%`;
}

function csvCell(value: string | number | null): string {
  const rawText = value == null ? '' : String(value);
  const text = typeof value === 'string' && /^[=+\-@]/.test(rawText) ? `'${rawText}` : rawText;
  return `"${text.replace(/"/g, '""')}"`;
}

function displayState(record: VirtualScreeningPredictionRecord | null): DisplayState {
  return record?.state || 'UNSCORED';
}

function stateLabel(state: DisplayState): string {
  if (state === 'UNSCORED') return 'Not run';
  if (state === 'QUEUED') return 'Queued';
  if (state === 'RUNNING') return 'Running';
  if (state === 'SUCCESS') return 'Success';
  return 'Failed';
}

function stateTone(state: DisplayState): string {
  if (state === 'SUCCESS') return 'tone-success';
  if (state === 'RUNNING') return 'tone-running';
  if (state === 'FAILURE') return 'tone-failure';
  if (state === 'QUEUED') return 'tone-queued';
  return 'tone-unscored';
}

function metricTone(value: number | null, kind: 'confidence' | 'pae' = 'confidence'): string {
  if (value === null) return 'conf-tone-na';
  if (kind === 'pae') {
    if (value <= 5) return 'conf-tone-vhigh';
    if (value <= 15) return 'conf-tone-high';
    return 'conf-tone-vlow';
  }
  if (value >= 80) return 'conf-tone-vhigh';
  if (value >= 60) return 'conf-tone-high';
  if (value >= 0.8 && value <= 1) return 'conf-tone-vhigh';
  if (value >= 0.5 && value <= 1) return 'conf-tone-high';
  return 'conf-tone-vlow';
}

function StateIcon({ state }: { state: DisplayState }) {
  if (state === 'SUCCESS') return <CheckCircle2 size={13} />;
  if (state === 'RUNNING') return <Loader2 size={13} className="spinning" />;
  if (state === 'FAILURE') return <AlertTriangle size={13} />;
  return <Clock3 size={13} />;
}

export function VirtualScreeningResultsSection({
  screening,
  projectTaskId,
  projectTaskState,
  progressPercent,
  canPredictStructures,
  components,
  predictionRecords = {},
  onPredictionRecordsChange
}: VirtualScreeningResultsSectionProps) {
  const rows = useMemo(() => parseRows(screening), [screening]);
  const [query, setQuery] = useState('');
  const [stateFilter, setStateFilter] = useState<StateFilter>('all');
  const [selectedBackend, setSelectedBackend] = useState<VirtualScreeningStructureBackend>('boltz');
  const [viewerColorMode, setViewerColorMode] = useState<'alphafold' | 'default'>('alphafold');
  const [sortKey, setSortKey] = useState<SortKey>('rank');
  const [sortDirection, setSortDirection] = useState<SortDirection>('asc');
  const [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState('1');
  const [selectedHitIdentity, setSelectedHitIdentity] = useState('');
  const [viewerLoadingKey, setViewerLoadingKey] = useState('');
  const [localError, setLocalError] = useState('');
  const [viewerWidth, setViewerWidth] = useState(VIEWER_WIDTH_DEFAULT);
  const [isViewerResizing, setIsViewerResizing] = useState(false);
  const viewerLayoutRef = useRef<HTMLDivElement | null>(null);
  const viewerResizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const {
    targetReady,
    ligandChain,
    runPrediction,
    ensureResult,
    recordForHit
  } = useVirtualScreeningPredictions({
    components,
    initialRecords: predictionRecords,
    onRecordsChange: onPredictionRecordsChange,
    onError: setLocalError
  });

  const normalizedQuery = query.trim().toLowerCase();
  const filteredRows = useMemo(() => {
    const next = rows.filter((row) => {
      if (normalizedQuery && !`${row.name} ${row.id} ${row.smiles}`.toLowerCase().includes(normalizedQuery)) {
        return false;
      }
      if (stateFilter === 'all') return true;
      const state = displayState(recordForHit(row, selectedBackend)).toLowerCase();
      return state === stateFilter;
    });
    const valueFor = (row: ScreeningRow): string | number | null => {
      if (sortKey === 'name') return row.name.toLowerCase();
      if (sortKey === 'rank') return row.rank;
      return row[sortKey === 'ic50' ? 'ic50Um' : sortKey];
    };
    return next.sort((left, right) => {
      const leftValue = valueFor(left);
      const rightValue = valueFor(right);
      if (leftValue === null || rightValue === null) {
        if (leftValue === rightValue) return left.rank - right.rank;
        return leftValue === null ? 1 : -1;
      }
      const comparison = typeof leftValue === 'string'
        ? leftValue.localeCompare(String(rightValue))
        : Number(leftValue) - Number(rightValue);
      if (comparison !== 0) return sortDirection === 'asc' ? comparison : -comparison;
      return left.rank - right.rank;
    });
  }, [normalizedQuery, recordForHit, rows, selectedBackend, sortDirection, sortKey, stateFilter]);

  const totalPages = Math.max(1, Math.ceil(filteredRows.length / PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages);
  const pageRows = filteredRows.slice((clampedPage - 1) * PAGE_SIZE, clampedPage * PAGE_SIZE);

  // Render-time state adjustment instead of effects — no extra render pass and
  // no post-paint wrong-page frame: clamp the page when the filtered set
  // shrinks, and keep the go-to-page input mirroring the effective page when
  // it changes from elsewhere (prev/next buttons, filter changes).
  // https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const [prevTotalPages, setPrevTotalPages] = useState(totalPages);
  if (totalPages !== prevTotalPages) {
    setPrevTotalPages(totalPages);
    if (page > totalPages) setPage(totalPages);
  }
  const [prevClampedPage, setPrevClampedPage] = useState(clampedPage);
  if (clampedPage !== prevClampedPage) {
    setPrevClampedPage(clampedPage);
    setPageInput(String(clampedPage));
  }

  useEffect(() => {
    if (!isViewerResizing) return;
    const handleMove = (event: PointerEvent) => {
      const state = viewerResizeStateRef.current;
      const container = viewerLayoutRef.current;
      if (!state || !container) return;
      const min = VIEWER_WIDTH_MIN;
      const max = Math.max(min, container.clientWidth - RESULTS_WIDTH_MIN - RESIZER_WIDTH - 12);
      setViewerWidth(Math.max(min, Math.min(max, state.startWidth + event.clientX - state.startX)));
    };
    const handleUp = () => {
      viewerResizeStateRef.current = null;
      setIsViewerResizing(false);
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
  }, [isViewerResizing]);

  const successRows = useMemo(
    () => rows
      .filter((row) => recordForHit(row, selectedBackend)?.state === 'SUCCESS')
      .sort((left, right) => left.rank - right.rank),
    [recordForHit, rows, selectedBackend]
  );
  const selectedRow = useMemo(
    () => rows.find((row) => rowIdentity(row) === selectedHitIdentity) || null,
    [rows, selectedHitIdentity]
  );
  const selectedRecord = selectedRow ? recordForHit(selectedRow, selectedBackend) : null;
  const viewerOpen = Boolean(selectedRow && selectedRecord?.state === 'SUCCESS');
  const selectedLoadKey = selectedRow ? `${selectedBackend}:${rowIdentity(selectedRow)}` : '';
  const viewerLayoutStyle = {
    '--lead-opt-result-left-width': `${viewerWidth}px`,
    '--lead-opt-result-main-min-width': `${RESULTS_WIDTH_MIN}px`
  } as CSSProperties;

  const handleViewerResizeStart = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if (typeof window !== 'undefined' && window.matchMedia('(max-width: 1100px)').matches) return;
    if (!viewerLayoutRef.current) return;
    viewerResizeStateRef.current = { startX: event.clientX, startWidth: viewerWidth };
    setIsViewerResizing(true);
    event.preventDefault();
  };

  const handleViewerResizerKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    const container = viewerLayoutRef.current;
    if (!container) return;
    const min = VIEWER_WIDTH_MIN;
    const max = Math.max(min, container.clientWidth - RESULTS_WIDTH_MIN - RESIZER_WIDTH - 12);
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setViewerWidth((current) => Math.max(min, current - VIEWER_WIDTH_STEP));
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      setViewerWidth((current) => Math.min(max, current + VIEWER_WIDTH_STEP));
    } else if (event.key === 'Home') {
      event.preventDefault();
      setViewerWidth(VIEWER_WIDTH_DEFAULT);
    }
  };

  const setSort = (nextKey: SortKey) => {
    setPage(1);
    if (nextKey === sortKey) {
      setSortDirection((previous) => previous === 'asc' ? 'desc' : 'asc');
      return;
    }
    setSortKey(nextKey);
    setSortDirection(nextKey === 'probability' || nextKey === 'pic50' ? 'desc' : 'asc');
  };

  const sortLabel = (key: SortKey, label: string) => (
    <button
      type="button"
      className={`virtual-screening-sort ${sortKey === key ? 'is-active' : ''}`}
      onClick={() => setSort(key)}
      aria-label={`Sort by ${label}`}
    >
      {label}
      {sortKey === key
        ? sortDirection === 'asc'
          ? <ArrowUp size={12} aria-hidden="true" />
          : <ArrowDown size={12} aria-hidden="true" />
        : null}
    </button>
  );

  const downloadCsv = () => {
    const header = [
      'Rank',
      'Name',
      'ID',
      'SMILES',
      'IC50_uM',
      'pIC50',
      'Binding_probability',
      'Ensemble_spread',
      'Entropy_PL'
    ];
    const body = rows.map((row) => [
      row.rank,
      row.name,
      row.id,
      row.smiles,
      row.ic50Um,
      row.pic50,
      row.probability,
      row.spread,
      row.entropy
    ].map(csvCell).join(','));
    const blob = new Blob(
      [[header.map(csvCell).join(','), ...body].join('\n') + '\n'],
      { type: 'text/csv;charset=utf-8' }
    );
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `nesso-screening-${projectTaskId || 'results'}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  };

  const openStructure = async (row: ScreeningRow) => {
    const backend = selectedBackend;
    const record = recordForHit(row, backend);
    if (record?.state !== 'SUCCESS') return;
    const identity = rowIdentity(row);
    const loadKey = `${backend}:${identity}`;
    setSelectedHitIdentity(identity);
    setLocalError('');
    if (record.structureText?.trim()) return;
    setViewerLoadingKey(loadKey);
    try {
      await ensureResult({ id: row.id, smiles: row.smiles }, backend);
    } finally {
      setViewerLoadingKey((current) => current === loadKey ? '' : current);
    }
  };

  const handleOpenKeyDown = (event: ReactKeyboardEvent<HTMLElement>, row: ScreeningRow) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    void openStructure(row);
  };

  const runStructurePrediction = (row: ScreeningRow) => {
    setLocalError('');
    void runPrediction({ id: row.id, smiles: row.smiles }, selectedBackend);
  };

  const renderMetricProfile = (record: VirtualScreeningPredictionRecord | null) => {
    const interfaceValue = record?.interfaceMetricValue ?? record?.pairIptm ?? null;
    const interfaceLabel = record?.interfaceMetricLabel || 'IPSAE';
    return (
      <div className="lead-opt-info-col lead-opt-info-col--profile virtual-screening-model-profile">
        <div className={`lead-opt-confidence-row ${metricTone(record?.ligandPlddt ?? null)}`}>
          <span className="lead-opt-confidence-label">pLDDT</span>
          <strong className="lead-opt-confidence-value">{formatNumber(record?.ligandPlddt ?? null, 1)}</strong>
        </div>
        <div className={`lead-opt-confidence-row ${metricTone(interfaceValue)}`}>
          <span className="lead-opt-confidence-label">{interfaceLabel}</span>
          <strong className="lead-opt-confidence-value">{formatNumber(interfaceValue, 3)}</strong>
        </div>
        <div className={`lead-opt-confidence-row ${metricTone(record?.pairPae ?? null, 'pae')}`}>
          <span className="lead-opt-confidence-label">PAE</span>
          <strong className="lead-opt-confidence-value">{formatNumber(record?.pairPae ?? null, 2)}</strong>
        </div>
      </div>
    );
  };

  const renderAffinityProfile = (row: ScreeningRow) => (
    <div className="lead-opt-info-col virtual-screening-affinity-profile">
      <div className="lead-opt-insight-item">
        <span className="lead-opt-insight-label">IC50</span>
        <strong className="lead-opt-insight-value">{formatIc50(row.ic50Um)}</strong>
      </div>
      <div className="lead-opt-insight-item">
        <span className="lead-opt-insight-label">pIC50</span>
        <strong className="lead-opt-insight-value">{formatNumber(row.pic50, 2)}</strong>
      </div>
      <div className="lead-opt-insight-item">
        <span className="lead-opt-insight-label">Bind</span>
        <strong className="lead-opt-insight-value">{formatProbability(row.probability)}</strong>
      </div>
    </div>
  );

  if (viewerOpen && selectedRow && selectedRecord) {
    const selectedStructureReady = Boolean(selectedRecord.structureText?.trim());
    const selectedLoading = viewerLoadingKey === selectedLoadKey && !selectedStructureReady;
    return (
      <div
        ref={viewerLayoutRef}
        className="lead-opt-mmp-layout lead-opt-mmp-layout--viewer-open lead-opt-mmp-layout--resizable"
        style={viewerLayoutStyle}
      >
        <section className="lead-opt-mmp-viewer">
          {selectedStructureReady ? (
            <>
              <MolstarViewer
                key={`${selectedRecord.taskId}:${selectedRecord.structureName || selectedRow.id}`}
                structureText={selectedRecord.structureText || ''}
                format={selectedRecord.structureFormat || 'cif'}
                colorMode={viewerColorMode}
                confidenceBackend={selectedBackend}
                scenePreset="lead_opt"
                leadOptStyleVariant="results"
                ligandFocusChainId={ligandChain}
                interactionGranularity="element"
                suppressAutoFocus={false}
                showSequence={false}
              />
              <div className="vs-viewer-color-mode-overlay">
                <div className="prediction-render-mode-switch" role="tablist" aria-label="3D color mode">
                  <button
                    type="button"
                    role="tab"
                    aria-selected={viewerColorMode === 'alphafold'}
                    className={`prediction-render-mode-btn ${viewerColorMode === 'alphafold' ? 'active' : ''}`}
                    onClick={() => setViewerColorMode('alphafold')}
                    title="Color structure by model confidence"
                  >
                    AF
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={viewerColorMode === 'default'}
                    className={`prediction-render-mode-btn ${viewerColorMode === 'default' ? 'active' : ''}`}
                    onClick={() => setViewerColorMode('default')}
                    title="Use standard element colors"
                  >
                    Std
                  </button>
                </div>
              </div>
            </>
          ) : (
            <div className="virtual-screening-viewer-loading" role="status">
              {selectedLoading ? <Loader2 size={22} className="spinning" /> : <AlertTriangle size={22} />}
              <strong>{selectedLoading ? 'Loading prediction archive…' : 'Structure could not be loaded.'}</strong>
              {selectedRecord.error ? <span>{selectedRecord.error}</span> : null}
            </div>
          )}
        </section>

        <div
          className={`panel-resizer lead-opt-layout-resizer ${isViewerResizing ? 'dragging' : ''}`}
          onPointerDown={handleViewerResizeStart}
          onKeyDown={handleViewerResizerKeyDown}
          role="separator"
          aria-label="Resize 3D and screening panels"
          aria-orientation="vertical"
          tabIndex={0}
        />

        <div className="lead-opt-mmp-main lead-opt-mmp-main--viewer-open">
          <section className="lead-opt-candidates-panel lead-opt-candidates-panel--card-mode virtual-screening-results-panel">
            <div className="lead-opt-panel-head">
              <div className="lead-opt-query-toolbar lead-opt-query-toolbar--single-row">
                <button
                  type="button"
                  className="lead-opt-row-action-btn lead-opt-card-exit-btn"
                  onClick={() => {
                    setSelectedHitIdentity('');
                    setViewerLoadingKey('');
                  }}
                  aria-label="Exit cards"
                  title="Exit cards"
                >
                  <X size={14} />
                </button>
                <span className="lead-opt-query-toolbar-spacer" />
                <div className="lead-opt-query-toolbar-right">
                  <select
                    className="lead-opt-backend-select lead-opt-backend-select--engine"
                    value={selectedBackend}
                    onChange={(event) => {
                      setSelectedBackend(event.target.value as VirtualScreeningStructureBackend);
                      setSelectedHitIdentity('');
                      setViewerLoadingKey('');
                    }}
                    aria-label="Structure prediction backend"
                  >
                    {BACKEND_OPTIONS.map((option) => (
                      <option key={option.key} value={option.key}>{option.label}</option>
                    ))}
                  </select>
                </div>
              </div>
            </div>

            {localError ? (
              <div className="alert error virtual-screening-result-error" role="alert">
                {localError}
                <button type="button" className="btn btn-ghost btn-compact" onClick={() => setLocalError('')}>Dismiss</button>
              </div>
            ) : null}

            <div className="lead-opt-card-list virtual-screening-card-list">
              {successRows.map((row) => {
                const record = recordForHit(row, selectedBackend);
                if (!record) return null;
                const identity = rowIdentity(row);
                const isSelected = identity === selectedHitIdentity;
                return (
                  <article
                    key={identity}
                    className={`lead-opt-result-card is-clickable${isSelected ? ' selected' : ''}`}
                    onClick={() => void openStructure(row)}
                    onKeyDown={(event) => handleOpenKeyDown(event, row)}
                    role="button"
                    tabIndex={0}
                    aria-label={`Open 3D result for ${row.name}`}
                  >
                    <div className="lead-opt-result-card-head">
                      <strong>#{row.rank}</strong>
                      <span className="virtual-screening-card-name" title={row.name}>{row.name}</span>
                      {viewerLoadingKey === `${selectedBackend}:${identity}`
                        ? <Loader2 size={13} className="spinning" />
                        : null}
                    </div>
                    <div className="lead-opt-result-card-media">
                      <div className="lead-opt-structure-hit has-prediction">
                        <MemoLigand2DPreview
                          smiles={record.ligandRenderSmiles || row.smiles}
                          width={CARD_PREVIEW_WIDTH}
                          height={CARD_PREVIEW_HEIGHT}
                          atomConfidences={record.ligandRenderAtomPlddts || null}
                          confidenceHint={record.ligandPlddt}
                        />
                      </div>
                    </div>
                    <div className="lead-opt-card-metric-strip">
                      <span className="lead-opt-card-pill">
                        <span className="lead-opt-card-pill-key">pIC50</span>
                        <strong>{formatNumber(row.pic50, 2)}</strong>
                      </span>
                      <span className="lead-opt-card-pill">
                        <span className="lead-opt-card-pill-key">pLDDT</span>
                        <strong>{formatNumber(record.ligandPlddt, 1)}</strong>
                      </span>
                      <span className="lead-opt-card-pill">
                        <span className="lead-opt-card-pill-key">{record.interfaceMetricLabel || 'IPSAE'}</span>
                        <strong>{formatNumber(record.interfaceMetricValue ?? record.pairIptm, 3)}</strong>
                      </span>
                      <span className="lead-opt-card-pill">
                        <span className="lead-opt-card-pill-key">PAE</span>
                        <strong>{formatNumber(record.pairPae, 2)}</strong>
                      </span>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>
        </div>
      </div>
    );
  }

  return (
    <section className="lead-opt-candidates-panel virtual-screening-results-panel">
      <div className="lead-opt-panel-head">
        <div className="lead-opt-query-toolbar lead-opt-query-toolbar--single-row">
          <select
            className="lead-opt-backend-select lead-opt-state-filter-select"
            value={stateFilter}
            onChange={(event) => {
              setStateFilter(event.target.value as StateFilter);
              setPage(1);
            }}
            aria-label="Filter structure prediction state"
          >
            <option value="all">All states</option>
            <option value="unscored">Not run</option>
            <option value="queued">Queued</option>
            <option value="running">Running</option>
            <option value="success">Success</option>
            <option value="failure">Failure</option>
          </select>
          <label className="virtual-screening-search">
            <Search size={14} />
            <input
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setPage(1);
              }}
              placeholder="Search name, ID, or SMILES"
              aria-label="Search screening results"
            />
          </label>
          <span className="lead-opt-query-toolbar-spacer" />
          <div className="lead-opt-query-toolbar-right">
            <span className="muted small">{filteredRows.length.toLocaleString()} hits</span>
            <select
              className="lead-opt-backend-select lead-opt-backend-select--engine"
              value={selectedBackend}
              onChange={(event) => {
                setSelectedBackend(event.target.value as VirtualScreeningStructureBackend);
                setSelectedHitIdentity('');
                setViewerLoadingKey('');
                setPage(1);
              }}
              aria-label="Structure prediction backend"
            >
              {BACKEND_OPTIONS.map((option) => (
                <option key={option.key} value={option.key}>{option.label}</option>
              ))}
            </select>
            <button
              type="button"
              className="lead-opt-row-action-btn"
              title="Export screening results as CSV"
              aria-label="Export screening results as CSV"
              onClick={downloadCsv}
              disabled={rows.length === 0}
            >
              <Download size={14} />
            </button>
          </div>
        </div>
      </div>

      {localError ? (
        <div className="alert error virtual-screening-result-error" role="alert">
          {localError}
          <button type="button" className="btn btn-ghost btn-compact" onClick={() => setLocalError('')}>Dismiss</button>
        </div>
      ) : null}

      {rows.length > 0 ? (
        <>
          <div className="lead-opt-result-table-wrap">
            <table className="lead-opt-candidate-table lead-opt-result-table virtual-screening-result-table">
              <thead>
                <tr>
                  <th className="col-rank">{sortLabel('rank', 'Rank')}</th>
                  <th className="col-structure">2D / 3D</th>
                  <th className="virtual-screening-col-compound">{sortLabel('name', 'Compound / SMILES')}</th>
                  <th className="col-insights virtual-screening-col-affinity">{sortLabel('pic50', 'Affinity profile')}</th>
                  <th className="col-insights col-insights-model">Model profile</th>
                  <th className="col-state">State</th>
                  <th className="col-actions">Run</th>
                </tr>
              </thead>
              <tbody>
                {pageRows.map((row) => {
                  const record = recordForHit(row, selectedBackend);
                  const state = displayState(record);
                  const pending = state === 'QUEUED' || state === 'RUNNING';
                  const openable = state === 'SUCCESS';
                  const runDisabled = pending || !canPredictStructures || !targetReady || !row.smiles;
                  const actionTitle = pending
                    ? stateLabel(state)
                    : !canPredictStructures
                      ? 'Read-only project'
                      : !targetReady
                        ? 'Add a target sequence or structure first'
                        : 'Run structure prediction';
                  return (
                    <tr key={`${rowIdentity(row)}:${row.rank}`}>
                      <td className="col-rank">{row.rank}</td>
                      <td className="col-structure">
                        <button
                          type="button"
                          className={`lead-opt-structure-hit${openable ? ' has-prediction' : ''}`}
                          onClick={() => {
                            if (openable) void openStructure(row);
                          }}
                          disabled={!openable}
                          title={openable ? 'Open 3D model' : 'Run prediction to open a 3D model'}
                        >
                          <MemoLigand2DPreview
                            smiles={record?.ligandRenderSmiles || row.smiles}
                            width={TABLE_PREVIEW_WIDTH}
                            height={TABLE_PREVIEW_HEIGHT}
                            atomConfidences={record?.state === 'SUCCESS' ? record.ligandRenderAtomPlddts || null : null}
                            confidenceHint={record?.state === 'SUCCESS' ? record.ligandPlddt : null}
                          />
                        </button>
                      </td>
                      <td className="virtual-screening-col-compound">
                        <strong title={row.name}>{row.name}</strong>
                        {row.id !== row.name ? <small title={row.id}>{row.id}</small> : null}
                        <code title={row.smiles}>{row.smiles || '-'}</code>
                      </td>
                      <td className="col-insights virtual-screening-col-affinity">{renderAffinityProfile(row)}</td>
                      <td className="col-insights col-insights-model">{renderMetricProfile(record)}</td>
                      <td className="col-state" title={record?.error || undefined}>
                        <span className={`lead-opt-state-pill ${stateTone(state)}`}>
                          <StateIcon state={state} />
                          {stateLabel(state)}
                        </span>
                      </td>
                      <td className="col-actions" onClick={(event) => event.stopPropagation()}>
                        <button
                          type="button"
                          className={[
                            'lead-opt-row-action-btn',
                            !pending ? 'lead-opt-row-action-btn-primary' : '',
                            state === 'RUNNING' ? 'lead-opt-row-action-btn--running' : '',
                            state === 'QUEUED' ? 'lead-opt-row-action-btn--queued' : ''
                          ].filter(Boolean).join(' ')}
                          onClick={pending ? undefined : () => runStructurePrediction(row)}
                          disabled={runDisabled}
                          title={actionTitle}
                          aria-label={actionTitle}
                        >
                          {state === 'RUNNING'
                            ? <Loader2 size={14} className="spinning" />
                            : state === 'QUEUED'
                              ? <Clock3 size={14} />
                              : <Play size={14} />}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {filteredRows.length > 0 ? (
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
                onClick={() => setPage((previous) => Math.max(1, previous - 1))}
                disabled={clampedPage <= 1}
                aria-label="Previous page"
                title="Previous page"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                type="button"
                className="lead-opt-row-action-btn"
                onClick={() => setPage((previous) => Math.min(totalPages, previous + 1))}
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
                  aria-label="Go to screening result page"
                />
              </label>
            </div>
          ) : (
            <p className="muted small">No hits match the current filters.</p>
          )}
        </>
      ) : (
        <p className="muted small">
          {projectTaskState === 'SUCCESS'
            ? 'No ranked compounds were found in this result archive.'
            : `Task is ${projectTaskState || 'not started'}${progressPercent > 0 ? ` (${Math.round(progressPercent)}%)` : ''}.`}
        </p>
      )}
    </section>
  );
}
