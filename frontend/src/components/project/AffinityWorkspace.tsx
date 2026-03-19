import { useEffect, useMemo, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';
import { CircleCheck, Dna, Eye, FlaskConical, Target } from 'lucide-react';
import { MolstarViewer, type MolstarAtomHighlight, type MolstarResiduePick } from './MolstarViewer';
import { JSMEEditor } from './JSMEEditor';
import { Ligand2DPreview } from './Ligand2DPreview';
import { LigandPropertyGrid } from './LigandPropertyGrid';
import { MetricsPanel } from './MetricsPanel';
import { resolveExactLigandAtomLinks } from './affinityAtomLinking';
import type { AffinityScoringMode } from '../../types/models';

export type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';
export type ResultsGridStyle = CSSProperties & { '--results-main-width'?: string };

function normalizeChainToken(value: string | null | undefined): string {
  return String(value || '').trim().toUpperCase();
}

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
  backend: string;
  mode: AffinityScoringMode;
  seed: number | null;
  targetFileName: string;
  ligandFileName: string;
  ligandSmiles: string;
  ligandEditorInput: string;
  useMsa: boolean;
  confidenceOnly: boolean;
  confidenceOnlyLocked: boolean;
  previewTargetStructureText: string;
  previewTargetStructureFormat: 'cif' | 'pdb';
  previewLigandStructureText: string;
  previewLigandStructureFormat: 'cif' | 'pdb';
  previewLigandChainId?: string;
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: ResultsGridStyle;
  onTargetFileChange: (file: File | null) => void;
  onLigandFileChange: (file: File | null) => void;
  onUseMsaChange: (checked: boolean) => void;
  onConfidenceOnlyChange: (checked: boolean) => void;
  onBackendChange: (backend: string) => void;
  onModeChange: (mode: AffinityScoringMode) => void;
  onSeedChange: (seed: number | null) => void;
  onLigandSmilesChange: (smiles: string) => void;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
}

