import { memo, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent, type ReactNode, type RefObject } from 'react';
import { useTabsKeyboard } from '../ui/useTabsKeyboard';
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


function PaeMatrixSection({ snapshotConfidence }: { snapshotConfidence: Record<string, unknown> | null }) {
  const conf = (snapshotConfidence || {}) as { pae_matrix?: number[][]; pae_chain_bounds?: Array<{ chain: number; start: number; end: number }> };
  let matrix = conf.pae_matrix;
  let bounds = conf.pae_chain_bounds;
  if (!Array.isArray(matrix) || matrix.length === 0) {
    try {
      const cached = JSON.parse(localStorage.getItem('vbio:pae:latest') || 'null');
      if (cached && Array.isArray(cached.pae_matrix)) {
        matrix = cached.pae_matrix;
        bounds = cached.pae_chain_bounds;
      }
    } catch {
      // ignore
    }
  }
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !Array.isArray(matrix) || matrix.length === 0) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const size = 300;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    canvas.style.width = `${size}px`;
    canvas.style.height = `${size}px`;
    ctx.scale(dpr, dpr);
    const n = matrix.length;
    const cell = size / n;
    for (let i = 0; i < n; i += 1) {
      for (let j = 0; j < n; j += 1) {
        const t = Math.min(1, Math.max(0, (matrix[i][j] ?? 25) / 25));
        ctx.fillStyle = `rgb(${Math.round(20 + 235 * t)},${Math.round(80 + 60 * Math.abs(t - 0.5))},${Math.round(240 - 210 * t)})`;
        ctx.fillRect(j * cell, i * cell, cell + 0.5, cell + 0.5);
      }
    }
    if (bounds && bounds.length > 1) {
      ctx.strokeStyle = 'rgba(255,255,255,0.9)';
      ctx.lineWidth = 1.5;
      let acc = 0;
      bounds.forEach((b, idx) => {
        acc += b.end - b.start + 1;
        if (idx < bounds.length - 1) {
          const pos = (acc / n) * size;
          ctx.beginPath();
          ctx.moveTo(pos, 0);
          ctx.lineTo(pos, size);
          ctx.moveTo(0, pos);
          ctx.lineTo(size, pos);
          ctx.stroke();
        }
      });
      ctx.fillStyle = 'rgba(255,255,255,0.95)';
      ctx.font = '11px sans-serif';
      let acc2 = 0;
      bounds.forEach((b) => {
        const mid = ((acc2 + (b.end - b.start) / 2) / n) * size;
        ctx.fillText(`链${b.chain}`, mid - 10, 12);
        acc2 += b.end - b.start + 1;
      });
    }
  }, [matrix, bounds]);
  if (!Array.isArray(matrix) || matrix.length === 0) return null;
  return (
    <div className="pae-matrix-chart">
      <canvas ref={canvasRef} aria-label="PAE matrix heatmap" />
      <div className="pae-matrix-legend">
        <span>0Å</span>
        <span className="pae-matrix-legend-bar" style={{ background: 'linear-gradient(90deg, rgb(20,80,240), rgb(120,140,140), rgb(255,140,30))' }} />
        <span>25Å</span>
      </div>
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
              <div className="ligand-preview-panel">{predictionLigandPreview}</div>
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
              <PaeMatrixSection snapshotConfidence={snapshotConfidence} />
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
