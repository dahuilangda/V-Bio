export type WorkflowKey =
  | 'prediction'
  | 'peptide_design'
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
    key: 'peptide_design',
    taskType: 'peptide_design',
    title: 'Peptide Designer',
    shortTitle: 'Peptide',
    description: 'Unified cyclic and bicyclic peptide design workflow.',
    runLabel: 'Run Peptide Designer',
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
  structure_prediction: 'prediction',
  structureprediction: 'prediction',
  'boltz-2 prediction': 'prediction',
  'boltz2 prediction': 'prediction',
  boltz_2_prediction: 'prediction',
  boltz2_prediction: 'prediction',
  peptide_design: 'peptide_design',
  peptide_designer: 'peptide_design',
  'peptide design': 'peptide_design',
  'peptide designer': 'peptide_design',
  peptidedesign: 'peptide_design',
  peptidedesigner: 'peptide_design',
  designer: 'peptide_design',
  bicyclic_designer: 'peptide_design',
  'bicyclic designer': 'peptide_design',
  bicyclicdesigner: 'peptide_design',
  lead_optimization: 'lead_optimization',
  'lead optimization': 'lead_optimization',
  leadoptimization: 'lead_optimization',
  lead_opt: 'lead_optimization',
  leadopt: 'lead_optimization',
  affinity: 'affinity',
  affinity_scoring: 'affinity',
  affinityscore: 'affinity',
  binding_affinity: 'affinity',
  score_binding_affinity: 'affinity',
  'affinity prediction': 'affinity'
};

export function normalizeWorkflowKey(taskTypeRaw: string | null | undefined): WorkflowKey {
  const normalized = String(taskTypeRaw || '').trim().toLowerCase();
  if (!normalized) return 'prediction';
  const compact = normalized.replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
  const compactNoUnderscore = compact.replace(/_/g, '');
  return taskTypeAlias[normalized] || taskTypeAlias[compact] || taskTypeAlias[compactNoUnderscore] || 'prediction';
}

export function isPredictionLikeWorkflowKey(taskTypeRaw: string | null | undefined): boolean {
  const key = normalizeWorkflowKey(taskTypeRaw);
  return key === 'prediction' || key === 'peptide_design';
}

export function getWorkflowDefinition(taskTypeRaw: string | null | undefined): WorkflowDefinition {
  const key = normalizeWorkflowKey(taskTypeRaw);
  const found = WORKFLOWS.find((item) => item.key === key);
  return found || WORKFLOWS[0];
}
