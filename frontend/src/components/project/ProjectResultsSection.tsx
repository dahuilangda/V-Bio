import { memo, useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent as ReactMouseEvent, type PointerEvent, type ReactNode, type RefObject } from 'react';
import { useTabsKeyboard } from '../ui/useTabsKeyboard';
import { OverviewLigandSequencePreview } from '../../pages/projectDetail/OverviewLigandSequencePreview';
import { AffinityResultsWorkspace } from './AffinityWorkspace';
import type { AffinitySignalCard, ResultsGridStyle } from './AffinityWorkspace';
import { LigandPropertyGrid } from './LigandPropertyGrid';
import { MetricsPanel } from './MetricsPanel';
import { MolstarViewer } from './MolstarViewer';
import { PeptideDesignResultsWorkspace } from './PeptideDesignResultsWorkspace';
import { VirtualScreeningResultsSection } from './VirtualScreeningResultsSection';
import type {
  InputComponent,
  VirtualScreeningPredictionRecord
} from '../../types/models';

export interface ProjectResultsSectionProps {
  isPredictionWorkflow: boolean;
  isPeptideDesignWorkflow: boolean;
  isVirtualScreeningWorkflow: boolean;
  isAffinityWorkflow: boolean;
  workflowTitle: string;
  workflowShortTitle: string;
  projectTaskState: string;
  projectTaskId: string;
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: ResultsGridStyle;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  snapshotCards: AffinitySignalCard[];
  snapshotConfidence: Record<string, unknown>;
  snapshotAffinity: Record<string, unknown>;
  resultChainIds: string[];
  selectedResultTargetChainId: string | null;
  selectedResultLigandChainId: string | null;
  selectedLigandComponentType?: 'protein' | 'dna' | 'rna' | 'ligand' | null;
  displayStructureText: string;
  displayStructureConfidenceText: string;
  displayStructureFormat: 'cif' | 'pdb';
  displayStructureColorMode: 'default' | 'alphafold';
  displayStructureName: string;
  confidenceBackend: string;
  projectBackend: string;
  predictionLigandPreview: ReactNode;
  predictionLigandRadarSmiles: string;
  hasAffinityDisplayStructure: boolean;
  affinityDisplayStructureText: string;
  affinityDisplayStructureFormat: 'cif' | 'pdb';
  affinityLigandSmiles: string;
  affinityPrimaryTargetChainId: string | null;
  affinityLigandAtomPlddts: number[];
  affinityLigandConfidenceHint: number | null;
  selectedResultLigandSequence: string;
  designedBinderComponent: InputComponent | null;
  designedBinderChainId: string | null;
  designedBinderResiduePlddts: number[] | null;
  peptideFallbackPlddt: number | null;
  peptideFallbackIptm: number | null;
  statusInfo: Record<string, unknown> | null;
  progressPercent: number;
  isStructurePredictionAllowed: boolean;
  virtualScreeningComponents: InputComponent[];
  predictionRecords: Record<string, VirtualScreeningPredictionRecord>;
  onPredictionRecordsChange?: (records: Record<string, VirtualScreeningPredictionRecord>) => void;
  requestStructureAction?: (options?: { preferredStructureName?: string }) => Promise<void> | void;
}


const VIRIDIS_STOPS: Array<[number, number, number]> = [
  [68, 1, 84],
  [59, 82, 139],
  [33, 145, 140],
  [94, 201, 98],
  [253, 231, 37]
];

function viridis(t: number): [number, number, number] {
  const x = Math.min(1, Math.max(0, t)) * (VIRIDIS_STOPS.length - 1);
  const i = Math.min(VIRIDIS_STOPS.length - 2, Math.floor(x));
  const f = x - i;
  const a = VIRIDIS_STOPS[i];
  const b = VIRIDIS_STOPS[i + 1];
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f)
  ];
}

// AlphaFold-DB-style ramp for PAE: light = confident (blends with the card),
// deep blue = high error. Keeps a saturated high-error linker band readable
// instead of glaring.
const PAE_STOPS: Array<[number, number, number]> = [
  [247, 249, 251],
  [198, 219, 239],
  [107, 174, 214],
  [43, 109, 161],
  [12, 48, 97]
];

