import { describe, expect, it } from 'vitest';
import {
  selectPeptideCandidateRowsFromSummaries,
  type PeptideCandidateSummarySource
} from './resultBundleParser';

/**
 * Direct tests for the candidate-source selection: a canonical results_summary
 * `top_results` array (curated, rank-sorted) must outrank the longer
 * design_results dump of every evaluated candidate in completion order, while
 * legacy archives without top_results keep the longest-source fallback.
 */

const source = (path: string, payload: unknown): PeptideCandidateSummarySource => ({
  path,
  payload: payload as Record<string, unknown>
});

describe('selectPeptideCandidateRowsFromSummaries', () => {
  it('canonical top_results win over a longer completion-order dump', () => {
    const rows = selectPeptideCandidateRowsFromSummaries([
      source('design_results.json', {
        candidates: [
          { generation: 1, sequence: 'GEN1JUNKAAAA', score: 0.31 },
          { generation: 1, sequence: 'GEN1JUNKBBBB', score: 0.44 },
          { rank: 1, generation: 6, sequence: 'BESTCANDIDATE', score: 0.96 },
          { rank: 2, generation: 5, sequence: 'RUNNERUPAAAAA', score: 0.91 }
        ]
      }),
      source('results_summary.json', {
        summary: { best_score: 0.96 },
        top_results: [
          { rank: 1, generation: 6, sequence: 'BESTCANDIDATE', score: 0.96 },
          { rank: 2, generation: 5, sequence: 'RUNNERUPAAAAA', score: 0.91 }
        ]
      })
    ]);
    expect(rows.map((r) => r.sequence)).toEqual(['BESTCANDIDATE', 'RUNNERUPAAAAA']);
  });

  it('longest legacy source wins when no top_results exists', () => {
    const rows = selectPeptideCandidateRowsFromSummaries([
      source('results_summary.json', {
        best_sequences: [{ sequence: 'AAA', score: 1 }]
      }),
      source('design_results.json', {
        candidates: [
          { sequence: 'BBB', score: 0.5 },
          { sequence: 'CCC', score: 0.4 }
        ]
      })
    ]);
    expect(rows.map((r) => r.sequence)).toEqual(['BBB', 'CCC']);
  });

  it('equal lengths resolve by file priority (design_results first)', () => {
    const rows = selectPeptideCandidateRowsFromSummaries([
      source('results_summary.json', {
        best_sequences: [{ sequence: 'FROMSUMMARY', score: 1 }]
      }),
      source('design_results.json', {
        candidates: [{ sequence: 'FROMDUMP', score: 1 }]
      })
    ]);
    expect(rows.map((r) => r.sequence)).toEqual(['FROMDUMP']);
  });

  it('empty and malformed sources are skipped', () => {
    const rows = selectPeptideCandidateRowsFromSummaries([
      source('design_results.json', { candidates: [] }),
      source('results_summary.json', { top_results: [{ sequence: 'ONLY' }] })
    ]);
    expect(rows.map((r) => r.sequence)).toEqual(['ONLY']);
    expect(selectPeptideCandidateRowsFromSummaries([])).toEqual([]);
  });
});
