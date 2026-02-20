import { useRef, useState } from 'react';
import type { InputComponent, Project, ProjectTask, ProteinTemplateUpload } from '../../types/models';
import type { ConstraintResiduePick } from '../../components/project/ConstraintEditor';
import { useTaskAttachmentCache } from './useTaskAttachmentCache';
import { useProjectPaneLayouts } from './useProjectPaneLayouts';
import type { ProjectWorkspaceDraft, WorkspaceTab } from './workspaceTypes';

export function useProjectDetailLocalState() {
  const [project, setProject] = useState<Project | null>(null);
  const [projectTasks, setProjectTasks] = useState<ProjectTask[]>([]);
  const [draft, setDraft] = useState<ProjectWorkspaceDraft | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);
  const [runRedirectTaskId, setRunRedirectTaskId] = useState<string | null>(null);
  const [runSuccessNotice, setRunSuccessNotice] = useState<string | null>(null);
  const [showFloatingRunButton, setShowFloatingRunButton] = useState(false);
  const [structureText, setStructureText] = useState('');
  const [structureFormat, setStructureFormat] = useState<'cif' | 'pdb'>('cif');
  const [structureTaskId, setStructureTaskId] = useState<string | null>(null);
  const [statusInfo, setStatusInfo] = useState<Record<string, unknown> | null>(null);
  const [nowTs, setNowTs] = useState(Date.now());
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('results');
  const [savedDraftFingerprint, setSavedDraftFingerprint] = useState('');
  const [savedComputationFingerprint, setSavedComputationFingerprint] = useState('');
  const [savedTemplateFingerprint, setSavedTemplateFingerprint] = useState('');
  const [runMenuOpen, setRunMenuOpen] = useState(false);
  const [proteinTemplates, setProteinTemplates] = useState<Record<string, ProteinTemplateUpload>>({});
  const {
    taskProteinTemplates,
    setTaskProteinTemplates,
    taskAffinityUploads,
    setTaskAffinityUploads,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow
  } = useTaskAttachmentCache();
  const [pickedResidue, setPickedResidue] = useState<ConstraintResiduePick | null>(null);
  const [activeConstraintId, setActiveConstraintId] = useState<string | null>(null);
  const [selectedContactConstraintIds, setSelectedContactConstraintIds] = useState<string[]>([]);
  const [selectedConstraintTemplateComponentId, setSelectedConstraintTemplateComponentId] = useState<string | null>(null);
  const [constraintPickModeEnabled, setConstraintPickModeEnabled] = useState(false);
  const constraintPickSlotRef = useRef<Record<string, 'first' | 'second'>>({});
  const constraintSelectionAnchorRef = useRef<string | null>(null);
  const statusRefreshInFlightRef = useRef<Set<string>>(new Set());
  const submitInFlightRef = useRef(false);
  const runRedirectTimerRef = useRef<number | null>(null);
  const runSuccessNoticeTimerRef = useRef<number | null>(null);
  const runActionRef = useRef<HTMLDivElement | null>(null);
  const topRunButtonRef = useRef<HTMLButtonElement | null>(null);
  const [activeComponentId, setActiveComponentId] = useState<string | null>(null);
  const [sidebarTypeOpen, setSidebarTypeOpen] = useState<Record<InputComponent['type'], boolean>>({
    protein: true,
    ligand: false,
    dna: false,
    rna: false
  });
  const [sidebarConstraintsOpen, setSidebarConstraintsOpen] = useState(true);
  const paneLayouts = useProjectPaneLayouts();

  return {
    project,
    setProject,
    projectTasks,
    setProjectTasks,
    draft,
    setDraft,
    loading,
    setLoading,
    saving,
    setSaving,
    submitting,
    setSubmitting,
    error,
    setError,
    resultError,
    setResultError,
    runRedirectTaskId,
    setRunRedirectTaskId,
    runSuccessNotice,
    setRunSuccessNotice,
    showFloatingRunButton,
    setShowFloatingRunButton,
    structureText,
    setStructureText,
    structureFormat,
    setStructureFormat,
    structureTaskId,
    setStructureTaskId,
    statusInfo,
    setStatusInfo,
    nowTs,
    setNowTs,
    workspaceTab,
    setWorkspaceTab,
    savedDraftFingerprint,
    setSavedDraftFingerprint,
    savedComputationFingerprint,
    setSavedComputationFingerprint,
    savedTemplateFingerprint,
    setSavedTemplateFingerprint,
    runMenuOpen,
    setRunMenuOpen,
    proteinTemplates,
    setProteinTemplates,
    taskProteinTemplates,
    setTaskProteinTemplates,
    taskAffinityUploads,
    setTaskAffinityUploads,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow,
    pickedResidue,
    setPickedResidue,
    activeConstraintId,
    setActiveConstraintId,
    selectedContactConstraintIds,
    setSelectedContactConstraintIds,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId,
    constraintPickModeEnabled,
    setConstraintPickModeEnabled,
    constraintPickSlotRef,
    constraintSelectionAnchorRef,
    statusRefreshInFlightRef,
    submitInFlightRef,
    runRedirectTimerRef,
    runSuccessNoticeTimerRef,
    runActionRef,
    topRunButtonRef,
    activeComponentId,
    setActiveComponentId,
    sidebarTypeOpen,
    setSidebarTypeOpen,
    sidebarConstraintsOpen,
    setSidebarConstraintsOpen,
    ...paneLayouts
  };
}