function paeColor(t: number): [number, number, number] {
  const x = Math.min(1, Math.max(0, t)) * (PAE_STOPS.length - 1);
  const i = Math.min(PAE_STOPS.length - 2, Math.floor(x));
  const f = x - i;
  const a = PAE_STOPS[i];
  const b = PAE_STOPS[i + 1];
  return [
    Math.round(a[0] + (b[0] - a[0]) * f),
    Math.round(a[1] + (b[1] - a[1]) * f),
    Math.round(a[2] + (b[2] - a[2]) * f)
  ];
}

const PAE_CSS = `linear-gradient(90deg, ${PAE_STOPS.map(
  ([r, g, b], i) => `rgb(${r},${g},${b}) ${(i / (PAE_STOPS.length - 1)) * 100}%`
).join(', ')})`;

const PAE_TRACK_HUES = ['#dfe9e2', '#dce6f4', '#f2e7da', '#eadff0'];

function chainLetter(index: number): string {
  return String.fromCharCode(65 + index);
}

interface PaeBounds {
  chain: number;
  start: number;
  end: number;
}

function observeOnce(el: HTMLElement, paint: () => void): () => void {
  if (typeof IntersectionObserver === 'undefined') {
    paint();
    return () => {};
  }
  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting)) {
      const idle = (window as Window & { requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number }).requestIdleCallback;
      if (idle) idle(paint, { timeout: 500 });
      else requestAnimationFrame(paint);
      observer.disconnect();
    }
  }, { rootMargin: '120px' });
  observer.observe(el);
  return () => observer.disconnect();
}

