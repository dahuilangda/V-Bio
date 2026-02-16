import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { CircleCheck, Dna, Eye, FlaskConical, Target } from 'lucide-react';
import { MolstarViewer } from './MolstarViewer';
import { JSMEEditor } from './JSMEEditor';
import { Ligand2DPreview } from './Ligand2DPreview';
import { LigandPropertyGrid } from './LigandPropertyGrid';
import { MetricsPanel } from './MetricsPanel';

export type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';
export type ResultsGridStyle = CSSProperties & { '--results-main-width'?: string };

export interface AffinitySignalCard {
  key: string;
  label: string;
  value: string;
  detail: string;
  tone: MetricTone;
}

interface AffinityBasicsWorkspaceProps {
  canEdit: boolean;
  submitting: boolean;
  targetFileName: string;
  ligandFileName: string;
  ligandSmiles: string;
  ligandEditorInput: string;
  confidenceOnly: boolean;
  confidenceOnlyLocked: boolean;
  confidenceOnlyHint: string;
  previewTargetStructureText: string;
  previewTargetStructureFormat: 'cif' | 'pdb';
  previewLigandStructureText: string;
  previewLigandStructureFormat: 'cif' | 'pdb';
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: ResultsGridStyle;
  onTargetFileChange: (file: File | null) => void;
  onLigandFileChange: (file: File | null) => void;
  onConfidenceOnlyChange: (checked: boolean) => void;
  onLigandSmilesChange: (smiles: string) => void;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

export function AffinityBasicsWorkspace({
  canEdit,
  submitting,
  targetFileName,
  ligandFileName,
  ligandSmiles,
  ligandEditorInput,
  confidenceOnly,
  confidenceOnlyLocked,
  confidenceOnlyHint,
  previewTargetStructureText,
  previewTargetStructureFormat,
  previewLigandStructureText,
  previewLigandStructureFormat,
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onTargetFileChange,
  onLigandFileChange,
  onConfidenceOnlyChange,
  onLigandSmilesChange,
  onResizerPointerDown,
  onResizerKeyDown
}: AffinityBasicsWorkspaceProps) {
  return (
    <section className="panel subtle affinity-basics-panel">
      <div className="affinity-basics-controls">
        <label className="field">
          <span className="affinity-field-title">
            <Dna size={13} />
            Target <span className="required-mark">*</span>
            {targetFileName ? <CircleCheck size={13} className="affinity-upload-ok" /> : null}
          </span>
          <input
            type="file"
            accept=".pdb,.ent,.cif,.mmcif"
            disabled={!canEdit || submitting}
            onClick={(event) => {
              (event.currentTarget as HTMLInputElement).value = '';
            }}
            onChange={(event) => onTargetFileChange(event.target.files?.[0] || null)}
          />
        </label>

        <label className="field">
          <span className="affinity-field-title">
            <FlaskConical size={13} />
            Ligand
            {ligandFileName ? <CircleCheck size={13} className="affinity-upload-ok" /> : null}
          </span>
          <input
            type="file"
            accept=".sdf,.sd,.mol2,.mol,.pdb,.ent,.cif,.mmcif"
            disabled={!canEdit || submitting}
            onClick={(event) => {
              (event.currentTarget as HTMLInputElement).value = '';
            }}
            onChange={(event) => onLigandFileChange(event.target.files?.[0] || null)}
          />
        </label>

        <label className="switch-field affinity-confidence-toggle">
          <input
            type="checkbox"
            checked={confidenceOnly}
            disabled={!canEdit || submitting || confidenceOnlyLocked}
            onChange={(event) => onConfidenceOnlyChange(event.target.checked)}
          />
          <span className="affinity-field-title">
            <Eye size={13} />
            Confidence Only
          </span>
          {confidenceOnlyHint.trim() ? <span className="muted small affinity-confidence-hint">{confidenceOnlyHint}</span> : null}
        </label>

      </div>

      <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
        <section className="panel structure-panel">
          <h2 className="affinity-section-title">
            <Eye size={15} />
            Preview
          </h2>
          {previewTargetStructureText ? (
            <MolstarViewer
              structureText={previewTargetStructureText}
              format={previewTargetStructureFormat}
              overlayStructureText={previewLigandStructureText}
              overlayFormat={previewLigandStructureFormat}
              colorMode="white"
            />
          ) : (
            <div className="ligand-preview-empty">Upload target file.</div>
          )}
        </section>

        <div
          className={`results-resizer ${isResultsResizing ? 'dragging' : ''}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize preview and ligand panels"
          tabIndex={0}
          onPointerDown={onResizerPointerDown}
          onKeyDown={onResizerKeyDown}
        />

        <aside className="panel info-panel">
          <h2 className="affinity-section-title">
            <FlaskConical size={15} />
            Ligand
          </h2>
          <section className="result-aside-block result-aside-block-ligand">
            <div className="result-aside-title affinity-title-with-icon">
              <Dna size={13} />
              2D (JSME)
            </div>
            <div className="jsme-editor-container affinity-jsme-shell">
              <JSMEEditor smiles={ligandEditorInput} onSmilesChange={onLigandSmilesChange} height={336} />
            </div>
          </section>
          <section className="result-aside-block">
            <div className="result-aside-title affinity-title-with-icon">
              <Target size={13} />
              Radar
            </div>
            <LigandPropertyGrid smiles={ligandSmiles} variant="radar" />
          </section>
        </aside>
      </div>
    </section>
  );
}

interface AffinityResultsWorkspaceProps {
  hasStructure: boolean;
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  colorMode: 'white' | 'alphafold';
  confidenceBackend: string;
  projectBackend: string;
  ligandSmiles: string;
  ligandAtomPlddts: number[];
  ligandConfidenceHint: number | null;
  snapshotCards: AffinitySignalCard[];
  snapshotConfidence: Record<string, unknown>;
  resultChainIds: string[];
  selectedTargetChainId: string | null;
  selectedLigandChainId: string | null;
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: ResultsGridStyle;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

export function AffinityResultsWorkspace({
  hasStructure,
  structureText,
  structureFormat,
  colorMode,
  confidenceBackend,
  projectBackend,
  ligandSmiles,
  ligandAtomPlddts,
  ligandConfidenceHint,
  snapshotCards,
  snapshotConfidence,
  resultChainIds,
  selectedTargetChainId,
  selectedLigandChainId,
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onResizerPointerDown,
  onResizerKeyDown
}: AffinityResultsWorkspaceProps) {
  return (
    <>
      <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
        <section className="panel structure-panel">
          <h2 className="affinity-section-title">
            <Eye size={15} />
            Complex
          </h2>
          {hasStructure ? (
            <MolstarViewer
              structureText={structureText}
              format={structureFormat}
              colorMode={colorMode}
              confidenceBackend={confidenceBackend || projectBackend}
            />
          ) : (
            <div className="ligand-preview-empty">Upload target file in Basics.</div>
          )}
        </section>

        <div
          className={`results-resizer ${isResultsResizing ? 'dragging' : ''}`}
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize structure and ligand panels"
          tabIndex={0}
          onPointerDown={onResizerPointerDown}
          onKeyDown={onResizerKeyDown}
        />

        <aside className="panel info-panel">
          <h2 className="affinity-section-title">
            <FlaskConical size={15} />
            Ligand
          </h2>
          <section className="result-aside-block result-aside-block-ligand">
            <div className="ligand-preview-panel">
              <Ligand2DPreview
                smiles={ligandSmiles}
                atomConfidences={ligandAtomPlddts}
                confidenceHint={ligandConfidenceHint}
              />
            </div>
          </section>

          <section className="result-aside-block">
            <div className="result-aside-title affinity-title-with-icon">
              <Target size={13} />
              Radar
            </div>
            {ligandSmiles.trim() ? (
              <LigandPropertyGrid smiles={ligandSmiles} variant="radar" />
            ) : (
              <div className="ligand-preview-empty">No ligand SMILES available.</div>
            )}
          </section>

          <section className="result-aside-block">
            <div className="result-aside-title affinity-title-with-icon">
              <Eye size={13} />
              Signals
            </div>
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
          selectedTargetChainId={selectedTargetChainId}
          selectedLigandChainId={selectedLigandChainId}
        />
      </div>
    </>
  );
}
