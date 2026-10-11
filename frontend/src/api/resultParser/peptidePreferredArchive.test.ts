import { describe, it, expect } from 'vitest';
import JSZip from 'jszip';
import { parseResultBundle } from './resultBundleParser';
import { derivePersistedResultConfidences } from '../../utils/resultConfidenceStorage';

/**
 * Regression: clicking a second peptide-design candidate downloads a
 * preferred-structure view archive containing only that candidate's structure
 * file. The fresh parse then knows fewer candidate structure names than the
 * persisted rows; wholesale replacement of persisted rows erased the other
 * candidates' names, so switching candidates stopped loading structures until
 * a page reload.
 */

function pdbText(): string {
  return [
    'ATOM      1  CA  ALA A' + '   1'.slice(-4) + '       0.000   0.000   0.000  1.00 50.00           C',
    'END'
  ].join('\n');
}

async function buildZip(entries: Record<string, string>): Promise<Blob> {
  const zip = new JSZip();
  for (const [name, content] of Object.entries(entries)) {
    zip.file(name, content);
  }
  const arrayBuffer = await zip.generateAsync({ type: 'arraybuffer' });
  return arrayBuffer as unknown as Blob;
}

function designResults(): Record<string, unknown> {
  const candidates = [1, 2, 3].map((rank) => ({
    rank,
    generation: 1,
    sequence: `PEPTIDE${rank}`,
    score: 1 - rank / 10,
    iptm: 0.7,
    plddt: 80,
    binder_chain_id: 'B',
    target_chain_id: 'A'
  }));
  return { candidates };
}

async function fullArchive(): Promise<Blob> {
  return buildZip({
    'design_results.json': JSON.stringify(designResults()),
    'results_summary.json': JSON.stringify({ peptide_design: {}, best_sequences: designResults().candidates }),
    'structures/rank_01.pdb': pdbText(),
    'structures/rank_02.pdb': pdbText(),
    'structures/rank_03.pdb': pdbText()
  });
}

async function preferredArchive(): Promise<Blob> {
  return buildZip({
    'design_results.json': JSON.stringify(designResults()),
    'results_summary.json': JSON.stringify({ peptide_design: {}, best_sequences: designResults().candidates }),
    'structures/rank_02.pdb': pdbText()
  });
}

const NAMED = (rows: Array<Record<string, unknown>>) =>
  rows.filter((r) => String(r.structure_name || '').trim()).length;

describe('peptide preferred-structure archive keeps persisted candidate names', () => {
  it('full parse names every rank authoritatively, without positional cross-assignment', async () => {
    const parsed = await parseResultBundle(await fullArchive(), { preservePeptideCandidateStructureText: false });
    const rows = (((parsed?.confidence as Record<string, unknown>)?.peptide_design as Record<string, unknown>)?.best_sequences ?? []) as Array<Record<string, unknown>>;
    expect(NAMED(rows)).toBe(3);
    expect(String(rows.find((r) => Number(r.rank) === 1)?.structure_name)).toBe('rank_01.pdb');
    expect(String(rows.find((r) => Number(r.rank) === 3)?.structure_name)).toBe('rank_03.pdb');
  });

  it('preferred parse names only the matching rank — no positional guessing', async () => {
    const parsed = await parseResultBundle(await preferredArchive(), {
      preservePeptideCandidateStructureText: false,
      preferredStructureName: 'rank_02.pdb'
    });
    const rows = (((parsed?.confidence as Record<string, unknown>)?.peptide_design as Record<string, unknown>)?.best_sequences ?? []) as Array<Record<string, unknown>>;
    expect(rows.length).toBe(3);
    expect(NAMED(rows)).toBe(1);
    expect(String(rows.find((r) => Number(r.rank) === 2)?.structure_name)).toBe('rank_02.pdb');
    expect(String(rows.find((r) => Number(r.rank) === 1)?.structure_name || '')).toBe('');
  });

  it('persisted candidate names survive a degraded preferred-structure pull', async () => {
    const full = await parseResultBundle(await fullArchive(), { preservePeptideCandidateStructureText: false });
    const pref = await parseResultBundle(await preferredArchive(), {
      preservePeptideCandidateStructureText: false,
      preferredStructureName: 'rank_02.pdb'
    });
    const { taskConfidence } = derivePersistedResultConfidences({
      parsedConfidenceValue: pref?.confidence,
      baseTaskConfidenceValue: full?.confidence
    });
    const mergedRows = (((taskConfidence.peptide_design as Record<string, unknown>)?.best_sequences ?? []) as Array<Record<string, unknown>>);
    expect(mergedRows.length).toBe(3);
    expect(NAMED(mergedRows)).toBe(3);
    // The viewed candidate keeps its freshly requested name.
    expect(String(mergedRows.find((r) => Number(r.rank) === 2)?.structure_name)).toBe('rank_02.pdb');
  });
});

