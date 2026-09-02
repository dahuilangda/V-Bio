import { useCallback, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { Link } from 'react-router-dom';
import type {
  InputComponent,
  PredictionConstraint,
  ProjectInputConfig,
  ProjectTask,
  ProteinTemplateUpload,
  VirtualScreeningPredictionRecord
} from '../../types/models';
import { downloadResultFile, terminateTask as terminateBackendTask } from '../../api/backendApi';
import { deleteProjectTask } from '../../api/supabaseLite';
import { createInputComponent, saveProjectInputConfig } from '../../utils/projectInputs';
import type { LeadOptHaloCandidate } from '../../components/project/leadopt/hooks/useLeadOptHaloRun';
import { normalizeTaskSummary } from '../../utils/taskMetadata';
import { getWorkflowDefinition } from '../../utils/workflows';
import { ProjectDetailLayout } from './ProjectDetailLayout';
import {
  filterConstraintsByBackend,
} from './projectDraftUtils';
import { useProjectResultDisplay } from './useProjectResultDisplay';
import { useProjectRunHandlers } from './useProjectRunHandlers';
import {
  constraintLabel,
  formatConstraintCombo as formatConstraintComboForWorkspace,
  formatConstraintDetail as formatConstraintDetailForWorkspace
} from './constraintWorkspaceUtils';
import { useConstraintWorkspaceActions } from './useConstraintWorkspaceActions';
import { scrollToEditorBlock } from './editorActions';
import { useProjectEditorHandlers } from './useProjectEditorHandlers';
import { useProjectSidebarActions } from './useProjectSidebarActions';
import { useProjectWorkflowSectionProps } from './useProjectWorkflowSectionProps';
import { useProjectRunState } from './useProjectRunState';
import { usePredictionWorkspaceProps } from './usePredictionWorkspaceProps';
import { useProjectDetailRuntimeContext } from './useProjectDetailRuntimeContext';
import { useAuth } from '../../hooks/useAuth';
import {
  buildLeadOptUploadSnapshotComponents,
  mergeTaskInputOptionsIntoProperties,
  type LeadOptPersistedUploads
} from './projectTaskSnapshot';
import type { CopilotPlanAction } from '../../types/models';
import { detectStructureFormat, extractProteinChainSequences, fetchValidatedStructure, resolveStructureFormat, rcsbCifUrl } from '../../utils/structureParser';
import { computeAutoPocketBox } from '../../utils/pocketBox';
import { peptidePocketSummaryLabel } from '../../utils/peptidePocket';
import { useCopilotAvailability } from '../../hooks/useCopilotAvailability';
import {
  ProjectCopilotModal,
  clearStoredCopilotTaskPrefill,
  readStoredCopilotOpen,
  readStoredCopilotTaskPrefill,
  writeStoredCopilotOpen
} from '../../components/copilot/ProjectCopilotModal';
import type { CopilotUploadedAttachment, CopilotAttachmentApplication } from '../../components/copilot/ProjectCopilotModal';

import {
  readText,
  asRecord,
  hasExplicitPeptideResiduePool,
  summarizeCopilotComponents,
  summarizeCopilotConstraints,
  summarizeCopilotTask,
  normalizeCopilotPrefillComponents,
  applyCopilotComponentPatchOperations,
  readFiniteNumber,
  hasPersistedIpsaeMetric,
  pickPreferredLeadOptTask
} from './workspaceViewHelpers';

type WorkspaceRuntime = ReturnType<typeof useProjectDetailRuntimeContext>;
type WorkspaceRuntimeReady = WorkspaceRuntime & {
  project: NonNullable<WorkspaceRuntime['project']>;
  draft: NonNullable<WorkspaceRuntime['draft']>;
};

export function useProjectDetailWorkspaceView() {
  const runtime = useProjectDetailRuntimeContext();
  const { locationSearch, entryRoutingResolved, loading, error, project, draft } = runtime;

  // Stale-while-revalidate: a refetch of the SAME project (submit, copilot prefill, param
  // change) keeps the current workspace on screen — the full-screen "Loading project..."
  // placeholder unmounted everything and remounted it, which the user saw as a flash. The
  // placeholder stays only for a genuinely new project (or the very first load).
  const projectId = String(runtime.projectId || '');
  if (!entryRoutingResolved || (loading && (!project || project.id !== projectId))) {
    const query = new URLSearchParams(locationSearch);
    const requestedTaskRowId =
      String(query.get('task_row_id') || '').trim() || String(query.get('source_task_row_id') || '').trim();
    const loadingLabel =
      !entryRoutingResolved
        ? 'Loading project...'
        : requestedTaskRowId || query.get('tab') === 'results'
          ? 'Loading current task...'
          : 'Loading project...';
    return <div className="centered-page">{loadingLabel}</div>;
  }

  if (error && !project) {
    return (
      <div className="page-grid">
        <div className="alert error">{error}</div>
        <Link className="btn btn-ghost" to="/projects">
          Back to projects
        </Link>
      </div>
    );
  }

  if (!project || !draft) {
    return null;
  }

  return <ProjectDetailWorkspaceLoaded runtime={runtime as WorkspaceRuntimeReady} />;
}

function ProjectDetailWorkspaceLoaded({ runtime }: { runtime: WorkspaceRuntimeReady }) {
  const { session } = useAuth();
  const copilotAvailable = useCopilotAvailability();
  const [headerStopRunPending, setHeaderStopRunPending] = useState(false);
  const [copilotOpen, setCopilotOpen] = useState(() => readStoredCopilotOpen({ contextType: 'task_detail', userId: session?.userId || null }));
  useEffect(() => {
    writeStoredCopilotOpen({ contextType: 'task_detail', userId: session?.userId || null }, copilotOpen);
  }, [copilotOpen, session?.userId]);


  const {
    loading,
    error,
    setError,
    project,
    draft,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isVirtualScreeningWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workspaceTab,
    hasIncompleteComponents,
    componentCompletion,
    submitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFile,
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError,
    affinityTargetChainIds,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    affinityMode,
    chainInfoById,
    componentTypeBuckets,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    activeConstraintId,
    selectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintPickSlotRef,
    updateConstraintPickSlot,
    constraintPickSlot,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    structureText,
    structureFormat,
    structureTaskId,
    confidenceBackend,
    projectBackend,
    activeResultTask,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreview,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity,
    snapshotConfidence,
    statusInfo,
    statusContextTaskRow,
    requestedStatusTaskRow,
    projectTasks,
    snapshotLigandAtomPlddts,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponent,
    snapshotLigandResiduePlddts,
    setProteinTemplates,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions,
    pickedResidue,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    constraintSelectedAtomRefs,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    allowedConstraintTypes,
    sidebarTypeOpen,
    activeComponentId,
    sidebarConstraintsOpen,
    selectedContactConstraintIdSet,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    affinityEnableDisabledReason,
    isResultsResizing,
    resultsGridRef,
    handleResultsResizerPointerDown,
    handleResultsResizerKeyDown,
    resultsGridStyle,
    resultChainIds,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityConfidenceOnlyChange,
    onAffinityModeChange,
    setAffinityLigandSmiles,
    leadOptPrimary,
    leadOptChainContext,
    leadOptPersistedUploads,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    handleComponentsResizerPointerDown,
    handleComponentsResizerKeyDown,
    proteinTemplates,
    customResidueLibrary,
    setCustomResidueLibrary,
    displayTaskState,
    isActiveRuntime,
    progressPercent,
    totalRuntimeSeconds,
    hasUnsavedChanges,
    runMenuOpen,
    runSuccessNotice,
    resultError,
    resultChainConsistencyWarning,
    runActionRef,
    topRunButtonRef,
    affinityDockPocket,
    onAffinityDockPocketChange,
    snapshotPic50,
    snapshotPic50Mw,
    displaySubmittedAt,
    patchTask,
    pullResultForViewer,
    persistDraftTaskSnapshot,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saveDraft,
    setRunRedirectTaskId,
    navigate,
    affinityLigandFile
  } = runtime;


  const handleLeadOptHaloTaskCompleted = async (payload: {
    taskId: string;
    candidates: LeadOptHaloCandidate[];
    roundsLog: Array<Record<string, unknown>>;
    roundsCompleted: number | null;
    totalRounds: number | null;
    mode: string;
    backend: string;
  }) => {
    const taskId = readText(payload.taskId).trim();
    const taskRowId = leadOptHaloTaskRowMapRef.current[taskId];
    const completedAt = new Date().toISOString();
    const rowPatch: Partial<ProjectTask> = {
      task_state: 'SUCCESS',
      status_text: `Optimization complete (${payload.candidates.length} candidates).`,
      error_text: '',
      completed_at: completedAt
    };
    if (taskRowId) {
      await patchTask(taskRowId, {
        ...rowPatch,
        confidence: {
          lead_opt_halo: {
            mode: payload.mode,
            backend: payload.backend,
            rounds_completed: payload.roundsCompleted,
            total_rounds: payload.totalRounds,
            rounds_log: payload.roundsLog,
            candidates: payload.candidates,
            candidate_count: payload.candidates.length
          }
        } as ProjectTask['confidence']
      });
      delete leadOptHaloTaskRowMapRef.current[taskId];
      return;
    }
    const fallbackRow = projectTasks.find((row) => readText(row.task_id).trim() === taskId);
    if (fallbackRow) {
      await patchTask(fallbackRow.id, {
        ...rowPatch,
        confidence: {
          lead_opt_halo: {
            mode: payload.mode,
            backend: payload.backend,
            rounds_completed: payload.roundsCompleted,
            total_rounds: payload.totalRounds,
            rounds_log: payload.roundsLog,
            candidates: payload.candidates,
            candidate_count: payload.candidates.length
          }
        } as ProjectTask['confidence']
      });
    }
    delete leadOptHaloTaskRowMapRef.current[taskId];
  };

  const handleLeadOptHaloTaskFailed = async (payload: { taskId: string; error: string }) => {
    const taskId = readText(payload.taskId).trim();
    const taskRowId = leadOptHaloTaskRowMapRef.current[taskId];
    const errorText = readText(payload.error).trim() || 'Optimization failed.';
    const rowPatch: Partial<ProjectTask> = {
      task_state: 'FAILURE',
      status_text: `Optimization failed${errorText ? `: ${errorText.slice(0, 140)}` : ''}`,
      error_text: errorText,
      completed_at: new Date().toISOString()
    };
    if (taskRowId) {
      await patchTask(taskRowId, rowPatch);
      delete leadOptHaloTaskRowMapRef.current[taskId];
      return;
    }
    const fallbackRow = projectTasks.find((row) => readText(row.task_id).trim() === taskId);
    if (fallbackRow) {
      await patchTask(fallbackRow.id, rowPatch);
    }
  };

  const handleLeadOptReferenceUploadsChange = useCallback(
    async (uploads: LeadOptPersistedUploads) => {
      if (!project || !draft || !canEdit) return;
      if (workspaceTab !== 'components') return;
      const targetName = readText(uploads.target?.fileName).trim();
      const targetSize = readText(uploads.target?.content).length;
      const ligandName = readText(uploads.ligand?.fileName).trim();
      const ligandSize = readText(uploads.ligand?.content).length;
      const contextDraftRowId =
        String((requestedStatusTaskRow || statusContextTaskRow)?.task_state || '').toUpperCase() === 'DRAFT'
          ? readText((requestedStatusTaskRow || statusContextTaskRow)?.id).trim()
          : '';
      const editableDraftRowId = contextDraftRowId;
      const effectiveLeadOptLigandSmiles = readText(leadOptPrimary.ligandSmiles).trim();
      const dedupeKey = `${project.id}|${editableDraftRowId}|${targetName}:${targetSize}|${ligandName}:${ligandSize}|${effectiveLeadOptLigandSmiles}`;
      if (leadOptUploadPersistKeyRef.current === dedupeKey) return;
      leadOptUploadPersistKeyRef.current = dedupeKey;

      const snapshotComponents = buildLeadOptUploadSnapshotComponents(
        draft.inputConfig.components,
        uploads,
        effectiveLeadOptLigandSmiles
      );
      setDraft((prev) =>
        prev
          ? {
              ...prev,
              inputConfig: {
                ...prev.inputConfig,
                components: snapshotComponents
              }
            }
          : prev
      );

      if (editableDraftRowId) {
        await patchTask(editableDraftRowId, {
          components: snapshotComponents,
          protein_sequence: leadOptPrimary.proteinSequence,
          ligand_smiles: effectiveLeadOptLigandSmiles
        });
      }
    },
    [
      canEdit,
      draft,
      leadOptPrimary.ligandSmiles,
      leadOptPrimary.proteinSequence,
      patchTask,
      project,
      setDraft,
      requestedStatusTaskRow,
      statusContextTaskRow,
      workspaceTab
    ]
  );

  const handleCopilotAttachments = useCallback(
    async (attachments: CopilotUploadedAttachment[], _content: string, applications?: CopilotAttachmentApplication[]) => {
      if (!canEdit || attachments.length === 0) return;
      if (!applications || applications.length === 0) return;
      const applicationsById = new Map(
        applications.map((application) => [application.attachmentId, application])
      );
      const roleEntries = attachments.map((attachment) => {
        const application = applicationsById.get(attachment.id);
        if (application && application.fileName !== attachment.name) {
          throw new Error('Copilot attachment declaration does not match the selected file.');
        }
        return {
          attachment,
          role: application?.role || null
        };
      });

      if (isAffinityWorkflow || isPeptideDesignWorkflow) {
        const target = roleEntries.find((entry) => entry.role === 'target')?.attachment || null;
        const ligand = roleEntries.find((entry) => entry.role === 'ligand')?.attachment || null;
        if (target) onAffinityTargetFileChange(target.file);
        if (isAffinityWorkflow && ligand) onAffinityLigandFileChange(ligand.file);
        // The target/ligand editors and 3D preview only render on the Components
        // tab; switch there so the applied files are actually visible.
        if (isAffinityWorkflow && (target || ligand)) setWorkspaceTab('components');
        return;
      }

      if (isLeadOptimizationWorkflow) {
        const target = roleEntries.find((entry) => entry.role === 'target')?.attachment || null;
        const ligand = roleEntries.find((entry) => entry.role === 'ligand')?.attachment || null;
        if (!target && !ligand) return;
        const readUpload = async (attachment: CopilotUploadedAttachment | null) =>
          attachment ? { fileName: attachment.name, content: await attachment.file.text() } : null;
        await handleLeadOptReferenceUploadsChange({
          target: await readUpload(target),
          ligand: await readUpload(ligand)
        });
        return;
      }

      if (isPredictionWorkflow) {
        const template = roleEntries.find((entry) => entry.role === 'template')?.attachment || null;
        if (!template) return;
        const targetProteinComponent = draft.inputConfig.components.find((component) => component.type === 'protein') || null;
        if (!targetProteinComponent) {
          setError('Copilot could not attach the template because the current prediction task has no protein component.');
          return;
        }
        const format = detectStructureFormat(template.name);
        if (!format) {
          setError('Copilot template upload supports .pdb, .ent, .cif, or .mmcif files.');
          return;
        }
        try {
          const contentText = await template.file.text();
          const chainSequences = extractProteinChainSequences(contentText, format);
          const chainIds = Object.keys(chainSequences).sort((a, b) => a.localeCompare(b));
          if (chainIds.length === 0) {
            setError('Copilot could not parse a protein chain from the uploaded template.');
            return;
          }
          const upload: ProteinTemplateUpload = {
            fileName: template.name,
            format,
            content: contentText,
            chainId: chainIds[0],
            chainSequences
          };
          setProteinTemplates((prev) => ({ ...prev, [targetProteinComponent.id]: upload }));
          setError(null);
          setWorkspaceTab('components');
        } catch (err) {
          setError(err instanceof Error ? err.message : 'Failed to read Copilot template upload.');
        }
      }
    },
    [
      canEdit,
      draft.inputConfig.components,
      handleLeadOptReferenceUploadsChange,
      isAffinityWorkflow,
      isLeadOptimizationWorkflow,
      isPeptideDesignWorkflow,
      isPredictionWorkflow,
      onAffinityLigandFileChange,
      onAffinityTargetFileChange,
      setError,
      setProteinTemplates,
      setWorkspaceTab
    ]
  );

  const workflow = getWorkflowDefinition(project.task_type);
  const runSubmitting = submitting;
  const leadOptWorkspaceTargetChain = readText(leadOptChainContext.targetChain).trim();
  const leadOptWorkspaceLigandChain = readText(leadOptChainContext.ligandChain).trim();

  const copilotSequenceAppliedRef = useRef(false);
  const saveDraftRef = useRef(saveDraft);
  saveDraftRef.current = saveDraft;

  const [copilotPrefillSave, setCopilotPrefillSave] = useState<{ components: InputComponent[] } | null>(null);
  useEffect(() => {
    if (copilotSequenceAppliedRef.current) return;
    const query = new URLSearchParams(runtime.locationSearch);
    const copilotComponentsRaw = String(query.get('copilot_components') || '').trim();
    const copilotSequence = String(query.get('copilot_sequence') || '').trim();
    const copilotParameterPatchRaw = String(query.get('copilot_parameter_patch') || '').trim();
    const copilotScreeningInput = String(query.get('copilot_screening_input') || '').trim();
    const storedCopilotPrefill =
      session?.userId && project?.id
        ? readStoredCopilotTaskPrefill(session.userId, project.id)
        : null;
    if ((!copilotComponentsRaw && !copilotSequence && !copilotParameterPatchRaw && !copilotScreeningInput && !storedCopilotPrefill) || !draft || !project) return;
    copilotSequenceAppliedRef.current = true;
    const aminoAcidPattern = /^[ACDEFGHIKLMNPQRSTVWY]+$/i;
    query.delete('copilot_components');
    query.delete('copilot_sequence');
    query.delete('copilot_parameter_patch');
    query.delete('copilot_screening_input');
    const nextSearch = query.toString();
    navigate(
      { pathname: window.location.pathname, search: nextSearch ? `?${nextSearch}` : '' },
      { replace: true }
    );
    let nextComponents: InputComponent[] = [];
    if (copilotComponentsRaw) {
      try {
        nextComponents = normalizeCopilotPrefillComponents(JSON.parse(copilotComponentsRaw));
      } catch {
        nextComponents = [];
      }
    }
    if (storedCopilotPrefill) {
      const storedComponents = normalizeCopilotPrefillComponents(storedCopilotPrefill.components);
      if (storedComponents.length > nextComponents.length) {
        nextComponents = storedComponents;
      }
    }
    if (nextComponents.length === 0 && aminoAcidPattern.test(copilotSequence)) {
      nextComponents = [{
        id: `copilot-protein-1`,
        type: 'protein',
        sequence: copilotSequence.toUpperCase(),
        numCopies: 1,
        useMsa: true,
      }];
    }
    const parameterPatch = (() => {
      if (!copilotParameterPatchRaw) return {};
      try {
        return asRecord(JSON.parse(copilotParameterPatchRaw));
      } catch {
        return {};
      }
    })();
    const replacement = asRecord(parameterPatch.componentsReplacement);
    const replacementComponents = normalizeCopilotPrefillComponents(replacement.components);
    const addedComponents = normalizeCopilotPrefillComponents(parameterPatch.componentsAdd);
    const patchedByOperations = applyCopilotComponentPatchOperations(draft.inputConfig.components, parameterPatch.componentsPatch);
    const componentOperationsChanged = patchedByOperations !== draft.inputConfig.components;
    const backendPatch = readText(parameterPatch.backend).trim().toLowerCase();
    const backendPatchAllowed = isVirtualScreeningWorkflow
      ? backendPatch === 'nesso'
      : backendPatch === 'boltz' || backendPatch === 'alphafold3' || backendPatch === 'protenix';
    const seedPatch = readFiniteNumber(parameterPatch.seed);
    const hasPatch =
      replacementComponents.length > 0 ||
      addedComponents.length > 0 ||
      componentOperationsChanged ||
      backendPatchAllowed ||
      seedPatch !== null;
    if (nextComponents.length === 0 && !hasPatch) return;
    setDraft((prev) =>
      {
        if (!prev) return prev;
        const patchedComponents =
          replacementComponents.length > 0
            ? replacementComponents
            : nextComponents.length > 0
              ? nextComponents
              : componentOperationsChanged
                ? applyCopilotComponentPatchOperations(prev.inputConfig.components, parameterPatch.componentsPatch)
              : addedComponents.length > 0
                ? [...prev.inputConfig.components, ...addedComponents]
                : prev.inputConfig.components;
        return {
          ...prev,
          backend: backendPatchAllowed ? backendPatch : prev.backend,
          inputConfig: {
            ...prev.inputConfig,
            version: 1,
            components: patchedComponents,
            constraints:
              nextComponents.length > 0 || (replacementComponents.length > 0 && replacement.clearConstraints !== false)
                ? []
                : prev.inputConfig.constraints,
            options: {
              ...prev.inputConfig.options,
              ...(seedPatch !== null ? { seed: Math.max(0, Math.floor(seedPatch)) } : {}),
              ...(copilotScreeningInput
                ? { virtualScreeningInput: copilotScreeningInput, virtualScreeningInputMode: 'paste' as const }
                : {})
            }
          },
        };
      }
    );
    if (storedCopilotPrefill && session?.userId && project.id) {
      clearStoredCopilotTaskPrefill(session.userId, project.id);
    }
    setCopilotPrefillSave({
      components:
        replacementComponents.length > 0
          ? replacementComponents
          : nextComponents.length > 0
            ? nextComponents
            : componentOperationsChanged
              ? patchedByOperations
            : addedComponents.length > 0
              ? [...draft.inputConfig.components, ...addedComponents]
              : draft.inputConfig.components
    });
  }, [draft, isVirtualScreeningWorkflow, navigate, project, runtime.locationSearch, session?.userId, setDraft]);

  useEffect(() => {
    if (!copilotPrefillSave || !draft) return;
    const hasAllComponents = copilotPrefillSave.components.every((expected) =>
      draft.inputConfig.components.some(
        (component) =>
          component.type === expected.type &&
          readText(component.sequence).trim() === readText(expected.sequence).trim()
      )
    );
    if (!hasAllComponents) return;
    setCopilotPrefillSave(null);
    void saveDraft();
  }, [copilotPrefillSave, draft, saveDraft]);

  const leadOptHaloTaskRowMapRef = useRef<Record<string, string>>({});
  const leadOptUploadPersistKeyRef = useRef('');
  const resultViewHydrationAttemptedRef = useRef<Set<string>>(new Set());
  const virtualScreeningPredictionPersistSignatureRef = useRef('');
  const virtualScreeningPredictionPersistQueueRef = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    if (workspaceTab !== 'results') return;
    if (isVirtualScreeningWorkflow) return;
    if (!isPredictionWorkflow && !isAffinityWorkflow) return;
    const contextTask = activeResultTask || statusContextTaskRow;
    const taskId = readText(contextTask?.task_id || project.task_id).trim();
    const taskRowId = readText(contextTask?.id).trim();
    const taskState = readText(contextTask?.task_state || project.task_state).trim().toUpperCase();
    if (!taskId || taskState !== 'SUCCESS') return;
    const snapshotConfidenceSource = contextTask?.confidence ?? project.confidence;
    if (hasPersistedIpsaeMetric(snapshotConfidenceSource)) return;
    const hydrationKey = `${taskRowId || '__project__'}:${taskId}`;
    if (resultViewHydrationAttemptedRef.current.has(hydrationKey)) return;
    resultViewHydrationAttemptedRef.current.add(hydrationKey);
    void pullResultForViewer(taskId, {
      taskRowId: taskRowId || undefined,
      persistProject: readText(project.task_id).trim() === taskId,
      resultMode: 'view'
    });
  }, [
    activeResultTask,
    isAffinityWorkflow,
    isPredictionWorkflow,
    isVirtualScreeningWorkflow,
    project.confidence,
    project.task_id,
    project.task_state,
    pullResultForViewer,
    statusContextTaskRow,
    workspaceTab
  ]);

  const preferredLeadOptSnapshotTask = useMemo(
    () => pickPreferredLeadOptTask(projectTasks),
    [projectTasks]
  );

  // HALO snapshot: candidates/rounds persisted on the task row by the run
  // handlers below (live runs carry their own copy inside the workspace).
  const leadOptHaloSnapshot = useMemo(() => {
    const row = preferredLeadOptSnapshotTask || requestedStatusTaskRow || statusContextTaskRow || activeResultTask || null;
    const confidence = asRecord(row?.confidence);
    const halo = asRecord(confidence.lead_opt_halo);
    if (Object.keys(halo).length === 0) return null;
    const candidates = Array.isArray(halo.candidates) ? halo.candidates : [];
    const roundsLog = Array.isArray(halo.rounds_log) ? halo.rounds_log : [];
    return {
      taskId: readText(row?.task_id).trim() || null,
      candidates: candidates as LeadOptHaloCandidate[],
      roundsLog: roundsLog as Array<Record<string, unknown>>,
      mode: readText(halo.mode),
      backend: readText(halo.backend),
      roundsCompleted: Number.isFinite(Number(halo.rounds_completed)) ? Number(halo.rounds_completed) : null,
      totalRounds: Number.isFinite(Number(halo.total_rounds)) ? Number(halo.total_rounds) : null
    };
  }, [preferredLeadOptSnapshotTask, requestedStatusTaskRow, statusContextTaskRow, activeResultTask]);

  const handleLeadOptHaloTaskQueued = async (payload: {
    taskId: string;
    requestPayload: Record<string, unknown>;
  }) => {
    const taskId = readText(payload.taskId).trim();
    if (!taskId) return;
    const snapshotComponents = buildLeadOptUploadSnapshotComponents(
      draft.inputConfig.components,
      leadOptPersistedUploads,
      leadOptPrimary.ligandSmiles
    );
    const draftTaskRow = await persistDraftTaskSnapshot(draft.inputConfig, {
      statusText: 'Optimization queued',
      reuseTaskRowId: null,
      snapshotComponents
    });
    leadOptHaloTaskRowMapRef.current[taskId] = draftTaskRow.id;
    setRunRedirectTaskId(taskId);
    await patchTask(draftTaskRow.id, {
      task_id: taskId,
      task_state: 'QUEUED',
      status_text: 'Optimization queued',
      backend: readText((payload.requestPayload as Record<string, unknown>).backend) || 'protenix2dock',
      confidence: {
        lead_opt_halo: {
          mode: readText((payload.requestPayload as Record<string, unknown>).mode) || 'fragment',
          backend: readText((payload.requestPayload as Record<string, unknown>).backend) || 'protenix2dock',
          stage: 'queued'
        }
      } as ProjectTask['confidence']
    });
  };

  const {
    componentStepLabel,
    isRunRedirecting,
    showQuickRunFab,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    runBlockedReason,
    runDisabled,
    canOpenRunMenu,
    sidebarTypeOrder
  } = useProjectRunState({
    workspaceTab,
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isVirtualScreeningWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    hasIncompleteComponents,
    componentCompletion,
    virtualScreeningInput: draft.inputConfig.options.virtualScreeningInput || '',
    virtualScreeningComponents: draft.inputConfig.components,
    submitting: runSubmitting,
    saving,
    runRedirectTaskId,
    showFloatingRunButton,
    affinityTargetFilePresent: Boolean(affinityTargetFile),
    affinityPreviewLoading,
    affinityPreviewCurrent,
    affinityPreviewError: String(affinityPreviewError || ''),
    affinityDockMode: affinityMode === 'dock',
    affinityDockPocketPresent: Boolean(affinityDockPocket),
    affinityTargetChainCount: affinityTargetChainIds.length,
    affinityLigandChainId,
    affinityLigandSmiles,
    affinityHasLigand,
    affinitySupportsActivity,
    affinityConfidenceOnly,
    affinityConfidenceOnlyLocked,
    draftBackend: draft.backend
  });
  const formatConstraintCombo = (constraint: PredictionConstraint) =>
    formatConstraintComboForWorkspace(constraint, chainInfoById, componentTypeBuckets);
  const formatConstraintDetail = (constraint: PredictionConstraint) =>
    formatConstraintDetailForWorkspace(constraint);
  const {
    addComponentToDraft,
    addConstraintFromSidebar,
    setAffinityEnabledFromWorkspace,
    setAffinityComponentFromWorkspace,
    jumpToComponent
  } = useProjectSidebarActions({
    draft,
    setDraft,
    setWorkspaceTab,
    setActiveComponentId,
    setSidebarTypeOpen,
    normalizedDraftComponents,
    setSidebarConstraintsOpen,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
    activeChainInfos,
    ligandChainOptions,
    constraintsSupported: allowedConstraintTypes.length > 0,
    isBondOnlyBackend,
    canEnableAffinityFromWorkspace,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    createInputComponent
  });
  const {
    clearConstraintSelection,
    selectConstraint,
    jumpToConstraint,
    navigateConstraint,
    focusConstraintPickSlot,
    applyPickToSelectedConstraint
  } = useConstraintWorkspaceActions({
    draft,
    setDraft,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    resolveTemplateComponentIdForConstraint,
    constraintSelectionAnchorRef,
    setWorkspaceTab,
    setSidebarConstraintsOpen,
    scrollToEditorBlock,
    constraintPickSlotRef,
    updateConstraintPickSlot,
    activeChainInfos,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    setPickedResidue,
    canEdit,
    ligandChainOptions,
    constraintsSupported: allowedConstraintTypes.length > 0,
    isBondOnlyBackend
  });
  const {
    displayStructureText,
    displayStructureConfidenceText,
    displayStructureFormat,
    displayStructureName,
    displayStructureColorMode,
    constraintStructureText,
    constraintStructureFormat,
    hasConstraintStructure,
    snapshotCards,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    affinityResultLigandSmiles,
    affinityResultLigandAtomPlddts,
    predictionLigandPreview,
    predictionLigandRadarSmiles,
    affinityDisplayStructureText,
    affinityDisplayStructureFormat,
    hasAffinityDisplayStructure,
  } = useProjectResultDisplay({
    shouldPrepareResultStructure: workspaceTab === 'results' && isPredictionWorkflow && !isVirtualScreeningWorkflow,
    shouldPrepareConstraintStructure: workspaceTab === 'constraints',
    shouldPrepareSnapshotCards:
      workspaceTab === 'results' && !isVirtualScreeningWorkflow && (isPredictionWorkflow || isAffinityWorkflow),
    shouldPreparePredictionLigandPreview:
      workspaceTab === 'results' && isPredictionWorkflow && !isVirtualScreeningWorkflow,
    shouldPrepareAffinityResultDisplay: workspaceTab === 'results' && isAffinityWorkflow,
    structureText,
    structureFormat,
    confidenceBackend,
    projectBackend,
    activeResultTaskStructureName: activeResultTask?.structure_name || '',
    projectStructureName: project.structure_name || '',
    draftColorMode: draft.color_mode,
    hasAf3ConfidenceSignals,
    hasProtenixConfidenceSignals,
    selectedTemplatePreviewContent: selectedTemplatePreview?.content || '',
    selectedTemplatePreviewFormat: selectedTemplatePreview?.format || 'pdb',
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    resultChainShortLabelById,
    snapshotPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotLigandMeanPlddt,
    snapshotPlddtTone,
    snapshotIptm,
    snapshotSelectedPairIptm,
    snapshotIc50Um,
    snapshotPic50,
    snapshotPic50Mw,
    snapshotIc50Error,
    snapshotIc50Tone,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotBindingTone,
    affinityPreviewTargetStructureText,
    affinityPreviewTargetStructureFormat,
    affinityPreviewLigandStructureText,
    affinityPreviewLigandStructureFormat,
    snapshotAffinity: snapshotAffinity || null,
    snapshotConfidence: snapshotConfidence || null,
    statusContextLigandSmiles: String(statusContextTaskRow?.ligand_smiles || ''),
    activeResultLigandSmiles: String(activeResultTask?.ligand_smiles || ''),
    snapshotLigandAtomPlddts: snapshotLigandAtomPlddts || [],
    affinityLigandSmiles,
    overviewPrimaryLigand,
    selectedResultLigandSequence,
    selectedResultLigandComponentType: selectedResultLigandComponent?.type || null,
    selectedResultLigandModifications: selectedResultLigandComponent?.modifications,
    snapshotLigandResiduePlddts,
  });
  const {
    handlePredictionComponentsChange,
    handlePredictionProteinTemplateChange,
    handlePredictionTemplateResiduePick,
    handleRuntimeBackendChange,
    handleRuntimeSeedChange,
    handleRuntimeLowVramChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideChiralityChange,
    handleRuntimePeptideStructureUploadChange,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimePeptideLengthRange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptidePocketFieldChange,
    handleRuntimePeptideDockPocketChange,
    handleRuntimeLeadOptOptionChange,
    handleRuntimeLeadOptDockPocketChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideResiduePoolChange,
    handleRuntimePeptideNonNaturalRangeChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleTaskNameChange,
    handleTaskSummaryChange
  } = useProjectEditorHandlers({
    isPeptideDesignWorkflow,
    setDraft,
    setPickedResidue,
    setProteinTemplates,
    filterConstraintsByBackend
  });
  // Peptide-design target context: the protein component owning the target
  // chain (properties.target, falling back to the first protein chain) hosts
  // the binding-pocket picker inside its component editor block — its uploaded
  // structure enables the docking-style 3D box, otherwise the target sequence
  // gets the constraint-style residue selection.
  const peptideTargetContext = useMemo(() => {
    const targetChain = String(draft.inputConfig.properties?.target || '').trim();
    const proteinChains = activeChainInfos.filter((info) => info.type === 'protein');
    const ownerChain =
      (targetChain
        ? proteinChains.find((info) => info.id === targetChain) || null
        : null) || proteinChains[0] || null;
    const component = ownerChain
      ? normalizedDraftComponents.find((item) => item.id === ownerChain.componentId) || null
      : null;
    return {
      componentId: component?.id || null,
      chainId: ownerChain?.id || null,
      sequence: component?.sequence || ''
    };
  }, [
    draft.inputConfig.properties?.target,
    activeChainInfos,
    normalizedDraftComponents
  ]);

  const { predictionConstraintsWorkspaceProps, predictionComponentsSidebarProps } = usePredictionWorkspaceProps({
    workspaceTab,
    draft,
    setDraft,
    filterConstraintsByBackend,
    constraintsWorkspaceRef,
    isConstraintsResizing,
    constraintsGridStyle,
    constraintCount,
    activeConstraintIndex,
    constraintTemplateOptions: constraintTemplateOptions || [],
    selectedTemplatePreview,
    setSelectedConstraintTemplateComponentId,
    canEdit,
    setWorkspaceTab,
    navigateConstraint,
    pickedResidue,
    hasConstraintStructure,
    constraintStructureText,
    constraintStructureFormat,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue,
    constraintSelectedAtomRefs,
    applyPickToSelectedConstraint,
    focusConstraintPickSlot,
    activeConstraintPickSlot: constraintPickSlot[activeConstraintId ?? ''] ?? 'first',
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    clearConstraintSelection,
    activeConstraintId,
    selectedContactConstraintIds,
    selectConstraint,
    allowedConstraintTypes,
    isBondOnlyBackend,
    hasIncompleteComponents,
    componentCompletion,
    sidebarTypeOrder,
    componentTypeBuckets,
    sidebarTypeOpen,
    setSidebarTypeOpen,
    addComponentToDraft,
    activeComponentId,
    jumpToComponent,
    sidebarConstraintsOpen,
    setSidebarConstraintsOpen,
    addConstraintFromSidebar,
    hasActiveChains: activeChainInfos.length > 0,
    selectedContactConstraintIdSet,
    jumpToConstraint,
    constraintLabel,
    formatConstraintCombo,
    formatConstraintDetail,
    canEnableAffinityFromWorkspace,
    setAffinityEnabledFromWorkspace,
    selectedWorkspaceTarget,
    selectedWorkspaceLigand,
    workspaceTargetOptions,
    workspaceLigandSelectableOptions,
    setAffinityComponentFromWorkspace,
    affinityEnableDisabledReason,
    showAffinityComputeToggle: !isPeptideDesignWorkflow,
    peptidePocket:
      isPeptideDesignWorkflow && peptideTargetContext.componentId
        ? {
            summaryLabel: peptidePocketSummaryLabel(
              draft.inputConfig.options.peptidePocketCenter,
              draft.inputConfig.options.peptidePocketResidues
            ),
            targetComponentId: peptideTargetContext.componentId
          }
        : null
  });
  const peptideResiduePoolAvailable = useMemo(() => {
    if (!isPeptideDesignWorkflow) return true;
    const query = new URLSearchParams(runtime.locationSearch);
    const requestedTaskRowId = readText(query.get('task_row_id')).trim();
    const taskRow =
      (requestedTaskRowId
        ? projectTasks.find((row) => readText(row.id).trim() === requestedTaskRowId)
        : null) ||
      requestedStatusTaskRow ||
      statusContextTaskRow ||
      activeResultTask ||
      null;
    const state = readText(taskRow?.task_state).trim().toUpperCase();
    if (state && state !== 'DRAFT') return hasExplicitPeptideResiduePool(taskRow);
    return true;
  }, [activeResultTask, isPeptideDesignWorkflow, projectTasks, requestedStatusTaskRow, runtime.locationSearch, statusContextTaskRow]);

  const handlePeptideRequestStructure = useCallback(async (options?: { preferredStructureName?: string }) => {
    const contextTask = activeResultTask || statusContextTaskRow;
    const taskId = String(contextTask?.task_id || project.task_id || '').trim();
    if (!taskId) return;
    await pullResultForViewer(taskId, {
      taskRowId: contextTask?.id || undefined,
      persistProject: String(project.task_id || '').trim() === taskId,
      resultMode: 'view',
      preferredStructureName: options?.preferredStructureName
    });
  }, [activeResultTask, project.task_id, pullResultForViewer, statusContextTaskRow]);

  const handleVirtualScreeningPredictionsChange = useCallback((
    records: Record<string, VirtualScreeningPredictionRecord>
  ) => {
    if (!isVirtualScreeningWorkflow || !canEdit) return;
    const normalizedRecords = records || {};
    const screeningTaskRow = statusContextTaskRow || requestedStatusTaskRow || activeResultTask;
    const signature = `${project.id}:${readText(screeningTaskRow?.id).trim()}:${JSON.stringify(
      Object.keys(normalizedRecords)
        .sort((left, right) => left.localeCompare(right))
        .map((key) => [key, normalizedRecords[key]])
    )}`;
    if (virtualScreeningPredictionPersistSignatureRef.current === signature) return;
    virtualScreeningPredictionPersistSignatureRef.current = signature;

    const nextOptions: ProjectInputConfig['options'] = {
      ...draft.inputConfig.options,
      virtualScreeningPredictions: normalizedRecords
    };
    const nextConfig: ProjectInputConfig = {
      ...draft.inputConfig,
      options: nextOptions
    };
    setDraft((previous) => {
      if (!previous) return previous;
      return {
        ...previous,
        inputConfig: {
          ...previous.inputConfig,
          options: {
            ...previous.inputConfig.options,
            virtualScreeningPredictions: normalizedRecords
          }
        }
      };
    });
    saveProjectInputConfig(project.id, nextConfig);

    if (!screeningTaskRow?.id) return;
    const sourceProperties = (screeningTaskRow.properties || nextConfig.properties) as ProjectInputConfig['properties'];
    // Persist ONLY the job records. Merging the live draft options here would
    // write unsaved editor state into the viewed row's stored options.
    const patchPayload = {
      properties: mergeTaskInputOptionsIntoProperties(sourceProperties, {
        virtualScreeningPredictions: normalizedRecords
      })
    };
    virtualScreeningPredictionPersistQueueRef.current =
      virtualScreeningPredictionPersistQueueRef.current
        .catch(() => undefined)
        .then(async () => {
          await patchTask(screeningTaskRow.id, patchPayload);
        })
        .catch((persistError) => {
          setError(
            persistError instanceof Error
              ? persistError.message
              : 'Failed to persist virtual-screening structure jobs.'
          );
        });
  }, [
    activeResultTask,
    canEdit,
    draft.inputConfig,
    isVirtualScreeningWorkflow,
    patchTask,
    project.id,
    requestedStatusTaskRow,
    saveProjectInputConfig,
    setDraft,
    setError,
    statusContextTaskRow
  ]);

  const {
    projectResultsSectionProps,
    affinityWorkflowSectionProps,
    leadOptimizationWorkflowSectionProps,
    predictionWorkflowSectionProps,
    virtualScreeningWorkflowSectionProps,
    workflowRuntimeSettingsSectionProps
  } = useProjectWorkflowSectionProps({
    isPredictionWorkflow,
    isPeptideDesignWorkflow,
    isVirtualScreeningWorkflow,
    isAffinityWorkflow,
    isLeadOptimizationWorkflow,
    workflowTitle: workflow.title,
    workflowShortTitle: workflow.shortTitle,
    projectTaskState: displayTaskState || project.task_state || '',
    projectTaskId:
      String(statusContextTaskRow?.task_id || '').trim() ||
      String(activeResultTask?.task_id || '').trim() ||
      String(project.task_id || '').trim(),
    statusInfo: statusInfo || null,
    progressPercent,
    onPeptideRequestStructure: handlePeptideRequestStructure,
    resultsGridRef,
    isResultsResizing,
    resultsGridStyle,
    onResultsResizerPointerDown: handleResultsResizerPointerDown,
    onResultsResizerKeyDown: handleResultsResizerKeyDown,
    snapshotCards,
    snapshotConfidence: snapshotConfidence || null,
    snapshotAffinity: snapshotAffinity || null,
    resultChainIds,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    displayStructureText: isPeptideDesignWorkflow ? structureText : displayStructureText,
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
    affinityResultLigandSmiles,
    affinityResultLigandAtomPlddts,
    affinityTargetChainIds,
    affinityLigandChainId,
    snapshotLigandAtomPlddts,
    snapshotPlddt,
    snapshotIptm,
    snapshotSelectedPairIptm,
    selectedResultLigandSequence,
    canEdit,
    submitting,
    affinityTargetFileName: affinityTargetFile?.name || '',
    affinityLigandFileName: affinityLigandFile?.name || '',
    affinityLigandSmiles,
    affinityPreviewLigandSmiles: String(affinityPreview?.ligandSmiles || ''),
    affinityMode,
    affinityDockPocket,
    affinityConfidenceOnlyUiValue,
    affinityConfidenceOnlyUiLocked,
    affinityPreviewStructureText,
    affinityPreviewStructureFormat,
    affinityPreviewLigandOverlayText,
    affinityPreviewLigandOverlayFormat,
    onAffinityTargetFileChange,
    onAffinityLigandFileChange,
    onAffinityConfidenceOnlyChange,
    onAffinityModeChange,
    onAffinityDockPocketChange,
    setAffinityLigandSmiles,
    leadOptProteinSequence: leadOptPrimary.proteinSequence,
    leadOptLigandSmiles: leadOptPrimary.ligandSmiles,
    leadOptTargetChain: leadOptWorkspaceTargetChain,
    leadOptLigandChain: leadOptWorkspaceLigandChain,
    leadOptReferenceScopeKey: `${project.id}:leadopt`,
    leadOptPersistedReferenceUploads: leadOptPersistedUploads,
    onLeadOptReferenceUploadsChange: handleLeadOptReferenceUploadsChange,
    onLeadOptHaloTaskQueued: handleLeadOptHaloTaskQueued,
    onLeadOptHaloTaskCompleted: handleLeadOptHaloTaskCompleted,
    onLeadOptHaloTaskFailed: handleLeadOptHaloTaskFailed,
    onLeadOptNavigateToResults: () => {
      setWorkspaceTab('results');
    },
    leadOptHaloSnapshot,
    leadOptOptions: draft.inputConfig.options,
    onLeadOptOptionChange: handleRuntimeLeadOptOptionChange,
    onLeadOptDockPocketChange: handleRuntimeLeadOptDockPocketChange,
    setDraft,
    setWorkspaceTab,
    workspaceTab,
    componentsWorkspaceRef,
    isComponentsResizing,
    componentsGridStyle,
    onComponentsResizerPointerDown: handleComponentsResizerPointerDown,
    onComponentsResizerKeyDown: handleComponentsResizerKeyDown,
    components: draft.inputConfig.components,
    onComponentsChange: handlePredictionComponentsChange,
    virtualScreeningInput: draft.inputConfig.options.virtualScreeningInput || '',
    virtualScreeningInputMode: draft.inputConfig.options.virtualScreeningInputMode || 'upload',
    virtualScreeningInputFileName: draft.inputConfig.options.virtualScreeningInputFileName || '',
    virtualScreeningPredictionRecords:
      draft.inputConfig.options.virtualScreeningPredictions || {},
    onVirtualScreeningPredictionRecordsChange: handleVirtualScreeningPredictionsChange,
    proteinTemplates,
    customResidueLibrary,
    onCustomResidueLibraryChange: setCustomResidueLibrary,
    onProteinTemplateChange: handlePredictionProteinTemplateChange,
    activeComponentId,
    setActiveComponentId,
    onProteinTemplateResiduePick: handlePredictionTemplateResiduePick,
    predictionConstraintsWorkspaceProps,
    predictionComponentsSidebarProps,
    backend: draft.backend,
    seed: draft.inputConfig.options.seed ?? null,
    lowVram: draft.inputConfig.options.lowVram ?? false,
    peptideDesignMode: draft.inputConfig.options.peptideDesignMode ?? 'linear',
    peptideChirality: draft.inputConfig.options.peptideChirality ?? 'l',
    peptideBinderLength: draft.inputConfig.options.peptideBinderLength ?? 20,
    peptideLengthMin: draft.inputConfig.options.peptideLengthMin ?? 10,
    peptideLengthMax: draft.inputConfig.options.peptideLengthMax ?? 25,
    peptideUseInitialSequence: draft.inputConfig.options.peptideUseInitialSequence ?? false,
    peptideInitialSequence: draft.inputConfig.options.peptideInitialSequence ?? '',
    peptideStructureUpload: draft.inputConfig.options.peptideStructureUpload ?? null,
    peptideSequenceMask:
      draft.inputConfig.options.peptideSequenceMask ??
      'X'.repeat(Math.max(1, draft.inputConfig.options.peptideBinderLength ?? 20)),
    peptideIterations: draft.inputConfig.options.peptideIterations ?? 12,
    peptideTargetComponentId: peptideTargetContext.componentId,
    peptideTargetChainId: peptideTargetContext.chainId,
    peptideTargetSequence: peptideTargetContext.sequence,
    peptidePocketCenter: draft.inputConfig.options.peptidePocketCenter ?? '',
    peptidePocketResidues: draft.inputConfig.options.peptidePocketResidues ?? '',
    peptidePocketBox: draft.inputConfig.options.peptidePocketBox ?? 6,
    peptideDockPocket: draft.inputConfig.options.peptideDockPocket ?? null,
    onPeptideDockPocketChange: handleRuntimePeptideDockPocketChange,
    peptidePopulationSize: draft.inputConfig.options.peptidePopulationSize ?? 16,
    peptideEliteSize: draft.inputConfig.options.peptideEliteSize ?? 5,
    peptideResiduePool: draft.inputConfig.options.peptideResiduePool ?? [],
    peptideResiduePoolAvailable,
    peptideNonNaturalMin: draft.inputConfig.options.peptideNonNaturalMin ?? 0,
    peptideNonNaturalMax: draft.inputConfig.options.peptideNonNaturalMax ?? 0,
    peptideBicyclicLinkerCcd: draft.inputConfig.options.peptideBicyclicLinkerCcd ?? 'SEZ',
    peptideBicyclicCysPositionMode: draft.inputConfig.options.peptideBicyclicCysPositionMode ?? 'auto',
    peptideBicyclicFixTerminalCys: draft.inputConfig.options.peptideBicyclicFixTerminalCys ?? true,
    peptideBicyclicIncludeExtraCys: draft.inputConfig.options.peptideBicyclicIncludeExtraCys ?? false,
    peptideBicyclicCys1Pos: draft.inputConfig.options.peptideBicyclicCys1Pos ?? 3,
    peptideBicyclicCys2Pos: draft.inputConfig.options.peptideBicyclicCys2Pos ?? 8,
    peptideBicyclicCys3Pos:
      draft.inputConfig.options.peptideBicyclicCys3Pos ??
      (draft.inputConfig.options.peptideBinderLength ?? 20),
    onBackendChange: handleRuntimeBackendChange,
    onSeedChange: handleRuntimeSeedChange,
    onLowVramChange: handleRuntimeLowVramChange,
    onPeptideDesignModeChange: handleRuntimePeptideDesignModeChange,
    onPeptideChiralityChange: handleRuntimePeptideChiralityChange,
    onPeptideStructureUploadChange: handleRuntimePeptideStructureUploadChange,
    onPeptideLengthRange: handleRuntimePeptideLengthRange,
    onPeptideUseInitialSequenceChange: handleRuntimePeptideUseInitialSequenceChange,
    onPeptideInitialSequenceChange: handleRuntimePeptideInitialSequenceChange,
    onPeptideSequenceMaskChange: handleRuntimePeptideSequenceMaskChange,
    onPeptideIterationsChange: handleRuntimePeptideIterationsChange,
    onPeptidePocketFieldChange: handleRuntimePeptidePocketFieldChange,
    onPeptidePopulationSizeChange: handleRuntimePeptidePopulationSizeChange,
    onPeptideEliteSizeChange: handleRuntimePeptideEliteSizeChange,
    onPeptideResiduePoolChange: handleRuntimePeptideResiduePoolChange,
    onPeptideNonNaturalRangeChange: handleRuntimePeptideNonNaturalRangeChange,
    onPeptideBicyclicLinkerCcdChange: handleRuntimePeptideBicyclicLinkerCcdChange,
    onPeptideBicyclicCysPositionModeChange: handleRuntimePeptideBicyclicCysPositionModeChange,
    onPeptideBicyclicFixTerminalCysChange: handleRuntimePeptideBicyclicFixTerminalCysChange,
    onPeptideBicyclicIncludeExtraCysChange: handleRuntimePeptideBicyclicIncludeExtraCysChange,
    onPeptideBicyclicCys1PosChange: handleRuntimePeptideBicyclicCys1PosChange,
    onPeptideBicyclicCys2PosChange: handleRuntimePeptideBicyclicCys2PosChange,
    onPeptideBicyclicCys3PosChange: handleRuntimePeptideBicyclicCys3PosChange
  });
  const taskListPage = useMemo(() => {
    const query = new URLSearchParams(runtime.locationSearch);
    const parsed = Number(query.get('task_list_page') || '');
    if (!Number.isFinite(parsed)) return 1;
    return Math.max(1, Math.floor(parsed));
  }, [runtime.locationSearch]);
  const taskHistoryPath = useMemo(() => {
    const query = new URLSearchParams();
    if (taskListPage > 1) {
      query.set('page', String(taskListPage));
    }
    const search = query.toString();
    return `/projects/${project.id}/tasks${search ? `?${search}` : ''}`;
  }, [project.id, taskListPage]);
  const {
    handleRunAction,
    handleRunCurrentDraft,
    handleRestoreSavedDraft,
    handleResetFromHeader,
    handleWorkspaceFormSubmit,
    handleOpenTaskHistory,
  } = useProjectRunHandlers({
    runDisabled,
    submitTask,
    setRunMenuOpen,
    loadProject,
    saving,
    submitting,
    loading,
    hasUnsavedChanges,
    saveDraft,
    taskHistoryPath,
    setRunRedirectTaskId,
    navigate,
  });

  // Lead-opt runs from the Optimization panel's own Run button; the header
  // Run stays disabled for this workflow with a pointer to the panel.
  const effectiveRunDisabled = runDisabled || isLeadOptimizationWorkflow;
  const effectiveRunBlockedReason = isLeadOptimizationWorkflow
    ? 'Use the Run Optimization button in the Lead Optimization workspace.'
    : runBlockedReason;
  const submitTaskRef = useRef(submitTask);
  const runDisabledRef = useRef(effectiveRunDisabled);
  const runBlockedReasonRef = useRef(effectiveRunBlockedReason);

  useEffect(() => {
    submitTaskRef.current = submitTask;
    runDisabledRef.current = effectiveRunDisabled;
    runBlockedReasonRef.current = effectiveRunBlockedReason;
  }, [effectiveRunBlockedReason, effectiveRunDisabled, submitTask]);

  const headerRuntimeTaskId =
    readText(statusContextTaskRow?.task_id).trim() ||
    readText(activeResultTask?.task_id).trim() ||
    readText(project.task_id).trim();
  const headerRuntimeStateToken = readText(displayTaskState || project.task_state).trim().toUpperCase();
  const showHeaderStopAction =
    isPeptideDesignWorkflow &&
    Boolean(headerRuntimeTaskId) &&
    (headerRuntimeStateToken === 'RUNNING' || headerRuntimeStateToken === 'QUEUED');
  const headerStopRunTitle = headerRuntimeStateToken === 'RUNNING' ? 'Stop run' : 'Cancel queued run';
  const headerStopRunDisabled = !showHeaderStopAction || headerStopRunPending || runSubmitting;
  const handleHeaderRunAction = () => {
    if (isLeadOptimizationWorkflow) {
      return;
    }
    handleRunAction();
  };
  const handleHeaderStopAction = () => {
    if (!showHeaderStopAction || headerStopRunDisabled) return;
    if (!headerRuntimeTaskId) return;
    const actionToken = headerRuntimeStateToken === 'RUNNING' ? 'stop' : 'cancel';
    if (!window.confirm(`Confirm ${actionToken} for task "${headerRuntimeTaskId}"?`)) return;
    setHeaderStopRunPending(true);
    setError(null);
    void terminateBackendTask(headerRuntimeTaskId)
      .then(async (response) => {
        if (response.terminated !== true) {
          throw new Error(`Backend did not confirm ${actionToken} for task "${headerRuntimeTaskId}".`);
        }
        await loadProject();
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : `Failed to ${actionToken} running task.`);
      })
      .finally(() => {
        setHeaderStopRunPending(false);
      });
  };

  const applyTaskDetailCopilotAction = useCallback(async (action: CopilotPlanAction) => {
    const patch = asRecord(action.payload?.parameterPatch);
    const applyMetadataPatch = async () => {
      const metadataPatch = asRecord(action.payload?.metadataPatch);
      const hasNamePatch = Object.prototype.hasOwnProperty.call(metadataPatch, 'taskName');
      const hasSummaryPatch = Object.prototype.hasOwnProperty.call(metadataPatch, 'taskSummary');
      const nextName = readText(metadataPatch.taskName).trim();
      const nextSummary = normalizeTaskSummary(readText(metadataPatch.taskSummary));
      if (!hasNamePatch && !hasSummaryPatch) throw new Error('No task name or description update was provided.');
      if (hasNamePatch && !nextName) throw new Error('Task name cannot be empty.');
      if (!canEdit) throw new Error('This project is read-only for your account.');

      const taskRow = activeResultTask || statusContextTaskRow;
      if (!taskRow?.id) throw new Error('No current task to update.');

      if (hasNamePatch) handleTaskNameChange(nextName);
      if (hasSummaryPatch) handleTaskSummaryChange(nextSummary);

      const payload: Partial<ProjectTask> = {};
      if (hasNamePatch) payload.name = nextName;
      if (hasSummaryPatch) payload.summary = nextSummary;
      await patchTask(taskRow.id, payload);
    };
    const applyPatch = () => {
      const seed = readFiniteNumber(patch.seed);
      if (seed !== null) handleRuntimeSeedChange(Math.floor(seed));
      const backendPatch = readText(patch.backend).trim().toLowerCase();
      const backendPatchAllowed = isVirtualScreeningWorkflow
        ? backendPatch === 'nesso'
        : backendPatch === 'boltz' ||
        backendPatch === 'alphafold3' ||
        backendPatch === 'protenix' ||
        backendPatch === 'boltz2dock' ||
        backendPatch === 'protenix2dock';
      if (backendPatchAllowed) {
        handleRuntimeBackendChange(backendPatch);
      }
      const affinityModePatch = readText(patch.affinityMode).trim();
      if (affinityModePatch === 'score' || affinityModePatch === 'pose' || affinityModePatch === 'refine' || affinityModePatch === 'interface' || affinityModePatch === 'dock') {
        onAffinityModeChange(affinityModePatch);
      }
      const affinityBinding = asRecord(patch.affinityBinding);
      if (affinityBinding && typeof affinityBinding.enabled === 'boolean') {
        // Enable/disable binding computation on the prediction task (the "Binding / Compute" checkbox).
        const wantEnabled = affinityBinding.enabled === true;
        if (wantEnabled && !canEnableAffinityFromWorkspace) {
          throw new Error('Cannot enable affinity: the task needs at least two components (a receptor and a ligand) before binding can be computed.');
        }
        setAffinityEnabledFromWorkspace(wantEnabled);
        if (wantEnabled) {
          // Chain-ID assignment (target/ligand/binder) goes directly into the draft properties.
          const targetChain = readText(affinityBinding.target).trim();
          const ligandChain = readText(affinityBinding.ligand || affinityBinding.binder).trim();
          setDraft((prev) =>
            prev
              ? {
                  ...prev,
                  inputConfig: {
                    ...prev.inputConfig,
                    properties: {
                      ...prev.inputConfig.properties,
                      ...(targetChain ? { target: targetChain } : {}),
                      ...(ligandChain ? { ligand: ligandChain, binder: ligandChain } : {}),
                    },
                  },
                }
              : prev
          );
        }
      }
      if (typeof patch.lowVram === 'boolean') {
        handleRuntimeLowVramChange(patch.lowVram);
      }
      const peptideDesignMode = readText(patch.peptideDesignMode).trim();
      if (peptideDesignMode === 'linear' || peptideDesignMode === 'cyclic' || peptideDesignMode === 'bicyclic') {
        handleRuntimePeptideDesignModeChange(peptideDesignMode);
      }
      const peptideBinderLength = readFiniteNumber(patch.peptideBinderLength);
      if (peptideBinderLength !== null) {
        handleRuntimePeptideBinderLengthChange(Math.max(1, Math.floor(peptideBinderLength)));
      }
      const peptideIterations = readFiniteNumber(patch.peptideIterations);
      if (peptideIterations !== null) handleRuntimePeptideIterationsChange(Math.max(1, Math.floor(peptideIterations)));
      const peptidePopulationSize = readFiniteNumber(patch.peptidePopulationSize);
      if (peptidePopulationSize !== null) handleRuntimePeptidePopulationSizeChange(Math.max(1, Math.floor(peptidePopulationSize)));
      const peptideEliteSize = readFiniteNumber(patch.peptideEliteSize);
      if (peptideEliteSize !== null) handleRuntimePeptideEliteSizeChange(Math.max(1, Math.floor(peptideEliteSize)));
      if (typeof patch.peptideUseInitialSequence === 'boolean') {
        handleRuntimePeptideUseInitialSequenceChange(patch.peptideUseInitialSequence);
      }
      const peptideInitialSequence = readText(patch.peptideInitialSequence).trim();
      if (peptideInitialSequence) handleRuntimePeptideInitialSequenceChange(peptideInitialSequence);
      const peptideSequenceMask = readText(patch.peptideSequenceMask).trim();
      if (peptideSequenceMask) handleRuntimePeptideSequenceMaskChange(peptideSequenceMask);
      const peptideChirality = readText(patch.peptideChirality).trim().toLowerCase();
      if (peptideChirality === 'l' || peptideChirality === 'd') {
        handleRuntimePeptideChiralityChange(peptideChirality as 'l' | 'd');
      }
      const peptideBicyclicLinkerCcd = readText(patch.peptideBicyclicLinkerCcd).trim();
      if (peptideBicyclicLinkerCcd === 'SEZ' || peptideBicyclicLinkerCcd === '29N' || peptideBicyclicLinkerCcd === 'BS3') {
        handleRuntimePeptideBicyclicLinkerCcdChange(peptideBicyclicLinkerCcd);
      }
      const peptideBicyclicCysPositionMode = readText(patch.peptideBicyclicCysPositionMode).trim();
      if (peptideBicyclicCysPositionMode === 'auto' || peptideBicyclicCysPositionMode === 'manual') {
        handleRuntimePeptideBicyclicCysPositionModeChange(peptideBicyclicCysPositionMode);
      }
      if (typeof patch.peptideBicyclicFixTerminalCys === 'boolean') {
        handleRuntimePeptideBicyclicFixTerminalCysChange(patch.peptideBicyclicFixTerminalCys);
      }
      if (typeof patch.peptideBicyclicIncludeExtraCys === 'boolean') {
        handleRuntimePeptideBicyclicIncludeExtraCysChange(patch.peptideBicyclicIncludeExtraCys);
      }
      const peptideBicyclicCys1Pos = readFiniteNumber(patch.peptideBicyclicCys1Pos);
      if (peptideBicyclicCys1Pos !== null) handleRuntimePeptideBicyclicCys1PosChange(Math.max(1, Math.floor(peptideBicyclicCys1Pos)));
      const peptideBicyclicCys2Pos = readFiniteNumber(patch.peptideBicyclicCys2Pos);
      if (peptideBicyclicCys2Pos !== null) handleRuntimePeptideBicyclicCys2PosChange(Math.max(1, Math.floor(peptideBicyclicCys2Pos)));
      const peptideBicyclicCys3Pos = readFiniteNumber(patch.peptideBicyclicCys3Pos);
      if (peptideBicyclicCys3Pos !== null) handleRuntimePeptideBicyclicCys3PosChange(Math.max(1, Math.floor(peptideBicyclicCys3Pos)));
      const componentsReplacement = asRecord(patch.componentsReplacement);
      const replacementComponentsRaw = componentsReplacement.components;
      if (Array.isArray(replacementComponentsRaw)) {
        const replacementComponents = replacementComponentsRaw
          .map((component, index) => {
            if (!component || typeof component !== 'object') return null;
            const raw = component as Record<string, unknown>;
            const type = readText(raw.type).trim();
            const sequence = readText(raw.sequence).trim();
            if (!type || !sequence) return null;
            // Fill defaults the InputComponent type requires but the planner may omit.
            // Ligands with a SMILES sequence default to inputMethod 'smiles' (the sequence IS the
            // SMILES string); proteins default useMsa to false.
            const isLigand = type === 'ligand';
            const inputMethod = readText(raw.inputMethod).trim() || (isLigand ? 'smiles' : undefined);
            return {
              id: readText(raw.id).trim() || `copilot-${index + 1}`,
              type: type as InputComponent['type'],
              sequence,
              numCopies: Math.max(1, Math.floor(Number(raw.numCopies)) || 1),
              ...(typeof raw.useMsa === 'boolean' ? { useMsa: raw.useMsa } : (!isLigand ? { useMsa: false } : {})),
              ...(typeof raw.cyclic === 'boolean' ? { cyclic: raw.cyclic } : {}),
              ...(inputMethod ? { inputMethod: inputMethod as InputComponent['inputMethod'] } : {}),
            } as InputComponent;
          })
          .filter((component): component is InputComponent => component !== null);
        if (replacementComponents.length > 0) {
          setDraft((prev) =>
            prev
              ? {
                  ...prev,
                  inputConfig: {
                    ...prev.inputConfig,
                    version: 1,
                    components: replacementComponents,
                    constraints: componentsReplacement.clearConstraints === false ? prev.inputConfig.constraints : []
                  }
                }
              : prev
          );
        }
      }
    };
    if (action.id === 'task_detail:create_new_task') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const components = Array.isArray(action.payload?.components) ? action.payload.components : [];
      if (components.length === 0) throw new Error('No components were provided for the new task.');
      const taskName = readText(action.payload?.taskName).trim();
      const taskSummary = readText(action.payload?.taskSummary).trim();
      const params = new URLSearchParams();
      params.set('tab', 'components');
      params.set('new_task', '1');
      params.set('copilot_components', JSON.stringify(components));
      if (taskName) params.set('copilot_task_name', taskName);
      if (taskSummary) params.set('copilot_task_summary', taskSummary);
      navigate(`/projects/${project.id}?${params.toString()}`);
      return 'New task draft created with the provided components.';
    }
    if (action.id === 'task_detail:apply_parameter_patch') {
      applyPatch();
      // Wait for React to commit the setDraft state updates, then save via saveDraftRef.current —
      // the ref points at the saveDraft of the render that committed the PATCHED draft, so its
      // closure reads the patched values (not the stale pre-patch draft). The double-rAF flush
      // makes the render commit; the ref makes the save read the new render's closure.
      await new Promise<void>((resolve) => {
        requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
      });
      await saveDraftRef.current();
      return 'Parameters updated and draft saved.';
    }
    if (action.id === 'task_detail:apply_metadata_patch') {
      await applyMetadataPatch();
      return 'Task metadata updated.';
    }
    if (action.id === 'task_detail:apply_structure_template') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!isPredictionWorkflow) throw new Error('Structure templates are only supported for prediction tasks.');
      // Identifier-first: the host builds the guaranteed-valid mmCIF URL from the entry id.
      const structurePdbId = String(action.payload?.structurePdbId || '').trim();
      const structureUrl = structurePdbId
        ? rcsbCifUrl(structurePdbId)
        : String(action.payload?.structureUrl || '').trim();
      if (!structureUrl) {
        throw new Error('No structure was provided — pass the chosen entry\'s structurePdbId (preferred) or a cifUrl returned by a lookup.');
      }
      const templateFileName = structurePdbId
        ? (String(action.payload?.fileName || '').trim() || `${structurePdbId.toUpperCase()}.cif`)
        : action.payload?.fileName;
      const targetProteinComponent = (draft.inputConfig?.components || []).find((component) => component.type === 'protein') || null;
      if (!targetProteinComponent) throw new Error('This prediction task has no protein component to attach a template to.');
      const { fileName, format, contentText } = await fetchValidatedStructure(structureUrl, templateFileName);
      const chainSequences = extractProteinChainSequences(contentText, format);
      const chainIds = Object.keys(chainSequences).sort((a, b) => a.localeCompare(b));
      if (!chainIds.length) throw new Error('No protein chain could be parsed from the fetched structure.');
      const upload: ProteinTemplateUpload = {
        fileName,
        format,
        content: contentText,
        chainId: chainIds[0],
        chainSequences
      };
      setProteinTemplates((prev) => ({ ...prev, [targetProteinComponent.id]: upload }));
      setWorkspaceTab('components');
      return `Structure template applied to component "${targetProteinComponent.id}" (chain ${chainIds[0]}). Switched to the Components tab.`;
    }
    if (action.id === 'task_detail:apply_docking_target_structure') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!isAffinityWorkflow) throw new Error('Docking target structures are only supported for docking tasks.');
      // Identifier-first: the host builds the guaranteed-valid mmCIF URL from the entry id.
      const structurePdbId = String(action.payload?.structurePdbId || '').trim();
      const structureUrl = structurePdbId
        ? rcsbCifUrl(structurePdbId)
        : String(action.payload?.structureUrl || '').trim();
      if (!structureUrl) {
        throw new Error('No docking target was provided — pass the chosen entry\'s structurePdbId (preferred) or a cifUrl returned by a lookup.');
      }
      const targetFileName = structurePdbId
        ? (String(action.payload?.fileName || '').trim() || `${structurePdbId.toUpperCase()}.cif`)
        : action.payload?.fileName;
      const { fileName, format, contentText } = await fetchValidatedStructure(structureUrl, targetFileName);
      const file = new File([contentText], fileName, { type: format === 'pdb' ? 'chemical/x-pdb' : 'chemical/x-cif' });
      // onTargetFileChange resets ligand-dependent state INCLUDING the SMILES the
      // user may have just had copilot fill in — a target swap must not discard an
      // independently-valid ligand SMILES, so capture and restore it.
      const ligandSmilesBefore = String(affinityLigandSmiles || '').trim();
      onAffinityTargetFileChange(file);
      if (ligandSmilesBefore) setAffinityLigandSmiles(ligandSmilesBefore);
      // The target viewer + preview pipeline only live on the Components tab; without
      // this switch the apply is invisible (the exact "applied but nothing loaded" bug).
      setWorkspaceTab('components');
      return 'Docking target structure applied. Switched to the Components tab — the 3D preview is being prepared.';
    }
    if (action.id === 'task_detail:apply_docking_ligand_smiles') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!isAffinityWorkflow) throw new Error('Docking ligand SMILES are only supported for docking tasks.');
      const smiles = String(action.payload?.smiles || '').trim();
      if (!smiles) throw new Error('No SMILES was provided.');
      // The SMILES is only consumed in dock mode; in pose/refine/interface the submit validation
      // requires an uploaded ligand file and this value would be silently ignored.
      const modeSwitched = affinityMode !== 'dock';
      if (modeSwitched) onAffinityModeChange('dock');
      setAffinityLigandSmiles(smiles);
      setWorkspaceTab('components');
      return modeSwitched
        ? 'Ligand SMILES set (mode switched to dock). Switched to the Components tab.'
        : 'Ligand SMILES set. Switched to the Components tab.';
    }
    if (action.id === 'task_detail:save_draft') {
      await saveDraft();
      return 'Draft saved.';
    }
    if (action.id === 'task_detail:set_docking_pocket_box') {
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!isAffinityWorkflow) throw new Error('Docking pocket boxes are only supported for docking tasks.');
      const mode = String(action.payload?.mode || 'auto').trim();
      if (!affinityTargetFile) {
        throw new Error('No target structure is uploaded yet — apply the docking target first.');
      }
      const structureText = await affinityTargetFile.text();
      // Name first, content sniff second (shared policy): a target applied without a
      // recognizable extension must still box instead of failing the whole docking chain
      // on its file name.
      const format = resolveStructureFormat(affinityTargetFile.name, structureText);
      if (!format) throw new Error('The uploaded target file is not a .pdb, .ent, .cif or .mmcif structure.');
      if (mode !== 'auto' && mode !== 'protein') {
        throw new Error('mode must be "auto" (ligand pocket if present, else whole protein) or "protein".');
      }
      // "auto": co-crystallized ligand pocket first, whole protein as fallback.
      // "protein": whole-protein box explicitly (strip heteroatoms so a co-crystal ligand
      // cannot pull the box off-center).
      const chosen = computeAutoPocketBox(
        mode === 'protein' ? structureText.replace(/^HETATM.*$/gm, '') : structureText,
        format
      );
      if (!chosen) throw new Error('No atoms could be parsed from the target structure.');
      onAffinityDockPocketChange({
        centerX: chosen.box.centerX,
        centerY: chosen.box.centerY,
        centerZ: chosen.box.centerZ,
        sizeX: chosen.box.sizeX,
        sizeY: chosen.box.sizeY,
        sizeZ: chosen.box.sizeZ,
        method: chosen.method
      });
      setWorkspaceTab('components');
      return chosen.ligandLabel
        ? `Pocket box set around the co-crystallized ligand ${chosen.ligandLabel} (generous size).`
        : 'Pocket box set to the whole protein (large box, blind docking).';
    }
    if (action.id === 'task_detail:submit_current') {
      if (runDisabledRef.current) {
        throw new Error(runBlockedReasonRef.current || 'Current task cannot be submitted yet.');
      }
      // Honest precondition (pi: actionable errors at the decision point): submit silently
      // no-ops without a pocket in dock mode — surface it HERE with the fix, so the receipt
      // is a failed one carrying next steps instead of a false "queued".
      if (isAffinityWorkflow && affinityMode === 'dock' && !affinityDockPocket) {
        throw new Error(
          'Dock mode requires a pocket box — apply task_detail:set_docking_pocket_box (mode "auto") first: it boxes the co-crystallized ligand site or the whole protein.'
        );
      }
      await submitTaskRef.current();
      return 'Task submitted. The task is now queued.';
    }
    if (action.id === 'task_detail:cancel_current') {
      const taskRow = statusContextTaskRow || activeResultTask;
      const runtimeTaskId = String(taskRow?.task_id || headerRuntimeTaskId || '').trim();
      const runtimeState = String(taskRow?.task_state || displayTaskState || '').trim().toUpperCase();
      if (!canEdit) throw new Error('This project is read-only for your account.');
      if (!runtimeTaskId) throw new Error('No active runtime task_id is available to cancel.');
      if (runtimeState !== 'QUEUED' && runtimeState !== 'RUNNING') {
        throw new Error('Current task is not running or queued.');
      }
      const response = await terminateBackendTask(runtimeTaskId);
      if (response.terminated !== true) {
        throw new Error(`Backend did not confirm cancellation for task "${runtimeTaskId}".`);
      }
      await loadProject();
      return 'Task cancelled.';
    }
    if (action.id === 'task_detail:delete_current') {
      const taskRow = activeResultTask || statusContextTaskRow;
      if (!taskRow) throw new Error('No current task to delete.');
      if (!canEdit) throw new Error('This project is read-only for your account.');
      const runtimeTaskId = String(taskRow.task_id || '').trim();
      const runtimeState = String(taskRow.task_state || displayTaskState || '').trim().toUpperCase();
      if ((runtimeState === 'QUEUED' || runtimeState === 'RUNNING') && !runtimeTaskId) {
        throw new Error('Task is active but task_id is missing; deletion is blocked to avoid orphan runtime.');
      }
      if (runtimeState === 'QUEUED' || runtimeState === 'RUNNING') {
        const response = await terminateBackendTask(runtimeTaskId);
        if (response.terminated !== true) {
          throw new Error(`Backend did not confirm cancellation for task "${runtimeTaskId}".`);
        }
      }
      await deleteProjectTask(taskRow.id);
      await loadProject();
      navigate(`/projects/${project.id}/tasks`, { replace: true });
      return 'Task deleted.';
    }
    // An unrecognized task-detail action must fail loudly: returning undefined would make the
    // Copilot panel record an `applied` receipt for a silent no-op.
    throw new Error(`Unsupported Copilot task-detail action: ${action.id}`);
  }, [
    affinityDockPocket,
    affinityLigandSmiles,
    affinityMode,
    affinityTargetFile,
    activeResultTask,
    canEdit,
    displayTaskState,
    draft,
    effectiveRunBlockedReason,
    effectiveRunDisabled,
    isAffinityWorkflow,
    isPredictionWorkflow,
    onAffinityTargetFileChange,
    setAffinityLigandSmiles,
    setProteinTemplates,
    setWorkspaceTab,
    handleHeaderRunAction,
    handleRuntimePeptideBinderLengthChange,
    handleRuntimeBackendChange,
    handleRuntimePeptideBicyclicCys1PosChange,
    handleRuntimePeptideBicyclicCys2PosChange,
    handleRuntimePeptideBicyclicCys3PosChange,
    handleRuntimePeptideBicyclicCysPositionModeChange,
    handleRuntimePeptideBicyclicFixTerminalCysChange,
    handleRuntimePeptideBicyclicIncludeExtraCysChange,
    handleRuntimePeptideBicyclicLinkerCcdChange,
    handleRuntimePeptideDesignModeChange,
    handleRuntimePeptideEliteSizeChange,
    handleRuntimePeptideInitialSequenceChange,
    handleRuntimePeptideIterationsChange,
    handleRuntimePeptideResiduePoolChange,
    handleRuntimePeptideNonNaturalRangeChange,
    handleRuntimePeptidePopulationSizeChange,
    handleRuntimePeptideSequenceMaskChange,
    handleRuntimePeptideUseInitialSequenceChange,
    handleRuntimeSeedChange,
    handleTaskNameChange,
    handleTaskSummaryChange,
    headerRuntimeTaskId,
    isVirtualScreeningWorkflow,
    navigate,
    onAffinityModeChange,
    patchTask,
    project.id,
    loadProject,
    saveDraft,
    setError,
    statusContextTaskRow
  ]);

  const copilotContextPayload = useMemo(() => {
    const statusTaskRowId = readText(statusContextTaskRow?.id).trim();
    const statusTaskId = readText(statusContextTaskRow?.task_id).trim();
    const statusTaskState = readText(statusContextTaskRow?.task_state).trim();
    const activeResultTaskRowId = readText(activeResultTask?.id).trim();
    const activeResultTaskId = readText(activeResultTask?.task_id).trim();
    const activeResultTaskState = readText(activeResultTask?.task_state).trim();
    return {
      page: {
        contextType: 'task_detail',
        workflowKey: workflow.key,
        workflowTitle: workflow.title,
        workflowShortTitle: workflow.shortTitle,
        runLabel: workflow.runLabel,
        supportsSequenceInputs: workflow.supportsSequenceInputs,
        availableActions: [
          'analyze_current_context',
          'plan_confirmed_parameter_patch',
          'plan_confirmed_submit',
          'plan_confirmed_cancel_current_task',
          'plan_confirmed_delete_current_task',
          'plan_confirmed_metadata_update',
          'apply_copilot_uploaded_files_when_supported'
        ]
      },
      project: { id: project.id, name: project.name, task_type: project.task_type, workflow_key: workflow.key },
      draft: {
        taskName: draft.taskName,
        taskSummary: draft.taskSummary,
        backend: draft.backend,
        options: draft.inputConfig?.options,
        components: summarizeCopilotComponents(draft.inputConfig?.components),
        constraints: summarizeCopilotConstraints(draft.inputConfig?.constraints)
      },
      runtime: {
        displayTaskState,
        runDisabled: effectiveRunDisabled,
        runBlockedReason: effectiveRunBlockedReason,
        activeTaskId: headerRuntimeTaskId,
        statusTaskRowId,
        statusTaskId,
        statusTaskState,
        activeResultTaskRowId,
        activeResultTaskId,
        activeResultTaskState,
        authoritativeTaskState:
          readText(displayTaskState).trim() ||
          statusTaskState ||
          activeResultTaskState ||
          readText(project.task_state).trim()
      },
      affinityUploads: isAffinityWorkflow
        ? {
            // PERSISTED uploads win over the transient File: the File hydrates only on the
            // Components tab, so a truth-lie here made the planner re-apply an existing target.
            targetFileName:
              runtime.affinityCurrentUploads?.target?.fileName || affinityTargetFile?.name || '',
            ligandFileName:
              runtime.affinityCurrentUploads?.ligand?.fileName || affinityLigandFile?.name || '',
            targetUploaded: Boolean(runtime.affinityCurrentUploads?.target || affinityTargetFile),
            ligandUploaded: Boolean(runtime.affinityCurrentUploads?.ligand || affinityLigandFile)
          }
        : undefined,
      currentTask: summarizeCopilotTask(statusContextTaskRow || activeResultTask || null)
    };
  }, [
    activeResultTask,
    affinityLigandFile,
    affinityTargetFile,
    displayTaskState,
    draft.inputConfig?.components,
    draft.inputConfig?.constraints,
    draft.inputConfig?.options,
    draft.backend,
    draft.taskName,
    draft.taskSummary,
    effectiveRunBlockedReason,
    effectiveRunDisabled,
    headerRuntimeTaskId,
    isAffinityWorkflow,
    project.id,
    project.name,
    project.task_state,
    project.task_type,
    statusContextTaskRow,
    workflow.key,
    workflow.runLabel,
    workflow.shortTitle,
    workflow.supportsSequenceInputs,
    workflow.title
  ]);

  const defaultDownloadTaskId = useMemo(() => {
    const viewerTaskId = readText(structureTaskId).trim();
    if (viewerTaskId) return viewerTaskId;
    const activeTaskId = readText(activeResultTask?.task_id).trim();
    const activeStructureName = readText(activeResultTask?.structure_name).trim();
    if (activeStructureName && activeTaskId) return activeTaskId;
    return readText(project.task_id).trim();
  }, [activeResultTask?.structure_name, activeResultTask?.task_id, project.task_id, structureTaskId]);

  return (
    <>
    <ProjectDetailLayout
      projectName={project.name}
      canDownloadResult={Boolean(
        defaultDownloadTaskId
      )}
      workflow={{
        shortTitle: workflow.shortTitle,
        runLabel: workflow.runLabel,
        description: workflow.description
      }}
      workspaceTab={workspaceTab}
      componentStepLabel={componentStepLabel}
      taskName={draft.taskName}
      taskSummary={draft.taskSummary}
      isPredictionWorkflow={isPredictionWorkflow}
      isVirtualScreeningWorkflow={isVirtualScreeningWorkflow}
      isAffinityWorkflow={isAffinityWorkflow}
      isLeadOptimizationWorkflow={isLeadOptimizationWorkflow}
      constraintsSupported={allowedConstraintTypes.length > 0}
      displayTaskState={displayTaskState}
      isActiveRuntime={isActiveRuntime}
      progressPercent={progressPercent}
      statusSubmittedAt={displaySubmittedAt}
      totalRuntimeSeconds={totalRuntimeSeconds}
      canEdit={canEdit}
      loading={loading}
      saving={saving}
      submitting={submitting}
      runSubmitting={runSubmitting}
      hasUnsavedChanges={hasUnsavedChanges}
      runMenuOpen={runMenuOpen}
      runDisabled={effectiveRunDisabled}
      runBlockedReason={effectiveRunBlockedReason}
      isRunRedirecting={isRunRedirecting}
      canOpenRunMenu={canOpenRunMenu}
      showHeaderRunAction
      showStopAction={showHeaderStopAction}
      stopSubmitting={headerStopRunPending}
      stopDisabled={headerStopRunDisabled}
      stopTitle={headerStopRunTitle}
      showQuickRunFab={showQuickRunFab}
      taskHistoryPath={taskHistoryPath}
      runSuccessNotice={runSuccessNotice}
      error={error}
      resultError={resultError}
      affinityPreviewError={affinityPreviewError}
      resultChainConsistencyWarning={resultChainConsistencyWarning}
      projectResultsSectionProps={projectResultsSectionProps}
      affinitySectionProps={affinityWorkflowSectionProps}
      leadOptimizationSectionProps={leadOptimizationWorkflowSectionProps}
      predictionSectionProps={predictionWorkflowSectionProps}
      virtualScreeningSectionProps={virtualScreeningWorkflowSectionProps}
      runtimeSettingsProps={workflowRuntimeSettingsSectionProps}
      runActionRef={runActionRef as RefObject<HTMLDivElement>}
      topRunButtonRef={topRunButtonRef as RefObject<HTMLButtonElement>}
      onOpenTaskHistory={handleOpenTaskHistory}
      onDownloadResult={() => {
        setError(null);
        if (!defaultDownloadTaskId) return;
        void downloadResultFile(defaultDownloadTaskId).catch((err) => {
          setError(err instanceof Error ? err.message : 'Failed to download result archive.');
        });
      }}
      onSaveDraft={() => {
        void saveDraft();
      }}
      onReset={handleResetFromHeader}
      onRunAction={handleHeaderRunAction}
      onStopAction={handleHeaderStopAction}
      onRestoreSavedDraft={handleRestoreSavedDraft}
      onRunCurrentDraft={handleRunCurrentDraft}
      onWorkspaceTabChange={setWorkspaceTab}
      onTaskNameChange={handleTaskNameChange}
      onTaskSummaryChange={handleTaskSummaryChange}
      onWorkspaceFormSubmit={handleWorkspaceFormSubmit}
    />
    {copilotAvailable && session?.userId ? (
      <ProjectCopilotModal
        open={copilotOpen}
        title="Copilot"
        subtitle={`${workflow.shortTitle} · ${project.name}`}
        contextType="task_detail"
        projectId={project.id}
        projectTaskId={readText((activeResultTask || statusContextTaskRow)?.id).trim() || null}
        currentUserId={session.userId}
        currentUsername={session.username}
        contextPayload={copilotContextPayload}
        onApplyPlanAction={applyTaskDetailCopilotAction}
        onSendAttachments={handleCopilotAttachments}
        onOpen={() => setCopilotOpen(true)}
        onClose={() => setCopilotOpen(false)}
      />
    ) : null}
    </>
  );
}
