import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';
import { CircleCheck, Dna, Eye, FlaskConical, Target } from 'lucide-react';
import { MolstarViewer, type MolstarAtomHighlight, type MolstarResiduePick } from './MolstarViewer';
import { JSMEEditor } from './JSMEEditor';
import { Ligand2DPreview } from './Ligand2DPreview';
import { LigandPropertyGrid } from './LigandPropertyGrid';
import { MetricsPanel } from './MetricsPanel';
import { resolveExactLigandAtomLinks } from './affinityAtomLinking';
import {
  InteractionsPanel,
  interactionResidueHighlights,
  parseInteractionsFromAffinity,
  type LigandInteraction
} from './InteractionsPanel';
import type { AffinityDockPocket, AffinityScoringMode } from '../../types/models';
import { PocketBoxControls } from './PocketBoxControls';
import { pocketTargetChanged, type PocketTargetSignature } from '../../utils/pocketBox';
import { useTabsKeyboard } from '../ui/useTabsKeyboard';

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
  dockPocket: AffinityDockPocket | null;
  seed: number | null;
  targetFileName: string;
  ligandFileName: string;
  ligandSmiles: string;
  ligandEditorInput: string;
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
  onConfidenceOnlyChange: (checked: boolean) => void;
  onBackendChange: (backend: string) => void;
  onModeChange: (mode: AffinityScoringMode) => void;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
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
  dockPocket,
  seed,
  targetFileName,
  ligandFileName,
  ligandSmiles,
  ligandEditorInput,
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
  onConfidenceOnlyChange,
  onBackendChange,
  onModeChange,
  onDockPocketChange,
  onSeedChange,
  onLigandSmilesChange,
  onResizerPointerDown,
  onResizerKeyDown
}: AffinityBasicsWorkspaceProps) {
  const isDockMode = mode === 'dock';
  const [pickedResidues, setPickedResidues] = useState<MolstarResiduePick[]>([]);
  const [boxWireframe, setBoxWireframe] = useState('');
  const [pocketDrawerOpen, setPocketDrawerOpen] = useState(false);
  // null = target not yet seen; only a real target change invalidates the pocket.
  const lastTargetSignatureRef = useRef<PocketTargetSignature | null>(null);

  useEffect(() => {
    if (!isDockMode) {
      setPickedResidues([]);
      setBoxWireframe('');
    }
  }, [isDockMode]);

  useEffect(() => {
    // A new target structure invalidates any pocket defined against the old one — the box
    // coordinates would dock against the wrong protein (the submitted box must be
    // remembered; see
    // pocketTargetChanged for why the preview-loading step must not count as a change).
    const next = { name: targetFileName.trim(), length: previewTargetStructureText.length };
    const previous = lastTargetSignatureRef.current;
    lastTargetSignatureRef.current = next;
    if (!pocketTargetChanged(previous, next)) return;
    setPickedResidues([]);
    setBoxWireframe('');
    onDockPocketChange(null);
  }, [targetFileName, previewTargetStructureText, onDockPocketChange]);

  const handleResiduePick = useCallback((pick: MolstarResiduePick) => {
    if (!isDockMode) return;
    setPickedResidues(prev => {
      const exists = prev.some(p => p.chainId === pick.chainId && p.residue === pick.residue);
      return exists
        ? prev.filter(p => !(p.chainId === pick.chainId && p.residue === pick.residue))
        : [...prev, pick];
    });
  }, [isDockMode]);

  const pickedHighlights = useMemo(
    () => (isDockMode ? pickedResidues.map(p => ({ chainId: p.chainId, residue: p.residue })) : undefined),
    [isDockMode, pickedResidues]
  );

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

          {!isDockMode ? (
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
          ) : (
            <div className="field affinity-upload-field affinity-upload-field--docked" title="Dock mode takes the ligand from the SMILES editor below">
              <span className="affinity-field-title">
                <FlaskConical size={13} />
                Ligand via SMILES
                {ligandSmiles.trim() ? <CircleCheck size={13} className="affinity-upload-ok" /> : null}
              </span>
              <div className="affinity-docked-ligand-hint">Use the editor below</div>
            </div>
          )}

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
              <option value="dock">Dock</option>
            </select>
          </label>

          {isDockMode ? (
            <div className="field affinity-inline-field">
              <span className="affinity-field-title">Box</span>
              <button
                type="button"
                className={`btn pocket-box-btn ${pocketDrawerOpen ? 'active' : ''}`}
                onClick={() => setPocketDrawerOpen(v => !v)}
                disabled={!canEdit || submitting}
              >
                {pocketDrawerOpen ? 'Hide' : 'Show'}
              </button>
            </div>
          ) : null}

          {/* Backend force-enables use_msa_server for this workflow. */}

          {!isDockMode ? (
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
          ) : null}
        </div>

      </div>

      <div ref={resultsGridRef} className={`results-grid ${isResultsResizing ? 'is-resizing' : ''}`} style={resultsGridStyle}>
        <section className="structure-panel">
          {previewTargetStructureText ? (
            <MolstarViewer
              structureText={previewTargetStructureText}
              format={previewTargetStructureFormat}
              overlayStructureText={isDockMode && boxWireframe ? boxWireframe : previewLigandStructureText}
              overlayFormat={isDockMode && boxWireframe ? 'pdb' : previewLigandStructureFormat}
              ligandFocusChainId={previewLigandChainId}
              autoFocusLigand={!isDockMode}
              colorMode="default"
              onResiduePick={handleResiduePick}
              pickMode="click"
              highlightResidues={pickedHighlights}
            />
          ) : targetFileName ? (
            <div className="ligand-preview-empty" role="status">Preparing 3D preview of {targetFileName}…</div>
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
          {isDockMode ? (
            <>
              {pocketDrawerOpen ? (
                <PocketBoxControls
                  pocket={dockPocket}
                  onPocketChange={onDockPocketChange}
                  proteinStructureText={previewTargetStructureText}
                  proteinStructureFormat={previewTargetStructureFormat}
                  pickedResidues={pickedResidues}
                  onBoxWireframeChange={setBoxWireframe}
                  onCollapse={() => setPocketDrawerOpen(false)}
                  canEdit={canEdit}
                  submitting={submitting}
                />
              ) : null}
            </>
          ) : null}
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
              <option value="boltz">Boltz2Dock</option>
              <option value="protenix">Protenix2Dock</option>
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
  snapshotAffinity?: Record<string, unknown> | null;
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
  snapshotAffinity = null,
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
  const interactionsReport = useMemo(() => parseInteractionsFromAffinity(snapshotAffinity), [snapshotAffinity]);
  const [selectedInteraction, setSelectedInteraction] = useState<LigandInteraction | null>(null);
  const [interactionAtomHighlights, setInteractionAtomHighlights] = useState<MolstarAtomHighlight[]>([]);
  // The workspace instance is reused across task results: a selection made against task A's
  // report must not keep highlighting residues/atoms on task B's structure. Render-time
  // adjustment (no effect pass): https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const [prevInteractionsReport, setPrevInteractionsReport] = useState(interactionsReport);
  if (interactionsReport !== prevInteractionsReport) {
    setPrevInteractionsReport(interactionsReport);
    setSelectedInteraction(null);
    setInteractionAtomHighlights([]);
  }

  const [prevViewerColorModeKey, setPrevViewerColorModeKey] = useState({
    initialViewerColorMode,
    structureText
  });
  if (initialViewerColorMode !== prevViewerColorModeKey.initialViewerColorMode || structureText !== prevViewerColorModeKey.structureText) {
    setPrevViewerColorModeKey({ initialViewerColorMode, structureText });
    setViewerColorMode(initialViewerColorMode);
  }

  // APG tabs keyboard behaviour for the 3D color mode switch.
  const colorModeTabs = useTabsKeyboard<'alphafold' | 'default'>(
    viewerColorMode,
    setViewerColorMode,
    ['alphafold', 'default']
  );

  const [prevExactLigandAtomLinks, setPrevExactLigandAtomLinks] = useState(exactLigandAtomLinks);
  if (exactLigandAtomLinks !== prevExactLigandAtomLinks) {
    setPrevExactLigandAtomLinks(exactLigandAtomLinks);
    if (
      !exactLigandAtomLinks ||
      selectedLigandAtomIndex === null ||
      selectedLigandAtomIndex < 0 ||
      selectedLigandAtomIndex >= exactLigandAtomLinks.atoms.length
    ) {
      setSelectedLigandAtomIndex(null);
    }
  }

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

  const highlightedLigandAtoms = useMemo<MolstarAtomHighlight[]>(() => {
    const base = activeLigandAtom ? [activeLigandAtom] : [];
    const interactionNames = new Set(interactionAtomHighlights.map((a) => a.atomName));
    return [
      ...interactionAtomHighlights,
      ...base.filter((a) => !interactionNames.has(a.atomName))
    ];
  }, [activeLigandAtom, interactionAtomHighlights]);

  const interactionResidues = useMemo(
    () => interactionResidueHighlights(interactionsReport, selectedInteraction),
    [interactionsReport, selectedInteraction]
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
              highlightResidues={interactionResidues}
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
              <div
                className="prediction-render-mode-switch"
                role="tablist"
                aria-label="3D color mode"
                {...colorModeTabs.props}
              >
                <button
                  type="button"
                  role="tab"
                  aria-selected={viewerColorMode === 'alphafold'}
                  tabIndex={colorModeTabs.tabTabIndex(viewerColorMode === 'alphafold')}
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
                  tabIndex={colorModeTabs.tabTabIndex(viewerColorMode === 'default')}
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

          <section className="result-aside-block">
            <div className="result-aside-title affinity-title-with-icon">
              <Target size={13} />
              Interactions
            </div>
            <InteractionsPanel
              report={interactionsReport}
              selectedInteraction={selectedInteraction}
              onSelectInteraction={(interaction) => {
                setSelectedInteraction(interaction);
                if (!interaction) setInteractionAtomHighlights([]);
              }}
              onAtomHighlight={setInteractionAtomHighlights}
              ligandChainId={exactLigandAtomLinks?.chainId}
              ligandResidueNumber={exactLigandAtomLinks ? exactLigandAtomLinks.residue : null}
            />
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