function PaeMatrixSection({ snapshotConfidence, taskId }: { snapshotConfidence: Record<string, unknown> | null; taskId: string }) {
  const conf = (snapshotConfidence || {}) as { pae_matrix?: number[][]; pae_chain_bounds?: PaeBounds[] };
  // Memoized so a parent re-render cannot hand the paint effect a fresh
  // matrix identity (which would tear down and re-arm the observer).
  const cachedEntry = useMemo(() => {
    // 自带矩阵时跳过缓存读取
    if (Array.isArray(conf.pae_matrix) && conf.pae_matrix.length > 0) return null;
    try {
      const cached = JSON.parse(localStorage.getItem('vbio:pae:latest') || 'null');
      // Only the task that captured this cache may render it.
      const cachedTaskId = String(cached?.task_id || '');
      const activeTaskId = String(taskId || '');
      if (
        cached &&
        Array.isArray(cached.pae_matrix) &&
        cachedTaskId &&
        cachedTaskId === activeTaskId
      ) {
        return { matrix: cached.pae_matrix as number[][], bounds: cached.pae_chain_bounds as PaeBounds[] | undefined };
      }
    } catch {
      // ignore
    }
    return null;
  }, [taskId, conf.pae_matrix]);
  let matrix = conf.pae_matrix;
  let bounds = conf.pae_chain_bounds;
  if (!Array.isArray(matrix) || matrix.length === 0) {
    if (cachedEntry) {
      matrix = cachedEntry.matrix;
      bounds = cachedEntry.bounds;
    }
  }
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  // bounds are in original-token space; the matrix is downsampled to n rows.
  // Everything that indexes the matrix (boundary lines, track, hover) works
  // in matrix space via this rescale.
  const n = Array.isArray(matrix) ? matrix.length : 0;
  const scaledBounds = useMemo<PaeBounds[] | null>(() => {
    if (!Array.isArray(bounds) || bounds.length === 0 || !n) return null;
    const tokenTotal = Math.max(...bounds.map((b) => b.end)) + 1;
    return bounds.map((b) => ({
      chain: b.chain,
      start: Math.round((b.start / tokenTotal) * n),
      end: Math.min(n - 1, Math.round((b.end / tokenTotal) * n))
    }));
  }, [bounds, n]);
  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    let painted = false;
    const paint = () => {
      if (painted) return;
      painted = true;
      const canvas = canvasRef.current;
      if (!canvas || !Array.isArray(matrix) || matrix.length === 0) return;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      const size = 600;
      const trackH = 40;
      canvas.width = size;
      canvas.height = size + trackH;
      const n = matrix.length;
      const image = ctx.createImageData(size, size);
      const px = image.data;
      const scale = size / n;
      for (let y = 0; y < size; y += 1) {
        const i = Math.min(n - 1, Math.floor(y / scale));
        const row = matrix[i] || [];
        for (let x = 0; x < size; x += 1) {
          const j = Math.min(n - 1, Math.floor(x / scale));
          const [r, g, b] = paeColor((row[j] ?? 25) / 25);
          const o = (y * size + x) * 4;
          px[o] = r;
          px[o + 1] = g;
          px[o + 2] = b;
          px[o + 3] = 255;
        }
      }
      ctx.putImageData(image, 0, 0);
      if (scaledBounds && scaledBounds.length > 1) {
        ctx.strokeStyle = 'rgba(70, 90, 80, 0.55)';
        ctx.lineWidth = 1.5;
        scaledBounds.slice(0, -1).forEach((b) => {
          const pos = (b.end / n) * size;
          ctx.beginPath();
          ctx.moveTo(pos, 0);
          ctx.lineTo(pos, size);
          ctx.moveTo(0, pos);
          ctx.lineTo(size, pos);
          ctx.stroke();
        });
      }
      // Chain track under the matrix: hue per chain, letter when wide enough.
      const trackY = size + 6;
      ctx.fillStyle = '#eef3ef';
      ctx.fillRect(0, trackY, size, trackH - 6);
      ctx.font = '600 22px sans-serif';
      scaledBounds?.forEach((b, idx) => {
        const x0 = (b.start / n) * size;
        const w = Math.max(1, ((b.end - b.start + 1) / n) * size);
        ctx.fillStyle = PAE_TRACK_HUES[idx % PAE_TRACK_HUES.length];
        ctx.fillRect(x0, trackY, w - 2, trackH - 6);
        if (w >= 26) {
          ctx.fillStyle = '#3d5a4c';
          ctx.fillText(chainLetter(b.chain), x0 + w / 2 - 8, trackY + 28);
        }
      });
    };
    return observeOnce(wrap, paint);
  }, [matrix, bounds]);
  if (!Array.isArray(matrix) || matrix.length === 0) return null;
  const cellTooltips = (event: ReactMouseEvent<HTMLDivElement>) => {
    const wrap = wrapRef.current;
    if (!wrap || !Array.isArray(matrix)) return;
    const canvas = canvasRef.current;
    if (!canvas || !wrap) return;
    const rect = canvas.getBoundingClientRect();
    const n = matrix.length;
    // The canvas is matrix (n rows) + chain track; cut at the track boundary.
    const matrixFrac = 600 / 640;
    const fracY = (event.clientY - rect.top) / rect.height;
    const fracX = (event.clientX - rect.left) / rect.width;
    if (fracY >= matrixFrac || fracY < 0 || fracX < 0 || fracX > 1) {
      wrap.removeAttribute('title');
      return;
    }
    const i = Math.min(n - 1, Math.max(0, Math.floor((fracY / matrixFrac) * n)));
    const j = Math.min(n - 1, Math.max(0, Math.floor(fracX * n)));
    const value = matrix[i]?.[j];
    const chainAt = (idx: number) => {
      const b = scaledBounds ? scaledBounds.find((entry) => idx >= entry.start && idx <= entry.end) : null;
      return b ? chainLetter(b.chain) : '';
    };
    wrap.title = `${chainAt(i)}→${chainAt(j)} PAE ${Number(value ?? 0).toFixed(1)}Å`;
  };
  return (
    <div className="pae-matrix-chart" ref={wrapRef} onMouseMove={cellTooltips}>
      <canvas ref={canvasRef} aria-label="PAE matrix heatmap" />
      <div className="pae-matrix-legend">
        <span>0Å</span>
        <span className="pae-matrix-legend-bar" style={{ background: PAE_CSS }} />
        <span>25Å</span>
      </div>
    </div>
  );
}

