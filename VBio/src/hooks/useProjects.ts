import { useCallback, useEffect, useState } from 'react';
import type { Project, Session } from '../types/models';
import { insertProject, listProjects, updateProject } from '../api/supabaseLite';
import { removeProjectInputConfig, removeProjectUiState } from '../utils/projectInputs';
import { normalizeWorkflowKey } from '../utils/workflows';

const DEFAULT_TASK_TYPE = 'prediction';

export interface CreateProjectInput {
  name: string;
  summary?: string;
  taskType?: string;
  backend?: string;
  useMsa?: boolean;
  proteinSequence?: string;
  ligandSmiles?: string;
}

export function useProjects(session: Session | null) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  const load = useCallback(async () => {
    if (!session) {
      setProjects([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const rows = await listProjects({
        userId: session.userId,
        search
      });
      setProjects(rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load projects.');
    } finally {
      setLoading(false);
    }
  }, [session, search]);

  useEffect(() => {
    void load();
  }, [load]);

  const createProject = useCallback(
    async (input: CreateProjectInput) => {
      if (!session) throw new Error('You must sign in first.');
      const workflow = normalizeWorkflowKey(input.taskType);
      const created = await insertProject({
        user_id: session.userId,
        name: input.name.trim(),
        summary: input.summary?.trim() || '',
        backend: input.backend || 'boltz',
        use_msa: input.useMsa ?? true,
        protein_sequence: input.proteinSequence || '',
        ligand_smiles: input.ligandSmiles || '',
        color_mode: 'white',
        task_type: workflow || DEFAULT_TASK_TYPE,
        task_id: '',
        task_state: 'DRAFT',
        status_text: 'Ready for input',
        error_text: '',
        confidence: {},
        affinity: {},
        structure_name: ''
      });
      setProjects((prev) => [created, ...prev]);
      return created;
    },
    [session]
  );

  const patchProject = useCallback(async (id: string, patch: Partial<Project>) => {
    const updated = await updateProject(id, patch);
    setProjects((prev) => prev.map((p) => (p.id === id ? { ...p, ...updated } : p)));
    return updated;
  }, []);

  const softDeleteProject = useCallback(
    async (id: string) => {
      await patchProject(id, {
        deleted_at: new Date().toISOString(),
        task_state: 'DRAFT',
        task_id: ''
      });
      removeProjectInputConfig(id);
      removeProjectUiState(id);
      setProjects((prev) => prev.filter((p) => p.id !== id));
    },
    [patchProject]
  );

  return {
    projects,
    loading,
    error,
    search,
    setSearch,
    load,
    createProject,
    patchProject,
    softDeleteProject
  };
}
