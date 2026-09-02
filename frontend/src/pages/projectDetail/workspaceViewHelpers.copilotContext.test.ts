import { describe, it, expect } from 'vitest';
import {
  summarizeCopilotTask,
  summarizeCopilotTaskProperties
} from './workspaceViewHelpers';
import type { ProjectTask } from '../../types/models';

function leadOptProperties(candidateCount = 365) {
  const candidates = Array.from({ length: candidateCount }, (_, index) => ({
    smiles: `CC(=O)N(C)C${'C'.repeat(index % 7)}c1ccccc1O${'N'.repeat(index % 3)}`,
    n_pairs: 3 + (index % 9),
    median_delta: -0.4 + (index % 13) * 0.05,
    properties: { molecular_weight: 220.1 + index, logp: 2.1, tpsa: 46.3 },
    property_deltas: { mw: 12.5, logp: -0.3, tpsa: 0.0 },
    final_highlight_atom_indices: Array.from({ length: 12 }, (_, i) => i),
    constant_smiles: 'CC(=O)N(C)C'
  }));
  const predictions: Record<string, unknown> = {};
  for (let index = 0; index < 80; index += 1) {
    const smiles = `CC(=O)N(C)C${'C'.repeat(index + 1)}c1ccccc1O`;
    predictions[`boltz::${smiles}`] = {
      taskId: `task-${index}`,
      state: index % 2 === 0 ? 'SUCCESS' : 'QUEUED',
      backend: 'boltz',
      pairIptm: 0.62 + (index % 11) * 0.01,
      ligandPlddt: 84.0,
      ligandAtomPlddts: Array.from({ length: 256 }, (_, i) => 80.0 + (i % 15)),
      updatedAt: 1700000000 + index
    };
  }
  return {
    lead_opt_list: {
      stage: 'candidates_ready',
      prediction_stage: 'idle',
      query_id: 'q-1',
      task_id: 't-1',
      transform_count: 365,
      candidate_count: 365,
      bucket_count: 80,
      mmp_database_id: 'mmp-1',
      mmp_database_label: 'ChEMBL30',
      target_chain: 'A',
      ligand_chain: 'B',
      selection: { selected_fragment_ids: ['f1'], direction: 'decrease', query_property: 'affinity' },
      query_result: {
        query_mode: 'one-to-many',
        aggregation_type: 'median',
        count: 365,
        global_count: 512,
        min_pairs: 3,
        transforms: candidates.slice(0, 80),
        global_transforms: candidates.slice(0, 80),
        clusters: candidates.slice(0, 80)
      },
      enumerated_candidates: candidates
    },
    lead_opt_state: {
      stage: 'predictions_partial',
      prediction_stage: 'running',
      query_id: 'q-1',
      task_id: 't-1',
      prediction_task_id: 'pt-9',
      prediction_candidate_smiles: 'CC(=O)N(C)Cc1ccccc1O',
      prediction_summary: { total: 80, queued: 40, running: 0, success: 40, failure: 0 },
      selected_backend: 'boltz',
      target_chain: 'A',
      ligand_chain: 'B',
      prediction_by_smiles: predictions,
      reference_prediction_by_backend: {
        boltz: {
          taskId: 'ref-1',
          state: 'SUCCESS',
          backend: 'boltz',
          pairIptm: 0.71,
          ligandPlddt: 88.2,
          updatedAt: 1700000001
        }
      }
    }
  };
}

describe('summarizeCopilotTaskProperties — bounded lead-opt context contract', () => {
  it('keeps the lead-opt projection small even with a full MMP snapshot', () => {
    const summary = summarizeCopilotTaskProperties(leadOptProperties());
    const serialized = JSON.stringify(summary);
    // The raw snapshot is ~150k chars; the Copilot view must stay a bounded summary.
    expect(JSON.stringify(leadOptProperties()).length).toBeGreaterThan(100000);
    expect(serialized.length).toBeLessThan(8000);

    const list = summary.lead_opt_list as Record<string, any>;
    expect(list.stage).toBe('candidates_ready');
    expect(list.query_id).toBe('q-1');
    expect(list.mmp_database_label).toBe('ChEMBL30');
    expect(list.selection).toEqual({ selected_fragment_ids: ['f1'], direction: 'decrease', query_property: 'affinity' });
    expect(list.query_result).toEqual({ query_mode: 'one-to-many', aggregation_type: 'median', count: 365, global_count: 512, min_pairs: 3 });

    const candidates = list.enumerated_candidates as Record<string, any>;
    expect(candidates.count).toBe(365);
    expect(candidates.top).toHaveLength(8);
    // Render-only data (highlight atom indices) never reaches the Copilot.
    expect(JSON.stringify(candidates.top[0])).not.toContain('final_highlight_atom_indices');
    expect(candidates.top[0].smiles).toBeTruthy();
    expect(candidates.top[0].properties.molecular_weight).toBe(220.1);

    const state = summary.lead_opt_state as Record<string, any>;
    expect(state.prediction_summary.total).toBe(80);
    const predictions = state.prediction_by_smiles as Record<string, any>;
    expect(predictions.total).toBe(80);
    expect(predictions.success).toBe(40);
    expect(predictions.top.length).toBeLessThanOrEqual(8);
    // SUCCESS records first, ranked by interface metric — the head sample is useful, not a clip.
    expect(predictions.top[0].state).toBe('SUCCESS');
    expect(predictions.top[0].smiles).toContain('c1ccccc1');
    expect(typeof predictions.top[0].pairIptm).toBe('number');
    // Per-atom pLDDT arrays never reach the Copilot context.
    expect(JSON.stringify(predictions.top)).not.toContain('ligandAtomPlddts');

    const reference = state.reference_prediction_by_backend as Array<Record<string, any>>;
    expect(reference).toHaveLength(1);
    expect(reference[0].pairIptm).toBe(0.71);
  });

  it('passes scalars through and reduces unknown nested objects to key names', () => {
    const summary = summarizeCopilotTaskProperties({
      stage: 'done',
      enabled: true,
      note: '   a short note   ',
      longNote: 'x'.repeat(500),
      __vbio_input_options_v1: { seed: 1, peptideResiduePool: [1, 2, 3] },
      someRows: [{ a: 1 }, { a: 2 }]
    });
    expect(summary.stage).toBe('done');
    expect(summary.enabled).toBe(true);
    expect(summary.note).toBe('a short note');
    expect((summary.longNote as string).length).toBeLessThanOrEqual(200);
    expect(summary.__vbio_input_options_v1).toEqual({ keys: ['seed', 'peptideResiduePool'] });
    expect(summary.someRows).toEqual({ count: 2 });
  });

  it('keeps summarizeCopilotTask bounded end to end for a lead-opt task row', () => {
    const task = {
      id: 'row-1',
      project_id: 'p1',
      name: 'Lead Opt run',
      task_state: 'SUCCESS',
      task_id: 't-1',
      properties: leadOptProperties(),
      confidence: { lead_opt_mmp: leadOptProperties().lead_opt_state },
      components: [],
      constraints: []
    } as unknown as ProjectTask;
    const summary = summarizeCopilotTask(task)!;
    const serialized = JSON.stringify(summary);
    expect(serialized.length).toBeLessThan(12000);
    expect(serialized).not.toContain('ligandAtomPlddts');
    expect(summary.id).toBe('row-1');
    expect(summary.task_state).toBe('SUCCESS');
  });
});
