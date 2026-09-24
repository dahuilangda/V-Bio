import { describe, expect, it } from 'vitest';
import { findProgressPercent } from './projectMetrics';
import { buildStagedProgressText } from '../../components/project/peptideDesignResults/parseHelpers';

describe('findProgressPercent', () => {
  it('reads the worker payload shape info.progress.progress_percent', () => {
    // What the gateway returns after _merge_tracker_payload: statusInfo IS
    // response.info, so the worker's progress object sits at info.progress.
    const statusInfo = {
      progress: {
        progress_percent: 45.5,
        current_generation: 3,
        total_generations: 6,
        completed_tasks: 48,
        total_tasks: 96
      },
      status: 'generation 3/6 running'
    };
    expect(findProgressPercent(statusInfo)).toBe(45.5);
  });

  it('falls back to the generation ratio when percent is absent', () => {
    const statusInfo = {
      progress: { current_generation: 2, total_generations: 8 }
    };
    expect(findProgressPercent(statusInfo)).toBe(25);
  });

  it('falls back to the completed/total task ratio', () => {
    const statusInfo = {
      info: { progress: { completed_tasks: 10, total_tasks: 40 } }
    };
    expect(findProgressPercent(statusInfo)).toBe(25);
  });

  it('does not rescale small 0-100 percent values (0.5 means half a percent)', () => {
    expect(findProgressPercent({ progress_percent: 0.5 })).toBe(0.5);
    expect(findProgressPercent({ progress: { progress_percent: 1 } })).toBe(1);
  });

  it('still scales bare 0-1 fractions under the ambiguous progress key', () => {
    expect(findProgressPercent({ progress: 0.25 })).toBe(25);
    expect(findProgressPercent({ ratio: 0.5 })).toBe(50);
  });

  it('returns null for payloads without progress information', () => {
    expect(findProgressPercent({})).toBeNull();
    expect(findProgressPercent({ peptide_design: { design_mode: 'cyclic' } })).toBeNull();
  });
});

describe('buildStagedProgressText', () => {
  it('renders generation, candidate counts and ETA', () => {
    const statusInfo = {
      progress: {
        current_generation: 3,
        total_generations: 6,
        completed_tasks: 45,
        total_tasks: 128,
        estimated_remaining_seconds: 2100
      }
    };
    expect(buildStagedProgressText(statusInfo)).toBe('Generation 3/6 · 45/128 candidates · ~35m left');
  });

  it('omits missing parts instead of rendering placeholders', () => {
    expect(buildStagedProgressText({ progress: { current_generation: 1, total_generations: 4 } })).toBe(
      'Generation 1/4'
    );
    expect(buildStagedProgressText({})).toBeNull();
  });
});
