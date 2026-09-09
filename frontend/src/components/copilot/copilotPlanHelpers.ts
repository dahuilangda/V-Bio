export const ARGUMENT_LABELS: Record<string, string> = {
  smiles: 'SMILES',
  structureUrl: 'Structure',
  sequence: 'Sequence',
  accession: 'Accession',
  cid: 'CID',
  projectId: 'Project',
  projectName: 'Project',
  taskRowId: 'Task',
  taskName: 'Task',
  components: 'Components',
  screeningCompounds: 'Library',
  metadataPatch: 'Changes',
  parameterPatch: 'Parameters',
};

export const TRIVIAL_ARGUMENT_KEYS = new Set([
  'create',
  'activityFilter',
  'workflowFilter',
  'sortBy',
  'backendFilter',
  'typeFilter',
  'stateFilter',
  'search',
  'pageSize',
  'updatedWithinDays',
  'minTaskCount',
]);

// Human-readable labels for the argument keys a confirmation action commonly carries, in display
// priority order. Drives the semantic key/value summary that replaces the old raw-JSON dump.


export interface ActionSummaryEntry {
  label: string;
  value: string;
}

import type { CopilotPlanAction } from '../../types/models';

export function planActionKey(action: CopilotPlanAction): string {
  const planId = String(action.plan_id || '').trim();
  const operationId = String(action.operation_id || '').trim();
  return planId && operationId ? `${planId}:${operationId}` : '';
}

export function formatActionSummary(action: CopilotPlanAction): ActionSummaryEntry[] {
  const args = action.arguments;
  if (!args || typeof args !== 'object') return [];
  const rows: ActionSummaryEntry[] = [];
  const seenLabels = new Set<string>();
  for (const [key, value] of Object.entries(args)) {
    if (TRIVIAL_ARGUMENT_KEYS.has(key)) continue;
    const label = ARGUMENT_LABELS[key];
    if (!label) continue;
    let text: string;
    if (typeof value === 'string') {
      text = value.trim();
    } else if (typeof value === 'number' || typeof value === 'boolean') {
      text = String(value);
    } else if (Array.isArray(value)) {
      text = `${value.length} item${value.length === 1 ? '' : 's'}`;
    } else if (value && typeof value === 'object') {
      // For nested objects (e.g. parameterPatch), render friendly key=value
      // pairs with display labels so engine/chirality choices are readable.
      const FRIENDLY: Record<string, string> = {
        backend: '',
        boltz2dock: 'Boltz2Dock',
        protenix2dock: 'Protenix2Dock',
        peptideChirality: 'Chirality',
        d: 'D-peptide',
        l: 'L-peptide',
        peptideDesignMode: 'Mode',
        linear: 'Linear', cyclic: 'Cyclic', bicyclic: 'Bicyclic',
      };
      const parts = Object.entries(value as Record<string, unknown>).map(([k, v]) => {
        const kl = k.replace(/_/g, '');
        const labelEntry = Object.prototype.hasOwnProperty.call(FRIENDLY, k) ? FRIENDLY[k]
          : Object.prototype.hasOwnProperty.call(FRIENDLY, kl) ? FRIENDLY[kl] : k;
        const vl = typeof v === 'string' && Object.prototype.hasOwnProperty.call(FRIENDLY, v)
          ? FRIENDLY[v]
          : String(v);
        return labelEntry === '' || labelEntry === undefined ? vl : `${labelEntry}: ${vl}`;
      });
      text = parts.filter(Boolean).join(', ') || '{}';
    } else {
      continue;
    }
    if (!text) continue;
    const display = text.length > 64 ? `${text.slice(0, 61)}...` : text;
    if (seenLabels.has(label)) continue;
    seenLabels.add(label);
    rows.push({ label, value: display });
  }
  return rows;
}

