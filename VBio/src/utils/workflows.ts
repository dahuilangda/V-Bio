export type WorkflowKey =
  | 'prediction'
  | 'designer'
  | 'bicyclic_designer'
  | 'lead_optimization'
  | 'affinity';

export interface WorkflowDefinition {
  key: WorkflowKey;
  taskType: string;
  title: string;
  shortTitle: string;
  description: string;
  runLabel: string;
  supportsSequenceInputs: boolean;
}

export const WORKFLOWS: WorkflowDefinition[] = [
  {
    key: 'prediction',
    taskType: 'prediction',
    title: 'Structure Prediction',
    shortTitle: 'Prediction',
    description: 'Protein-ligand structure prediction with Boltz-2 / AlphaFold3.',
    runLabel: 'Run Prediction',
    supportsSequenceInputs: true
  },
  {
    key: 'designer',
    taskType: 'designer',
    title: 'Molecule Designer',
    shortTitle: 'Designer',
    description: 'Generative molecular design workflow.',
    runLabel: 'Run Design',
    supportsSequenceInputs: false
  },
  {
    key: 'bicyclic_designer',
    taskType: 'bicyclic_designer',
    title: 'Bicyclic Designer',
    shortTitle: 'Bicyclic',
    description: 'Bicyclic peptide design workflow.',
    runLabel: 'Run Bicyclic Design',
    supportsSequenceInputs: false
  },
  {
    key: 'lead_optimization',
    taskType: 'lead_optimization',
    title: 'Lead Optimization',
    shortTitle: 'Lead Opt',
    description: 'Iterative lead optimization pipeline.',
    runLabel: 'Run Optimization',
    supportsSequenceInputs: false
  },
  {
    key: 'affinity',
    taskType: 'affinity',
    title: 'Affinity Scoring',
    shortTitle: 'Affinity',
    description: 'Binding affinity estimation and scoring.',
    runLabel: 'Run Affinity',
    supportsSequenceInputs: false
  }
];

const taskTypeAlias: Record<string, WorkflowKey> = {
  prediction: 'prediction',
  'boltz-2 prediction': 'prediction',
  'boltz2 prediction': 'prediction',
  designer: 'designer',
  bicyclic_designer: 'bicyclic_designer',
  'bicyclic designer': 'bicyclic_designer',
  lead_optimization: 'lead_optimization',
  'lead optimization': 'lead_optimization',
  affinity: 'affinity',
  'affinity prediction': 'affinity'
};

export function normalizeWorkflowKey(taskTypeRaw: string | null | undefined): WorkflowKey {
  const normalized = String(taskTypeRaw || '').trim().toLowerCase();
  if (!normalized) return 'prediction';
  return taskTypeAlias[normalized] || 'prediction';
}

export function getWorkflowDefinition(taskTypeRaw: string | null | undefined): WorkflowDefinition {
  const key = normalizeWorkflowKey(taskTypeRaw);
  const found = WORKFLOWS.find((item) => item.key === key);
  return found || WORKFLOWS[0];
}
