import { useCallback, useMemo, useState } from 'react';
import type { AffinityDockPocket, PredictionOptions } from '../../types/models';
import type { LeadOptPersistedUploads } from './leadopt/hooks/useLeadOptReferenceFragment';
import { useLeadOptReferenceFragment } from './leadopt/hooks/useLeadOptReferenceFragment';
import { resolveVariableSelection } from './leadopt/hooks/fragmentVariableSelection';
import { LeadOptReferencePanel } from './leadopt/LeadOptReferencePanel';
import { LeadOptFragmentPanel } from './leadopt/LeadOptFragmentPanel';
import {
  LeadOptHaloCandidatesPanel,
  LeadOptHaloParamsPanel,
  LeadOptHaloProgressPanel
} from './leadopt/LeadOptHaloPanels';
import { useLeadOptHaloRun, type LeadOptHaloCandidate } from './leadopt/hooks/useLeadOptHaloRun';
import { LeadOptPocketPicker } from '../../pages/projectDetail/LeadOptPocketPicker';

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

/**
 * HALO lead-optimization workspace (mmpdb retrieval flow retired):
 * Build tab — reference uploads + fragment selection (preserved), docking-style
 * pocket box on the target (remembered; clear = blind), iteration params.
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
  const [oracleConcurrency, setOracleConcurrency] = useState(8);

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

  const mode = options.leadOptMode || 'fragment';
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

  const pocketLabel = options.leadOptPocketCenter
    ? `center ${options.leadOptPocketCenter}`
    : 'empty — blind docking over the whole target';

  const referenceSmiles = (options.leadOptReferenceSmiles || '').trim() || reference.effectiveLigandSmiles.trim();
  const keepFragment = (options.leadOptKeepFragmentSmiles || '').trim() || variableSelection.variableSmilesList.join('.');
  const editAtomIndices = options.leadOptEditAtomIndices || (selectedAtomIndices.length > 0
    ? selectedAtomIndices.join(',')
    : '');

  const runDisabledReason = useMemo(() => {
    if (!targetUpload) return 'Upload a target structure first.';
    if (!referenceSmiles && !keepFragment && mode === 'denovo' && !options.leadOptPocketCenter) {
      return 'De novo needs a pocket box (or a reference ligand) for placement.';
    }
    if (mode !== 'denovo' && !referenceSmiles && !keepFragment) {
      return 'Upload a reference ligand or select fragments first.';
    }
    return '';
  }, [targetUpload, mode, options.leadOptPocketCenter, referenceSmiles, keepFragment]);

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
      oracle_concurrency: oracleConcurrency,
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
    oracleConcurrency,
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

  return (
    <div className="lead-opt-layout">
      <div className="lead-opt-main">
        {error ? <div className="alert error">{error}</div> : null}
        <LeadOptReferencePanel
          canEdit={canEdit}
          loading={reference.busy}
          submitting={submitting}
          referenceReady={reference.referenceReady}
          previewStructureText={reference.previewStructureText}
          previewStructureFormat={reference.previewStructureFormat}
          previewOverlayStructureText={reference.previewOverlayStructureText}
          previewOverlayStructureFormat={reference.previewOverlayStructureFormat}
          ligandChain={reference.referenceLigandChainId || ligandChain}
          highlightedLigandAtoms={reference.highlightedLigandAtoms}
          highlightedPocketResidues={reference.highlightedPocketResidues}
          activeMolstarAtom={reference.activeMolstarAtom}
          onResiduePick={reference.handleMolstarResiduePick}
          onTargetFileChange={reference.handleTargetFileChange}
          onLigandFileChange={reference.handleLigandFileChange}
        />
        <LeadOptFragmentPanel
          effectiveLigandSmiles={reference.fragmentSourceSmiles || reference.effectiveLigandSmiles}
          fragments={reference.fragments}
          selectedFragmentIds={reference.selectedFragmentIds}
          activeFragmentId={reference.activeFragmentId}
          onAtomClick={reference.handleFragmentAtomClick}
          onToggleFragmentSelection={reference.toggleFragmentSelection}
          onClearFragmentSelection={reference.clearFragmentSelection}
        />
        <LeadOptPocketPicker
          canEdit={canEdit}
          targetStructure={targetStructure}
          dockPocket={options.leadOptDockPocket ?? null}
          onDockPocketChange={onDockPocketChange}
          onPocketCenterChange={(center) => onOptionChange('leadOptPocketCenter', center)}
        />
      </div>
      <div className="lead-opt-side">
        <LeadOptHaloParamsPanel
          canEdit={canEdit}
          running={halo.running}
          mode={mode}
          backend={backend}
          rounds={options.leadOptRounds ?? 6}
          budgetPerRound={options.leadOptBudgetPerRound ?? 48}
          scaffoldHopRatio={options.leadOptScaffoldHopRatio ?? 0.4}
          oracleConcurrency={oracleConcurrency}
          pocketLabel={pocketLabel}
          canRun={!runDisabledReason}
          runDisabledReason={runDisabledReason}
          onModeChange={(value) => onOptionChange('leadOptMode', value)}
          onBackendChange={(value) => onOptionChange('leadOptBackend', value)}
          onRoundsChange={(value) => onOptionChange('leadOptRounds', value)}
          onBudgetChange={(value) => onOptionChange('leadOptBudgetPerRound', value)}
          onScaffoldHopRatioChange={(value) => onOptionChange('leadOptScaffoldHopRatio', value)}
          onOracleConcurrencyChange={setOracleConcurrency}
          onRun={runOptimization}
        />
      </div>
    </div>
  );
}