export function AffinityBasicsWorkspace({
  canEdit,
  submitting,
  backend,
  mode,
  seed,
  targetFileName,
  ligandFileName,
  ligandSmiles,
  ligandEditorInput,
  useMsa,
  confidenceOnly,
  confidenceOnlyLocked,
  previewTargetStructureText,
  previewTargetStructureFormat,
  previewLigandStructureText,
  previewLigandStructureFormat,
  previewLigandChainId = '',
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onTargetFileChange,
  onLigandFileChange,
  onUseMsaChange,
  onConfidenceOnlyChange,
  onBackendChange,
  onModeChange,
  onSeedChange,
  onLigandSmilesChange,
  onResizerPointerDown,
  onResizerKeyDown
}: AffinityBasicsWorkspaceProps) {
  return (
    <section className="affinity-basics-panel">
      <div className="affinity-basics-controls">
        <div className="affinity-basics-upload-row">
          <label className="field affinity-upload-field">
            <span className="affinity-field-title">
              <Dna size={13} />
              Target <span className="required-mark">*</span>
              {targetFileName ? <CircleCheck size={13} className="affinity-upload-ok" /> : null}
            </span>
            <input
              type="file"
              className="file-input-unified"
              accept=".pdb,.ent,.cif,.mmcif"
              disabled={!canEdit || submitting}
              onClick={(event) => {
                (event.currentTarget as HTMLInputElement).value = '';
              }}
              onChange={(event) => onTargetFileChange(event.target.files?.[0] || null)}
            />
          </label>

          <label className="field affinity-upload-field">
            <span className="affinity-field-title">
              <FlaskConical size={13} />
              Ligand
              {ligandFileName ? <CircleCheck size={13} className="affinity-upload-ok" /> : null}
            </span>
            <input
              type="file"
              className="file-input-unified"
              accept=".sdf,.sd,.mol2,.mol,.pdb,.ent,.cif,.mmcif"
              disabled={!canEdit || submitting}
              onClick={(event) => {
                (event.currentTarget as HTMLInputElement).value = '';
              }}
              onChange={(event) => onLigandFileChange(event.target.files?.[0] || null)}
            />
          </label>

          <label className="field affinity-inline-field">
            <span className="affinity-field-title">Mode</span>
            <select
              value={mode}
              disabled={!canEdit || submitting}
              onChange={(event) => onModeChange(event.target.value as AffinityScoringMode)}
            >
              <option value="score">Score</option>
              <option value="pose">Pose</option>
              <option value="refine">Refine</option>
              <option value="interface">Interface</option>
            </select>
          </label>

          <label className="switch-field affinity-inline-toggle">
            <input
              type="checkbox"
              checked={useMsa}
              disabled={!canEdit || submitting}
              onChange={(event) => onUseMsaChange(event.target.checked)}
            />
            <span className="affinity-field-title">
              <Dna size={13} />
              Use MSA
            </span>
          </label>

          <label className="switch-field affinity-inline-toggle">
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
          </label>
        </div>
      </div>

      <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
        <section className="structure-panel">
          {previewTargetStructureText ? (
            <MolstarViewer
              structureText={previewTargetStructureText}
              format={previewTargetStructureFormat}
              overlayStructureText={previewLigandStructureText}
              overlayFormat={previewLigandStructureFormat}
              ligandFocusChainId={previewLigandChainId}
              autoFocusLigand
              colorMode="default"
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

        <aside className="info-panel">
          <section className="result-aside-block result-aside-block-ligand">
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

      <section className="panel subtle affinity-runtime-card">
        <div className="affinity-basics-settings-row">
          <label className="field affinity-inline-field">
            <span className="affinity-field-title">Backend</span>
            <select
              value={backend}
              disabled={!canEdit || submitting}
              onChange={(event) => onBackendChange(event.target.value)}
            >
              <option value="boltz">Boltz-2</option>
            </select>
          </label>

          <label className="field affinity-inline-field">
            <span>Seed (optional)</span>
            <input
              type="number"
              min={0}
              value={seed ?? ''}
              onChange={(event) => {
                const value = event.target.value;
                const nextSeed = value === '' ? null : Math.max(0, Math.floor(Number(value) || 0));
                onSeedChange(nextSeed);
              }}
              disabled={!canEdit || submitting}
              placeholder="Default: 42"
            />
          </label>
        </div>
      </section>
    </section>
  );
}

interface AffinityResultsWorkspaceProps {
  hasStructure: boolean;
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  colorMode: 'default' | 'alphafold';
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
  const initialViewerColorMode = useMemo<'default' | 'alphafold'>(
    () => (colorMode === 'alphafold' ? 'alphafold' : 'default'),
    [colorMode]
  );
  const [viewerColorMode, setViewerColorMode] = useState<'default' | 'alphafold'>(initialViewerColorMode);
  const exactLigandAtomLinks = useMemo(
    () =>
      resolveExactLigandAtomLinks({
        confidence: snapshotConfidence || null,
        renderedSmiles: ligandSmiles,
        structureText,
        structureFormat,
        selectedLigandChainId
      }),
    [ligandSmiles, selectedLigandChainId, snapshotConfidence, structureFormat, structureText]
  );
  const [selectedLigandAtomIndex, setSelectedLigandAtomIndex] = useState<number | null>(null);

  useEffect(() => {
    setViewerColorMode(initialViewerColorMode);
  }, [initialViewerColorMode, structureText]);

  useEffect(() => {
    if (!exactLigandAtomLinks) {
      setSelectedLigandAtomIndex(null);
      return;
    }
    if (
      selectedLigandAtomIndex === null ||
      selectedLigandAtomIndex < 0 ||
      selectedLigandAtomIndex >= exactLigandAtomLinks.atoms.length
    ) {
      setSelectedLigandAtomIndex(null);
    }
  }, [exactLigandAtomLinks, selectedLigandAtomIndex]);

  const activeLigandAtom = useMemo<MolstarAtomHighlight | null>(() => {
    if (!exactLigandAtomLinks) return null;
    if (selectedLigandAtomIndex === null) return null;
    const entry = exactLigandAtomLinks.atoms[selectedLigandAtomIndex];
    if (!entry) return null;
    return {
      chainId: entry.chainId,
      residue: entry.residue,
      atomName: entry.atomName,
      emphasis: 'active'
    };
  }, [exactLigandAtomLinks, selectedLigandAtomIndex]);

  const highlightedLigandAtoms = useMemo<MolstarAtomHighlight[]>(
    () => (activeLigandAtom ? [activeLigandAtom] : []),
    [activeLigandAtom]
  );

  const handleLigand2DAtomClick = (atomIndex: number) => {
    if (!exactLigandAtomLinks) return;
    if (!Number.isFinite(atomIndex) || atomIndex < 0 || atomIndex >= exactLigandAtomLinks.atoms.length) return;
    setSelectedLigandAtomIndex((current) => (current === atomIndex ? null : atomIndex));
  };

  const handleLigand3DPick = (pick: MolstarResiduePick) => {
    if (!exactLigandAtomLinks) return;
    const atomName = String(pick.atomName || '').trim();
    if (!atomName) return;
    if (normalizeChainToken(pick.chainId) !== normalizeChainToken(exactLigandAtomLinks.chainId)) return;
    if (pick.residue !== exactLigandAtomLinks.residue) return;
    const atomIndex = exactLigandAtomLinks.displayAtomIndexByAtomName.get(atomName);
    if (typeof atomIndex !== 'number') return;
    setSelectedLigandAtomIndex((current) => (current === atomIndex ? null : atomIndex));
  };

  return (
    <>
      <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
        <section className="structure-panel structure-panel--results-compact">
          {hasStructure ? (
            <MolstarViewer
              key={`affinity-results-viewer:${viewerColorMode}:${selectedLigandChainId || '-'}:${selectedTargetChainId || '-'}`}
              structureText={structureText}
              format={structureFormat}
              colorMode={viewerColorMode}
              confidenceBackend={confidenceBackend || projectBackend}
              scenePreset="lead_opt"
              leadOptStyleVariant="results"
              ligandFocusChainId={selectedLigandChainId || ''}
              interactionGranularity="element"
              onResiduePick={exactLigandAtomLinks ? handleLigand3DPick : undefined}
              highlightAtoms={highlightedLigandAtoms}
              activeAtom={activeLigandAtom}
              suppressAutoFocus={false}
              showSequence={false}
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

        <aside className="info-panel">
          <section className="result-aside-block result-aside-block-ligand">
            <div className="result-aside-head">
              <div className="result-aside-title">Ligand</div>
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
            <div className="ligand-preview-panel">
              <Ligand2DPreview
                smiles={ligandSmiles}
                atomConfidences={ligandAtomPlddts}
                confidenceHint={ligandConfidenceHint}
                highlightAtomIndices={selectedLigandAtomIndex === null ? null : [selectedLigandAtomIndex]}
                onAtomClick={exactLigandAtomLinks ? handleLigand2DAtomClick : undefined}
                onBackgroundClick={exactLigandAtomLinks ? () => setSelectedLigandAtomIndex(null) : undefined}
              />
            </div>
            {exactLigandAtomLinks ? (
              <div className="muted small top-margin">Click a ligand atom in 2D or 3D to inspect the same exact atom.</div>
            ) : null}
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
