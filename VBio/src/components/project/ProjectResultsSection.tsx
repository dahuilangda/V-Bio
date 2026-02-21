import type { KeyboardEvent, PointerEvent, ReactNode, RefObject } from 'react';
import { AffinityResultsWorkspace } from './AffinityWorkspace';
import type { AffinitySignalCard, ResultsGridStyle } from './AffinityWorkspace';
import { LigandPropertyGrid } from './LigandPropertyGrid';
import { MetricsPanel } from './MetricsPanel';
import { MolstarViewer } from './MolstarViewer';

export interface ProjectResultsSectionProps {
  isPredictionWorkflow: boolean;
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
  resultChainIds: string[];
  selectedResultTargetChainId: string | null;
  selectedResultLigandChainId: string | null;
  displayStructureText: string;
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
}

export function ProjectResultsSection({
  isPredictionWorkflow,
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
  resultChainIds,
  selectedResultTargetChainId,
  selectedResultLigandChainId,
  displayStructureText,
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
  affinityLigandConfidenceHint
}: ProjectResultsSectionProps) {
  if (isPredictionWorkflow) {
    return (
      <>
        <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
          <section className="panel structure-panel">
            <h2>Structure Viewer</h2>

            <MolstarViewer
              structureText={displayStructureText}
              format={displayStructureFormat}
              colorMode={displayStructureColorMode}
              confidenceBackend={confidenceBackend || projectBackend}
            />

            <span className="muted small">Current structure file: {displayStructureName}</span>
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

          <aside className="panel info-panel">
            <h2>Overview</h2>

            <section className="result-aside-block result-aside-block-ligand">
              <div className="result-aside-title">Ligand</div>
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
}
