import { useEffect, useRef } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { Project, ProteinTemplateUpload, TaskState } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import { saveProjectUiState } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';
import type { WorkspaceTab } from './workspaceTypes';

interface UseProjectWorkspaceRuntimeUiOptions {
  project: Project | null;
  workspaceTab: WorkspaceTab;
  setWorkspaceTab: Dispatch<SetStateAction<WorkspaceTab>>;
  setNowTs: Dispatch<SetStateAction<number>>;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  taskProteinTemplates: Record<string, Record<string, ProteinTemplateUpload>>;
  taskAffinityUploads: Record<string, AffinityPersistedUploads>;
  activeConstraintId: string | null;
  selectedConstraintTemplateComponentId: string | null;
}

export function useProjectWorkspaceRuntimeUi({
  project,
  workspaceTab,
  setWorkspaceTab,
  setNowTs,
  proteinTemplates,
  taskProteinTemplates,
  taskAffinityUploads,
  activeConstraintId,
  selectedConstraintTemplateComponentId,
}: UseProjectWorkspaceRuntimeUiOptions): void {
  const prevTaskStateRef = useRef<TaskState | null>(null);

  useEffect(() => {
    if (!project) return;
    const workflowDef = getWorkflowDefinition(project.task_type);
    const allowsComponentsTab =
      workflowDef.key === 'prediction' || workflowDef.key === 'affinity' || workflowDef.key === 'lead_optimization';
    const allowsConstraintsTab = workflowDef.key === 'prediction' || workflowDef.key === 'lead_optimization';
    if (
      (!allowsComponentsTab && workspaceTab === 'components') ||
      (!allowsConstraintsTab && workspaceTab === 'constraints')
    ) {
      setWorkspaceTab('basics');
    }
  }, [project, workspaceTab, setWorkspaceTab]);

  useEffect(() => {
    if (!project) return;
    const prev = prevTaskStateRef.current;
    const next = project.task_state;
    if (prev && prev !== next && next === 'SUCCESS') {
      setWorkspaceTab('results');
    }
    prevTaskStateRef.current = next;
  }, [project, setWorkspaceTab]);

  useEffect(() => {
    if (!project) return;
    if (!['QUEUED', 'RUNNING'].includes(project.task_state)) return;

    const timer = setInterval(() => {
      setNowTs(Date.now());
    }, 1000);

    return () => clearInterval(timer);
  }, [project, setNowTs]);

  useEffect(() => {
    if (!project) return;
    saveProjectUiState(project.id, {
      proteinTemplates,
      taskProteinTemplates,
      taskAffinityUploads,
      activeConstraintId,
      selectedConstraintTemplateComponentId
    });
  }, [
    project,
    proteinTemplates,
    taskProteinTemplates,
    taskAffinityUploads,
    activeConstraintId,
    selectedConstraintTemplateComponentId,
  ]);
}
