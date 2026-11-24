import { describe, it, expect } from 'vitest';
import { shrinkToFitScale } from './useShrinkToFit';

describe('shrinkToFitScale', () => {
  it('leaves content that fits unscaled', () => {
    expect(shrinkToFitScale(200, 232)).toBe(1);
    expect(shrinkToFitScale(232, 232)).toBe(1);
  });

  it('scales content down to the available width', () => {
    expect(shrinkToFitScale(249, 232)).toBe(0.931);
  });

  it('ignores measurements taken before layout', () => {
    expect(shrinkToFitScale(249, 0)).toBe(1);
    expect(shrinkToFitScale(0, 232)).toBe(1);
  });
});