function MsaCoverageSection({ snapshotConfidence }: { snapshotConfidence: Record<string, unknown> | null }) {
  const conf = (snapshotConfidence || {}) as { msa?: { depth?: number; chains?: Array<{ chain: string; length: number; depth: number; coverage: number[] }> } };
  const chains = useMemo(() => Array.isArray(conf.msa?.chains) ? conf.msa!.chains!.filter(
    (c) => c && Array.isArray(c.coverage) && c.coverage.length > 0
  ) : [], [conf.msa]);
  const sectionRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const section = sectionRef.current;
    if (!section || chains.length === 0) return;
    let painted = false;
    const paint = () => {
      if (painted) return;
      painted = true;
      section.querySelectorAll<HTMLCanvasElement>('canvas[data-chain]').forEach((canvas) => {
        const entry = chains.find((c) => `msa-cov-${c.chain}` === canvas.dataset.chain);
        if (!entry) return;
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        const width = canvas.width;
        const height = canvas.height;
        ctx.clearRect(0, 0, width, height);
        ctx.fillStyle = '#e3efe8';
        ctx.fillRect(0, 0, width, height);
        const cols = entry.coverage.length;
        const barW = width / cols;
        entry.coverage.forEach((value, col) => {
          const h = (Math.min(100, Math.max(0, value)) / 100) * height;
          const [r, g, b] = viridis(0.25 + 0.6 * (Math.min(100, Math.max(0, value)) / 100));
          ctx.fillStyle = `rgb(${r},${g},${b})`;
          ctx.fillRect(col * barW, height - h, barW + 0.5, h);
        });
      });
    };
    return observeOnce(section, paint);
  }, [chains]);
  if (chains.length === 0) return null;
  return (
    <div className="msa-coverage-chart" ref={sectionRef}>
      <div className="msa-coverage-title">
        MSA coverage
        <span className="muted small">{`total ${Math.round(Number(conf.msa?.depth) || chains.reduce((acc, c) => acc + Math.round(c.depth), 0))} seq`}</span>
      </div>
      {chains.map((c) => (
        <div key={c.chain} className="msa-coverage-row">
          <span className="msa-coverage-label">{`${c.chain || '?'} · ${Math.round(c.depth)} seq`}</span>
          <canvas data-chain={`msa-cov-${c.chain}`} width={600} height={44} aria-label={`MSA coverage chain ${c.chain}`} />
        </div>
      ))}
    </div>
  );
}


