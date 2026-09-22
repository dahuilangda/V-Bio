import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ensureStructureConfidenceColoringData, stripStructureConfidenceColoringData } from '../../api/backendApi';
import { MolstarViewer } from './MolstarViewer';
import { asString } from '../../pages/projectTasks/recordReaders';
import {
  EMPTY_RECORD_ROWS,
  PEPTIDE_RESULTS_PAGE_SIZE_OPTIONS,
  buildCandidateRows,
  buildRawCandidateRowsSignature,
  extractRawCandidates,
  extractRuntimeContext,
  normalizeModelLabel,
  resolvePeptideFocusChainId,
  shouldUseLivePeptideRows,
  structureNameMatches,
  type PeptideDesignCandidate,
  type PeptideDesignResultsWorkspaceProps,
  type PeptideSortKey,
} from './peptideDesignResults/parseHelpers';
import { PeptideCandidateCard, PeptideCandidateTableRow } from './peptideDesignResults/PeptideCandidateViews';
export function PeptideDesignResultsWorkspace({
  projectTaskId,
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onResizerPointerDown,
  onResizerKeyDown,
  snapshotConfidence,
  statusInfo,
  projectTaskState,
  progressPercent,
  displayStructureText,
  displayStructureFormat,
  displayStructureName,
  selectedResultTargetChainId,
  selectedResultLigandChainId,
  selectedResultLigandSequence,
  confidenceBackend,
  projectBackend,
  fallbackPlddt,
  fallbackIptm,
  onRequestStructure
}: PeptideDesignResultsWorkspaceProps) {
  void selectedResultLigandSequence;
  void fallbackPlddt;
  void fallbackIptm;
  const runtimeContext = useMemo(
    () =>
      extractRuntimeContext({
        statusInfo: statusInfo || {},
        snapshotConfidence: snapshotConfidence || {},
        projectTaskState,
        fallbackProgressPercent: progressPercent
      }),
    [snapshotConfidence, statusInfo, projectTaskState, progressPercent]
  );
  const runtimeModelLabel = useMemo(
    () => normalizeModelLabel(confidenceBackend) || normalizeModelLabel(projectBackend) || 'Boltz',
    [confidenceBackend, projectBackend]
  );
  const finalizedCandidateRows = useMemo(
    () => extractRawCandidates(snapshotConfidence || {}),
    [snapshotConfidence]
  );
  const useLiveCandidateRows = shouldUseLivePeptideRows(runtimeContext.state);
  const liveCandidateRows = useLiveCandidateRows ? runtimeContext.liveCandidateRows : EMPTY_RECORD_ROWS;
  const liveCandidateRowsSignature = useMemo(
    () => buildRawCandidateRowsSignature(liveCandidateRows),
    [liveCandidateRows]
  );
  const liveDefaultState = runtimeContext.state === 'UNSCORED' ? 'RUNNING' : runtimeContext.state;

  const candidates = useMemo<PeptideDesignCandidate[]>(() => {
    void liveCandidateRowsSignature;
    return buildCandidateRows(
      finalizedCandidateRows,
      liveCandidateRows,
      liveDefaultState,
      runtimeModelLabel,
      selectedResultLigandChainId || undefined,
      selectedResultTargetChainId || undefined
    );
  }, [
    finalizedCandidateRows,
    liveCandidateRowsSignature,
    liveDefaultState,
    runtimeModelLabel,
    selectedResultLigandChainId,
    selectedResultTargetChainId
  ]);

  const [selectedCandidateId, setSelectedCandidateId] = useState('');
  const initialViewerColorMode: 'default' | 'alphafold' =
    confidenceBackend === 'alphafold3' ||
    confidenceBackend === 'protenix' ||
    projectBackend === 'alphafold3' ||
    projectBackend === 'protenix'
      ? 'alphafold'
      : 'default';
  const [viewerColorMode, setViewerColorMode] = useState<'default' | 'alphafold'>(initialViewerColorMode);
  const [cardMode, setCardMode] = useState(false);
  const [sortKey, setSortKey] = useState<PeptideSortKey>('score');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');
  const [page, setPage] = useState(1);
  const [pageInput, setPageInput] = useState('1');
  const [pageSize, setPageSize] = useState<(typeof PEPTIDE_RESULTS_PAGE_SIZE_OPTIONS)[number]>(20);
  const [requestingStructure, setRequestingStructure] = useState(false);
  const [structureRequestError, setStructureRequestError] = useState('');
  const requestedStructureKeyRef = useRef('');
  const hasAnyIpsae = useMemo(
    () => candidates.some((candidate) => candidate.interfaceMetricSource === 'ipsae' || candidate.ipsae !== null),
    [candidates]
  );
  const interfaceMetricHeaderLabel = useMemo(() => {
    const hasIptm = candidates.some((candidate) => candidate.interfaceMetricSource === 'iptm');
    if (hasAnyIpsae && hasIptm) return 'Interface';
    if (hasAnyIpsae) return 'IPSAE';
    return 'ipTM';
  }, [candidates, hasAnyIpsae]);

  const sortedCandidates = useMemo(() => {
    const sorted = [...candidates];
    const dir = sortDirection === 'asc' ? 1 : -1;
    const score = (value: number | null) => (value === null ? Number.NEGATIVE_INFINITY : value);

    sorted.sort((a, b) => {
      let diff = 0;
      if (sortKey === 'rank') diff = a.rank - b.rank;
      if (sortKey === 'generation') diff = score(a.generation) - score(b.generation);
      if (sortKey === 'score') diff = score(a.score) - score(b.score);
      if (sortKey === 'plddt') diff = score(a.plddt) - score(b.plddt);
      if (sortKey === 'interface') diff = score(a.interfaceMetric) - score(b.interfaceMetric);

      if (diff !== 0) return diff * dir;
      return a.rank - b.rank;
    });
    return sorted;
  }, [candidates, sortDirection, sortKey]);

  const scoreRange = useMemo(() => {
    const values = sortedCandidates
      .map((candidate) => candidate.score)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    if (values.length === 0) {
      return { min: null as number | null, max: null as number | null };
    }
    return { min: Math.min(...values), max: Math.max(...values) };
  }, [sortedCandidates]);

  const totalPages = useMemo(
    () => Math.max(1, Math.ceil(sortedCandidates.length / pageSize)),
    [pageSize, sortedCandidates.length]
  );
  const clampedPage = Math.max(1, Math.min(totalPages, page));
  const pagedCandidates = useMemo(
    () => sortedCandidates.slice((clampedPage - 1) * pageSize, clampedPage * pageSize),
    [sortedCandidates, clampedPage, pageSize]
  );
  const cardCandidates = pagedCandidates;

  // Render-time adjustments (not effects): reset the viewer color mode on task
  // change, keep the selection valid, clamp the page when the list shrinks.
  const [prevInitialViewerColorMode, setPrevInitialViewerColorMode] = useState(initialViewerColorMode);
  if (initialViewerColorMode !== prevInitialViewerColorMode) {
    setPrevInitialViewerColorMode(initialViewerColorMode);
    setViewerColorMode(initialViewerColorMode);
  }

  // The selection must point at an existing candidate, or be empty only when the list is.
  if (!sortedCandidates.length) {
    if (selectedCandidateId !== '') setSelectedCandidateId('');
  } else if (!sortedCandidates.some((item) => item.id === selectedCandidateId)) {
    setSelectedCandidateId(sortedCandidates[0].id);
  }

  const [prevClampedPage, setPrevClampedPage] = useState(clampedPage);
  if (clampedPage !== prevClampedPage) {
    setPrevClampedPage(clampedPage);
    if (page !== clampedPage) {
      setPage(clampedPage);
    }
  }

  useEffect(() => {
    setPageInput(String(clampedPage));
  }, [clampedPage]);

  const selectedCandidate = useMemo(() => {
    if (!sortedCandidates.length) return null;
    return sortedCandidates.find((item) => item.id === selectedCandidateId) || sortedCandidates[0];
  }, [sortedCandidates, selectedCandidateId]);
  const selectedCandidateStableId = selectedCandidate?.id || '';

  useEffect(() => {
    if (sortedCandidates.length > 0) return;
    setCardMode(false);
  }, [sortedCandidates.length]);

  useEffect(() => {
    setCardMode(false);
    setSelectedCandidateId('');
    setPage(1);
    setPageInput('1');
    requestedStructureKeyRef.current = '';
    setRequestingStructure(false);
    setStructureRequestError('');
  }, [projectTaskId]);

  const hasCandidateRows = sortedCandidates.length > 0;
  const selectedStructureName = asString(selectedCandidate?.structureName).trim();
  const loadedStructureMatchesSelected = structureNameMatches(displayStructureName, selectedStructureName);
  const selectedCandidateStructureText = asString(selectedCandidate?.structureText).trim();
  const viewerRawStructureText = cardMode
    ? selectedCandidateStructureText || (loadedStructureMatchesSelected ? displayStructureText : '')
    : '';
  const viewerStructureFormat = cardMode && selectedCandidateStructureText ? selectedCandidate?.structureFormat || 'cif' : displayStructureFormat;
  const hasViewerRawStructureText = viewerRawStructureText.trim().length > 0;
  const viewerStandardStructureText = useMemo(
    () => (cardMode && hasViewerRawStructureText ? stripStructureConfidenceColoringData(viewerRawStructureText, viewerStructureFormat) : ''),
    [cardMode, hasViewerRawStructureText, viewerRawStructureText, viewerStructureFormat]
  );
  const viewerConfidenceStructureText = useMemo(
    () =>
      cardMode && hasViewerRawStructureText
        ? ensureStructureConfidenceColoringData(viewerRawStructureText, viewerStructureFormat, confidenceBackend || projectBackend)
        : '',
    [cardMode, hasViewerRawStructureText, viewerRawStructureText, viewerStructureFormat, confidenceBackend, projectBackend]
  );
  // Load the structure ONCE after the user opens the 3D card. The _ma_qa_metric confidence blocks
  // are harmless for the element-symbol Std theme, so AF<->Std toggles don't reload the structure.
  const viewerStructureText = cardMode ? viewerConfidenceStructureText || viewerStandardStructureText : '';
  const canRequestStructure = runtimeContext.state === 'SUCCESS' && Boolean(onRequestStructure);
  const canRequestSelectedStructure = canRequestStructure && Boolean(selectedStructureName);
  const viewerLigandFocusChainId = useMemo(() => {
    if (!cardMode) return selectedResultLigandChainId || '';
    const preferredChain = selectedResultLigandChainId || undefined;
    const candidateSequence = asString(selectedCandidate?.sequence || '').trim().toUpperCase();
    const structureTextForFocus = asString(viewerStructureText).trim();
    if (!structureTextForFocus) return selectedResultLigandChainId || '';
    const focusChain = resolvePeptideFocusChainId(
      structureTextForFocus,
      viewerStructureFormat,
      candidateSequence,
      preferredChain
    );
    return focusChain || selectedResultLigandChainId || '';
  }, [cardMode, selectedCandidate?.sequence, selectedResultLigandChainId, viewerStructureFormat, viewerStructureText]);

  useEffect(() => {
    if (!cardMode) return;
    if (!canRequestSelectedStructure) return;
    if (!hasCandidateRows) return;
    if (asString(viewerStructureText).trim()) return;
    const preferredStructureName = selectedStructureName;
    if (!preferredStructureName) return;
    const requestKey = `${projectTaskId}:${selectedCandidate?.id || 'none'}:${preferredStructureName || '-'}`;
    if (requestedStructureKeyRef.current === requestKey) return;
    requestedStructureKeyRef.current = requestKey;
    setStructureRequestError('');
    setRequestingStructure(true);
    Promise.resolve(onRequestStructure?.({ preferredStructureName }))
      .catch((error) => {
        setStructureRequestError(error instanceof Error ? error.message : 'Failed to load the requested peptide structure.');
      })
      .finally(() => setRequestingStructure(false));
  }, [canRequestSelectedStructure, cardMode, hasCandidateRows, onRequestStructure, projectTaskId, selectedCandidate?.id, selectedStructureName, viewerStructureText]);

  const openCandidateCard = useCallback((candidateId: string) => {
    setSelectedCandidateId(candidateId);
    setCardMode(true);
  }, []);

  const selectCandidateCard = useCallback((candidateId: string) => {
    setSelectedCandidateId(candidateId);
  }, []);

  const onSort = (key: PeptideSortKey) => {
    if (sortKey === key) {
      setSortDirection((prev) => (prev === 'asc' ? 'desc' : 'asc'));
      return;
    }
    setSortKey(key);
    setSortDirection(key === 'rank' ? 'asc' : 'desc');
  };

  const sortMark = (key: PeptideSortKey) => {
    if (sortKey !== key) return '';
    return sortDirection === 'asc' ? ' \u2191' : ' \u2193';
  };

  const renderViewerModeSwitch = () => (
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
  );

  const renderCandidateTable = (standalone = false) => (
    <section className={`peptide-result-list-panel${standalone ? ' peptide-result-list-panel--standalone' : ''}`}>
      <div className="lead-opt-result-table-wrap peptide-result-table-wrap">
        <table className="lead-opt-candidate-table lead-opt-result-table peptide-result-table">
          <thead>
            <tr>
              <th className="col-rank">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('rank')}>
                  #{sortMark('rank')}
                </button>
              </th>
              <th className="col-actions peptide-col-open">2D</th>
              <th className="col-n">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('generation')}>
                  Gen{sortMark('generation')}
                </button>
              </th>
              <th className="col-delta">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('score')}>
                  Score{sortMark('score')}
                </button>
              </th>
              <th className="col-insights peptide-col-metric">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('plddt')}>
                  pLDDT{sortMark('plddt')}
                </button>
              </th>
              <th className="col-insights peptide-col-metric">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('interface')}>
                  {interfaceMetricHeaderLabel}{sortMark('interface')}
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            {pagedCandidates.map((candidate) => (
              <PeptideCandidateTableRow
                key={candidate.id}
                candidate={candidate}
                isSelected={candidate.id === selectedCandidateStableId}
                isCardMode={cardMode}
                scoreMin={scoreRange.min}
                scoreMax={scoreRange.max}
                onOpen={openCandidateCard}
                onSelect={selectCandidateCard}
              />
            ))}
            {sortedCandidates.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <div className="ligand-preview-empty">No designed peptide records yet.</div>
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      {sortedCandidates.length > 0 && totalPages > 1 ? (
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
              aria-label="Go to peptide result page"
            />
          </label>
          <label className="project-page-size">
            <span className="muted small">Rows</span>
            <select
              value={String(pageSize)}
              onChange={(event) => {
                const parsed = Number(event.target.value);
                if (!Number.isFinite(parsed)) return;
                const next = PEPTIDE_RESULTS_PAGE_SIZE_OPTIONS.find((value) => value === parsed);
                if (!next) return;
                setPageSize(next);
                setPage(1);
              }}
              aria-label="Rows per page"
            >
              {PEPTIDE_RESULTS_PAGE_SIZE_OPTIONS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
    </section>
  );

  const renderCandidateCards = () => (
    <section className="peptide-result-card-panel">
      <div className="lead-opt-query-toolbar lead-opt-query-toolbar--single-row peptide-result-toolbar peptide-result-toolbar--card">
        <div className="peptide-result-toolbar-left">
          <button
            type="button"
            className="lead-opt-row-action-btn lead-opt-card-exit-btn"
            onClick={() => setCardMode(false)}
            title="Exit cards"
            aria-label="Exit cards"
          >
            <X size={14} />
          </button>
        </div>
        <span className="lead-opt-query-toolbar-spacer" />
        <div className="lead-opt-query-toolbar-right">
          {renderViewerModeSwitch()}
        </div>
      </div>
      {sortedCandidates.length === 0 ? (
        <section className="result-aside-block peptide-selected-card">
          <div className="ligand-preview-empty">No designed peptide cards yet.</div>
        </section>
      ) : (
        <div className="peptide-card-list-wrap">
          <div className="lead-opt-card-list peptide-card-list">
            {cardCandidates.map((candidate) => (
              <PeptideCandidateCard
                key={candidate.id}
                candidate={candidate}
                isSelected={candidate.id === selectedCandidateStableId}
                scoreMin={scoreRange.min}
                scoreMax={scoreRange.max}
                isIpsaeVisible={hasAnyIpsae}
                onSelect={selectCandidateCard}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  );

  if (!cardMode) {
    return renderCandidateTable(true);
  }

  return (
    <div
      ref={resultsGridRef}
      className={`results-grid peptide-results-grid--card ${isResultsResizing ? 'is-resizing' : ''}`}
      style={resultsGridStyle}
    >
      <section className="structure-panel structure-panel--results-compact peptide-results-structure-panel">
        {viewerStructureText.trim() ? (
          <MolstarViewer
            key={`peptide-results-viewer:${selectedCandidate?.id || 'none'}:${viewerStructureFormat}`}
            structureText={viewerStructureText}
            format={viewerStructureFormat}
            colorMode={viewerColorMode}
            confidenceBackend={viewerColorMode === 'alphafold' ? confidenceBackend || projectBackend : ''}
            scenePreset="lead_opt"
            leadOptStyleVariant="results"
            ligandFocusChainId={viewerLigandFocusChainId || ''}
            interactionGranularity="element"
            isAutoFocusSuppressed={false}
            isSequenceVisible={false}
          />
        ) : (
          <div className={structureRequestError ? 'alert error' : 'ligand-preview-empty'}>
            <span>{structureRequestError || (requestingStructure ? 'Loading structure...' : canRequestSelectedStructure ? 'Preparing structure...' : 'No structure is available for this peptide.')}</span>
          </div>
        )}
      </section>

      <div
        className={`results-resizer ${isResultsResizing ? 'dragging' : ''}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize structure and peptide result panels"
        tabIndex={0}
        onPointerDown={onResizerPointerDown}
        onKeyDown={onResizerKeyDown}
      />

      <aside className="info-panel">{renderCandidateCards()}</aside>
    </div>
  );
}
