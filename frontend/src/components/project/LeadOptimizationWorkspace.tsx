import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent
} from 'react';
import { Box, RotateCcw } from 'lucide-react';
import type { AffinityDockPocket, PredictionOptions } from '../../types/models';
import type { LeadOptPersistedUploads } from './leadopt/hooks/useLeadOptReferenceFragment';
import { useLeadOptReferenceFragment } from './leadopt/hooks/useLeadOptReferenceFragment';
import { resolveVariableSelection } from './leadopt/hooks/fragmentVariableSelection';
import { useLeadOptPocketBox } from './leadopt/hooks/useLeadOptPocketBox';
import { LeadOptReferencePanel } from './leadopt/LeadOptReferencePanel';
import { LeadOptFragmentPanel } from './leadopt/LeadOptFragmentPanel';
import {
  LeadOptHaloCandidatesPanel,
  LeadOptHaloParamsPanel,
  LeadOptHaloProgressPanel
} from './leadopt/LeadOptHaloPanels';
import { useLeadOptHaloRun, type LeadOptHaloCandidate } from './leadopt/hooks/useLeadOptHaloRun';
import type { LeadOptHaloMode } from '../../api/backendLeadOptimizationApi';
import { PocketBoxControls } from './PocketBoxControls';

export interface LeadOptHaloSnapshot {
  taskId: string | null;
  candidates: LeadOptHaloCandidate[];
  roundsLog: Array<Record<string, unknown>>;
  mode: string;
  backend: string;
  roundsCompleted: number | null;
  totalRounds: number | null;
}

interface LeadOptimizationWorkspaceProps {
  viewMode: 'reference' | 'design';
  canEdit: boolean;
  submitting: boolean;
  proteinSequence: string;
  ligandSmiles: string;
  targetChain: string;
  ligandChain: string;
  onLigandSmilesChange: (value: string) => void;
  referenceScopeKey?: string;
  persistedReferenceUploads?: LeadOptPersistedUploads | null;
  onReferenceUploadsChange?: (uploads: LeadOptPersistedUploads) => void;
  options: PredictionOptions;
  onOptionChange: (
    key: 'leadOptMode' | 'leadOptBackend' | 'leadOptRounds' | 'leadOptBudgetPerRound' | 'leadOptScaffoldHopRatio'
      | 'leadOptPocketCenter' | 'leadOptReferenceSmiles' | 'leadOptKeepFragmentSmiles' | 'leadOptEditAtomIndices',
    value: string | number | null
  ) => void;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
  haloSnapshot: LeadOptHaloSnapshot | null;
  onHaloTaskQueued: (payload: { taskId: string; requestPayload: Record<string, unknown> }) => Promise<void> | void;
  onHaloTaskCompleted: (payload: {
    taskId: string;
    candidates: LeadOptHaloCandidate[];
    roundsLog: Array<Record<string, unknown>>;
    roundsCompleted: number | null;
    totalRounds: number | null;
    mode: string;
    backend: string;
  }) => Promise<void> | void;
  onHaloTaskFailed: (payload: { taskId: string; error: string }) => Promise<void> | void;
  onNavigateToResults?: () => void;
  onRegisterHeaderRunAction?: (action: (() => void | Promise<void>) | null) => void;
}

function detectStructureFormat(fileName: string): 'pdb' | 'cif' {
  return fileName.toLowerCase().endsWith('.pdb') ? 'pdb' : 'cif';
}

// Docking-style resizable split: 3D reference on the left, fragments + run
// parameters on the right (mmpdb-era layout).
const LEFT_PANEL_MIN = 360;
const RIGHT_PANEL_MIN = 300;
const RESIZER_WIDTH = 10;
const LEFT_PANEL_KEY_STEP = 24;
const LEFT_PANEL_DEFAULT = 720;

/**
 * HALO lead-optimization workspace (mmpdb retrieval flow retired):
 * Build tab — docking-style reference viewer (uploads + pocket box on the
 * target, remembered; clear = blind) on the left, fragment selection
 * (2D RDKit click-select, auto-split) + iteration params on the right.
 * Design tab — per-round progress + ranked candidates from the RL loop.
 */