export const ProjectResultsSection = memo(function ProjectResultsSection({
  isPredictionWorkflow,
  isPeptideDesignWorkflow,
  isVirtualScreeningWorkflow,
  isAffinityWorkflow,
  workflowTitle,
  workflowShortTitle,
  projectTaskState,
  projectTaskId,
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onResizerPointerDown,
  onResizerKeyDown,
  snapshotCards,
  snapshotConfidence,
  snapshotAffinity,
  resultChainIds,
  selectedResultTargetChainId,
  selectedResultLigandChainId,
  selectedLigandComponentType,
  displayStructureText,
  displayStructureConfidenceText,
  displayStructureFormat,
  displayStructureColorMode,
  displayStructureName,
  confidenceBackend,
  projectBackend,
  predictionLigandPreview,
  predictionLigandRadarSmiles,
  hasAffinityDisplayStructure,
  affinityDisplayStructureText,
  affinityDisplayStructureFormat,
  affinityLigandSmiles,
  affinityPrimaryTargetChainId,
  affinityLigandAtomPlddts,
  affinityLigandConfidenceHint,
  selectedResultLigandSequence,
  designedBinderComponent,
  designedBinderChainId: _designedBinderChainId,
  designedBinderResiduePlddts,
  peptideFallbackPlddt,
  peptideFallbackIptm,
  statusInfo,
  progressPercent,
  isStructurePredictionAllowed,
  virtualScreeningComponents,
  predictionRecords,
  onPredictionRecordsChange,
  requestStructureAction
}: ProjectResultsSectionProps) {
  const initialPredictionColorMode = useMemo<'default' | 'alphafold'>(
    () => (displayStructureColorMode === 'alphafold' ? 'alphafold' : 'default'),
    [displayStructureColorMode]
  );
  const [predictionViewerColorMode, setPredictionViewerColorMode] = useState<'default' | 'alphafold'>(
    initialPredictionColorMode
  );

  // Render-time adjustment (no effect pass): the viewer reverts to the task's
  // default color mode whenever the opened task or its default changes.
  const [prevPredictionColorModeKey, setPrevPredictionColorModeKey] = useState({
    initialPredictionColorMode,
    projectTaskId
  });
  if (initialPredictionColorMode !== prevPredictionColorModeKey.initialPredictionColorMode || projectTaskId !== prevPredictionColorModeKey.projectTaskId) {
    setPrevPredictionColorModeKey({ initialPredictionColorMode, projectTaskId });
    setPredictionViewerColorMode(initialPredictionColorMode);
  }

  // APG tabs keyboard behaviour for the 3D color mode switch.
  const colorModeTabs = useTabsKeyboard<'default' | 'alphafold'>(
    predictionViewerColorMode,
    setPredictionViewerColorMode,
    ['alphafold', 'default']
  );

  const predictionViewerStructureText = useMemo(
    () => displayStructureConfidenceText,
    [displayStructureConfidenceText]
  );
  const effectivePredictionBackend = String(confidenceBackend || projectBackend).trim().toLowerCase();
  const isAffinityOnlyPrediction =
    effectivePredictionBackend === 'nesso' ||
    snapshotConfidence.structure_available === false;

  if (isVirtualScreeningWorkflow) {
    return (
      <VirtualScreeningResultsSection
        screening={snapshotAffinity || {}}
        projectTaskId={projectTaskId}
        projectTaskState={projectTaskState}
        progressPercent={progressPercent}
        isStructurePredictionAllowed={isStructurePredictionAllowed}
        components={virtualScreeningComponents}
        predictionRecords={predictionRecords}
        onPredictionRecordsChange={onPredictionRecordsChange}
      />
    );
  }

  if (isPeptideDesignWorkflow) {
    return (
      <PeptideDesignResultsWorkspace
        projectTaskId={projectTaskId}
        resultsGridRef={resultsGridRef}
        isResultsResizing={isResultsResizing}
        resultsGridStyle={resultsGridStyle}
        onResizerPointerDown={onResizerPointerDown}
        onResizerKeyDown={onResizerKeyDown}
        snapshotConfidence={snapshotConfidence || {}}
        displayStructureText={displayStructureText}
        displayStructureFormat={displayStructureFormat}
        displayStructureName={displayStructureName}
        selectedResultTargetChainId={selectedResultTargetChainId}
        selectedResultLigandChainId={selectedResultLigandChainId}
        selectedResultLigandSequence={selectedResultLigandSequence}
        confidenceBackend={confidenceBackend}
        projectBackend={projectBackend}
        fallbackPlddt={peptideFallbackPlddt}
        fallbackIptm={peptideFallbackIptm}
        statusInfo={statusInfo || {}}
        projectTaskState={projectTaskState}
        progressPercent={progressPercent}
        onRequestStructure={requestStructureAction}
      />
    );
  }

  if (isPredictionWorkflow) {
    return (
      <>
        <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
          <section className="structure-panel structure-panel--prediction">
            <MolstarViewer
              key={`prediction-results-viewer:${projectTaskId || '-'}:${selectedResultLigandChainId || '-'}`}
              structureText={predictionViewerStructureText}
              format={displayStructureFormat}
              colorMode={predictionViewerColorMode}
              confidenceBackend={confidenceBackend || projectBackend}
              scenePreset="lead_opt"
              leadOptStyleVariant="results"
              ligandFocusChainId={selectedResultLigandChainId || ''}
              interactionGranularity="element"
              isAutoFocusSuppressed={false}
              isSequenceVisible={false}
              emptyMessage={
                isAffinityOnlyPrediction
                  ? 'Nesso-1 produced affinity signals without a 3D structure.'
                  : undefined
              }
            />
          </section>

          <div
            className={`results-resizer ${isResultsResizing ? 'dragging' : ''}`}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize structure and overview panels"
            tabIndex={0}
            onPointerDown={onResizerPointerDown}
            onKeyDown={onResizerKeyDown}
          />

          <aside className="info-panel">
            <section className="result-aside-block result-aside-block-ligand">
              <div className="result-aside-head">
                <div className="result-aside-title">Ligand</div>
                {!isAffinityOnlyPrediction && (
                  <div
                    className="prediction-render-mode-switch"
                    role="tablist"
                    aria-label="3D color mode"
                    {...colorModeTabs.props}
                  >
                  <button
                    type="button"
                    role="tab"
                    aria-selected={predictionViewerColorMode === 'alphafold'}
                    tabIndex={colorModeTabs.tabTabIndex(predictionViewerColorMode === 'alphafold')}
                    className={`prediction-render-mode-btn ${
                      predictionViewerColorMode === 'alphafold' ? 'active' : ''
                    }`}
                    onClick={() => setPredictionViewerColorMode('alphafold')}
                    title="Color structure by model confidence"
                  >
                    AF
                  </button>
                  <button
                    type="button"
                    role="tab"
                    aria-selected={predictionViewerColorMode === 'default'}
                    tabIndex={colorModeTabs.tabTabIndex(predictionViewerColorMode === 'default')}
                    className={`prediction-render-mode-btn ${
                      predictionViewerColorMode === 'default' ? 'active' : ''
                    }`}
                    onClick={() => setPredictionViewerColorMode('default')}
                    title="Use standard element colors"
                  >
                    Std
                  </button>
                  </div>
                )}
              </div>
              <div className="ligand-preview-panel">
                {designedBinderComponent && designedBinderComponent.sequence && !selectedResultLigandSequence ? (
                  <OverviewLigandSequencePreview
                    sequence={designedBinderComponent.sequence}
                    residuePlddts={designedBinderResiduePlddts}
                    modifications={designedBinderComponent.modifications}
                  />
                ) : predictionLigandPreview}
              </div>
              {predictionLigandRadarSmiles ? <LigandPropertyGrid smiles={predictionLigandRadarSmiles} variant="radar" /> : null}
            </section>

            <section className="result-aside-block">
              <div className="result-aside-title">Model Signals</div>
              <div className="overview-signal-list">
                {snapshotCards.map((card) => (
                  <div key={card.key} className={`overview-signal-row tone-${card.tone}`}>
                    <div className="overview-signal-main">
                      <span className="overview-signal-label">{card.label}</span>
                      <span className="overview-signal-detail">{card.detail ?? ''}</span>
                    </div>
                    <strong className={`overview-signal-value metric-value-${card.tone}`}>{card.value}</strong>
                  </div>
                ))}
              </div>
              <PaeMatrixSection snapshotConfidence={snapshotConfidence} taskId={projectTaskId} />
              <MsaCoverageSection snapshotConfidence={snapshotConfidence} />
            </section>
          </aside>
        </div>

        <div className="results-bottom">
          <MetricsPanel
            title="Confidence"
            data={snapshotConfidence || {}}
            chainIds={resultChainIds}
        selectedTargetChainId={selectedResultTargetChainId}
        selectedLigandChainId={selectedResultLigandChainId}
        selectedLigandComponentType={selectedLigandComponentType || null}
      />
        </div>
      </>
    );
  }

  if (isAffinityWorkflow) {
    return (
      <AffinityResultsWorkspace
        hasStructure={hasAffinityDisplayStructure}
        snapshotAffinity={snapshotAffinity || null}
        structureText={affinityDisplayStructureText}
        structureFormat={affinityDisplayStructureFormat}
        colorMode={displayStructureColorMode}
        confidenceBackend={confidenceBackend}
        projectBackend={projectBackend}
        ligandSmiles={affinityLigandSmiles}
        ligandAtomPlddts={affinityLigandAtomPlddts}
        ligandConfidenceHint={affinityLigandConfidenceHint}
        snapshotCards={snapshotCards}
        snapshotConfidence={snapshotConfidence || {}}
        resultChainIds={resultChainIds}
        selectedTargetChainId={selectedResultTargetChainId || affinityPrimaryTargetChainId}
        selectedLigandChainId={selectedResultLigandChainId || null}
        selectedLigandComponentType={selectedLigandComponentType || null}
        resultsGridRef={resultsGridRef}
        isResultsResizing={isResultsResizing}
        resultsGridStyle={resultsGridStyle}
        onResizerPointerDown={onResizerPointerDown}
        onResizerKeyDown={onResizerKeyDown}
      />
    );
  }

  return (
    <section className="panel">
      <h2>{workflowTitle}</h2>
      <p className="muted">
        This project is set to <strong>{workflowShortTitle}</strong>. Configure workflow-specific parameters in Basics.
      </p>
      <div className="status-stats">
        <div className="status-stat">
          <span className="muted small">Workflow</span>
          <strong>{workflowTitle}</strong>
        </div>
        <div className="status-stat">
          <span className="muted small">Current State</span>
          <strong>{projectTaskState}</strong>
        </div>
        <div className="status-stat">
          <span className="muted small">Task ID</span>
          <strong>{projectTaskId || '-'}</strong>
        </div>
      </div>
    </section>
  );
});