/**
 * Regression (v11 L/D bicyclic runs): the archive carries BOTH the curated
 * `results_summary.json` top_results (rank-sorted, structure-linked) and a
 * LONGER `design_results.json` dump of every evaluated candidate in completion
 * order. Length-based selection preferred the dump, so the candidate list
 * opened on generation-1 rows with fabricated ranks and the promoted task
 * iptm/plddt described the first-evaluated candidate instead of the best one.
 */
describe('curated top_results outrank the longer design_results dump', () => {
  const completionOrderDump = {
    candidates: [
      // First-evaluated generation-1 candidate: worst score, no rank, no structure.
      { generation: 1, sequence: 'GEN1WORSTAA', score: 0.31, iptm: 0.31, plddt: 61.2 },
      { generation: 1, sequence: 'GEN1SECONDAA', score: 0.44, iptm: 0.44, plddt: 66 },
      // Curated elites appear mid-dump without the mirror fields.
      { generation: 6, sequence: 'BESTCANDIDATE', score: 0.96, iptm: 0.96, plddt: 96 },
      { generation: 5, sequence: 'RUNNERUPAAAAA', score: 0.91, iptm: 0.91, plddt: 93 }
    ]
  };
  const curatedSummary = {
    summary: { best_score: 0.96, backend: 'protenix', iterations: 6 },
    peptide_design: { candidate_count: 2 },
    top_results: [
      {
        rank: 1,
        generation: 6,
        sequence: 'BESTCANDIDATE',
        score: 0.96,
        iptm: 0.96,
        plddt: 96,
        structure_file: 'structures/rank_01.pdb',
        structure_name: 'rank_01.pdb'
      },
      {
        rank: 2,
        generation: 5,
        sequence: 'RUNNERUPAAAAA',
        score: 0.91,
        iptm: 0.91,
        plddt: 93,
        structure_file: 'structures/rank_02.pdb',
        structure_name: 'rank_02.pdb'
      }
    ]
  };

  it('uses top_results rows and promotes the real best metrics', async () => {
    const zip = await buildZip({
      'design_results.json': JSON.stringify(completionOrderDump),
      'results_summary.json': JSON.stringify(curatedSummary),
      'structures/rank_01.pdb': pdbText(),
      'structures/rank_02.pdb': pdbText()
    });
    const parsed = await parseResultBundle(zip, { preservePeptideCandidateStructureText: false });
    const confidence = (parsed?.confidence ?? {}) as Record<string, unknown>;
    const rows = ((confidence.peptide_design as Record<string, unknown>)?.best_sequences ?? []) as Array<Record<string, unknown>>;
    expect(rows.map((r) => String(r.sequence))).toEqual(['BESTCANDIDATE', 'RUNNERUPAAAAA']);
    expect(rows.map((r) => Number(r.rank))).toEqual([1, 2]);
    expect(rows.map((r) => String(r.structure_name))).toEqual(['rank_01.pdb', 'rank_02.pdb']);
    // Task-level metrics promote the rank-1 candidate, not the dump's head.
    expect(confidence.iptm).toBeCloseTo(0.96, 5);
    expect(confidence.plddt).toBeCloseTo(96, 5);
  });
});