export function LeadOptimizationWorkspace({
  viewMode,
  canEdit,
  submitting,
  ligandSmiles,
  targetChain,
  ligandChain,
  onLigandSmilesChange,
  referenceScopeKey,
  persistedReferenceUploads,
  onReferenceUploadsChange,
  options,
  onOptionChange,
  onDockPocketChange,
  haloSnapshot,
  onHaloTaskQueued,
  onHaloTaskCompleted,
  onHaloTaskFailed,
  onNavigateToResults
}: LeadOptimizationWorkspaceProps) {
  const [error, setError] = useState('');
  const [leftPanelWidth, setLeftPanelWidth] = useState(LEFT_PANEL_DEFAULT);
  const [isResizing, setIsResizing] = useState(false);
  const layoutRef = useRef<HTMLDivElement | null>(null);
  const resizeStateRef = useRef<{ startX: number; startWidth: number } | null>(null);

  const reference = useLeadOptReferenceFragment({
    ligandSmiles,
    onLigandSmilesChange,
    currentVariableQuery: '',
    onAutoVariableQuery: () => {},
    onError: (message) => setError(message || ''),
    scopeKey: referenceScopeKey || `${targetChain}:${ligandChain}`,
    persistedUploads: persistedReferenceUploads ?? undefined,
    deferHydrationPreview: viewMode !== 'reference',
    onPersistedUploadsChange: onReferenceUploadsChange
  });

  const halo = useLeadOptHaloRun({
    onTaskQueued: async ({ taskId, input }) => {
      await onHaloTaskQueued({ taskId, requestPayload: input as unknown as Record<string, unknown> });
    },
    onTaskCompleted: onHaloTaskCompleted,
    onTaskFailed: onHaloTaskFailed
  });

  const backend = options.leadOptBackend || 'protenix2dock';

  const selectedFragmentItems = useMemo(
    () => reference.fragments.filter((fragment) => reference.selectedFragmentIds.includes(fragment.fragment_id)),
    [reference.fragments, reference.selectedFragmentIds]
  );
  const variableSelection = useMemo(
    () => resolveVariableSelection(selectedFragmentItems, reference.fragments, reference.ligandAtomBonds),
    [selectedFragmentItems, reference.fragments, reference.ligandAtomBonds]
  );
  const selectedItems = variableSelection.effectiveItems;
  const selectedAtomIndices = selectedItems.flatMap((item) => item.atom_indices);

  const targetUpload = reference.persistedUploads?.target || null;
  const targetStructure = useMemo(
    () =>
      targetUpload && targetUpload.content.trim()
        ? {
            fileName: targetUpload.fileName,
            format: detectStructureFormat(targetUpload.fileName),
            content: targetUpload.content
          }
        : null,
    [targetUpload]
  );

  const pocket = useLeadOptPocketBox({
    targetStructure,
    referenceOverlayText: reference.previewOverlayStructureText,
    referenceOverlayFormat: reference.previewOverlayStructureFormat,
    referenceReady: reference.referenceReady,
    dockPocket: options.leadOptDockPocket ?? null,
    onDockPocketChange,
    onPocketCenterChange: (center) => onOptionChange('leadOptPocketCenter', center)
  });

  const pocketLabel = options.leadOptPocketCenter
    ? `center ${options.leadOptPocketCenter}`
    : 'empty — blind docking over the whole target';

  const referenceSmiles = (options.leadOptReferenceSmiles || '').trim() || reference.effectiveLigandSmiles.trim();
  const keepFragment = (options.leadOptKeepFragmentSmiles || '').trim() || variableSelection.variableSmilesList.join('.');
  const editAtomIndices = options.leadOptEditAtomIndices || (selectedAtomIndices.length > 0
    ? selectedAtomIndices.join(',')
    : '');

  // Mode is inferred from what the user selected, not chosen: a reference
  // ligand / selected fragments means fragment replacement; without any
  // reference the run generates de novo inside the pocket box.
  const hasReference = Boolean(referenceSmiles || keepFragment);
  const mode: LeadOptHaloMode = hasReference ? 'fragment' : 'denovo';

  const runDisabledReason = useMemo(() => {
    if (!targetUpload) return 'Upload a target structure first.';
    if (!hasReference && !options.leadOptPocketCenter) {
      return 'Upload a reference ligand or select fragments first.';
    }
    return '';
  }, [targetUpload, hasReference, options.leadOptPocketCenter]);

  const runOptimization = useCallback(async () => {
    if (!targetUpload) {
      setError('Upload a target structure first.');
      return;
    }
    setError('');
    const ligandUpload = reference.persistedUploads?.ligand || null;
    const input = {
      mode,
      backend,
      protein_upload: {
        content_base64: btoa(unescape(encodeURIComponent(targetUpload.content))),
        file_name: targetUpload.fileName
      },
      ...(ligandUpload && ligandUpload.content.trim()
        ? {
            reference_upload: {
              content_base64: btoa(unescape(encodeURIComponent(ligandUpload.content))),
              file_name: ligandUpload.fileName
            }
          }
        : {}),
      reference_smiles: referenceSmiles || undefined,
      keep_fragment_smiles: keepFragment || undefined,
      edit_atom_indices: editAtomIndices || undefined,
      pocket: options.leadOptPocketCenter || undefined,
      scaffold_hop_ratio: options.leadOptScaffoldHopRatio ?? 0.4,
      rounds: options.leadOptRounds ?? 6,
      budget_per_round: options.leadOptBudgetPerRound ?? 48,
      target_chain: targetChain,
      priority: 'default'
    };
    await halo.submit(input);
    if (onNavigateToResults) onNavigateToResults();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    targetUpload,
    reference.persistedUploads?.ligand,
    mode,
    backend,
    referenceSmiles,
    keepFragment,
    editAtomIndices,
    options.leadOptPocketCenter,
    options.leadOptScaffoldHopRatio,
    options.leadOptRounds,
    options.leadOptBudgetPerRound,
    targetChain,
    halo.submit,
    onNavigateToResults
  ]);

  const designCandidates = halo.runState.candidates.length > 0
    ? halo.runState.candidates
    : haloSnapshot?.candidates || [];
  const designRoundsLog = halo.runState.roundsLog.length > 0
    ? halo.runState.roundsLog
    : haloSnapshot?.roundsLog || [];

  const computeLeftBounds = useCallback((containerWidth: number): { min: number; max: number } => {
    const minWidth = LEFT_PANEL_MIN;
    const maxWidth = containerWidth - RIGHT_PANEL_MIN - RESIZER_WIDTH - 12;
    return { min: minWidth, max: Math.max(minWidth, maxWidth) };
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

  useEffect(() => {
    if (!isResizing) return;
    const handleMove = (moveEvent: PointerEvent) => {
      const state = resizeStateRef.current;
      const container = layoutRef.current;
      if (!state || !container) return;
      const delta = moveEvent.clientX - state.startX;
      const bounds = computeLeftBounds(container.clientWidth);
      setLeftPanelWidth(Math.max(bounds.min, Math.min(bounds.max, state.startWidth + delta)));
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
  }, [computeLeftBounds, isResizing]);

  const layoutStyle = useMemo(
    () => ({ '--lead-opt-left-width': `${leftPanelWidth}px` }) as CSSProperties,
    [leftPanelWidth]
  );

  if (viewMode === 'design') {
    return (
      <div className="lead-opt-layout">
        <div className="lead-opt-main">
          <LeadOptHaloProgressPanel progress={halo.runState.progress} roundsLog={designRoundsLog} />
          {error ? <div className="alert error">{error}</div> : null}
          <LeadOptHaloCandidatesPanel
            candidates={designCandidates}
            mode={haloSnapshot?.mode || halo.runState.mode}
            backend={haloSnapshot?.backend || halo.runState.backend}
          />
        </div>
      </div>
    );
  }

  const pocketToolbar = targetStructure ? (
    <div className="lead-opt-pocket-toolbar">
      <button
        type="button"
        className={`btn pocket-box-btn ${pocket.drawerOpen ? 'active' : ''}`}
        onClick={pocket.toggleDrawer}
        disabled={!canEdit}
        title="Show the box controls (center/size sliders, ligand presets)"
      >
        <Box size={12} />
        {pocket.drawerOpen ? 'Hide box' : 'Box'}
      </button>
      <button
        type="button"
        className="btn btn-ghost btn-compact"
        onClick={pocket.clearPocket}
        disabled={!canEdit}
        title="Remove the box — optimization runs blind over the whole target"
      >
        <RotateCcw size={11} />
        Clear
      </button>
    </div>
  ) : null;

  return (
    <div className="lead-opt-workspace">
      {error ? <div className="alert error">{error}</div> : null}
      <div ref={layoutRef} className="lead-opt-layout lead-opt-layout--resizable" style={layoutStyle}>
        <LeadOptReferencePanel
          canEdit={canEdit}
          loading={reference.busy}
          submitting={submitting}
          referenceReady={reference.referenceReady}
          previewStructureText={reference.previewStructureText}
          previewStructureFormat={reference.previewStructureFormat}
          previewOverlayStructureText={reference.previewOverlayStructureText}
          previewOverlayStructureFormat={reference.previewOverlayStructureFormat}
          boxOverlayText={pocket.boxWireframe}
          pocketToolbar={pocketToolbar}
          pocketControls={
            pocket.drawerOpen && targetStructure ? (
              <PocketBoxControls
                pocket={options.leadOptDockPocket ?? null}
                onPocketChange={onDockPocketChange}
                proteinStructureText={targetStructure.content}
                proteinStructureFormat={targetStructure.format}
                pickedResidues={[]}
                onBoxWireframeChange={pocket.applyBoxWireframe}
                onCollapse={pocket.toggleDrawer}
                canEdit={canEdit}
                submitting={false}
              />
            ) : null
          }
          ligandChain={reference.referenceLigandChainId || ligandChain}
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

        <div className="lead-opt-side">
          <LeadOptFragmentPanel
            effectiveLigandSmiles={reference.fragmentSourceSmiles || reference.effectiveLigandSmiles}
            fragments={reference.fragments}
            selectedFragmentIds={reference.selectedFragmentIds}
            activeFragmentId={reference.activeFragmentId}
            onAtomClick={reference.handleFragmentAtomClick}
            onToggleFragmentSelection={reference.toggleFragmentSelection}
            onClearFragmentSelection={reference.clearFragmentSelection}
          />
          <LeadOptHaloParamsPanel
            canEdit={canEdit}
            running={halo.running}
            backend={backend}
            rounds={options.leadOptRounds ?? 6}
            budgetPerRound={options.leadOptBudgetPerRound ?? 48}
            pocketLabel={pocketLabel}
            canRun={!runDisabledReason}
            runDisabledReason={runDisabledReason}
            onBackendChange={(value) => onOptionChange('leadOptBackend', value)}
            onRoundsChange={(value) => onOptionChange('leadOptRounds', value)}
            onBudgetChange={(value) => onOptionChange('leadOptBudgetPerRound', value)}
            onRun={runOptimization}
          />
        </div>
      </div>
    </div>
  );
}
