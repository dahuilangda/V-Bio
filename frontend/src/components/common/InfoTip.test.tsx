import { describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { InfoTip, tooltipHorizontalShift } from './InfoTip';

describe('tooltipHorizontalShift', () => {
  it('leaves a bubble that fits the viewport where it is', () => {
    expect(tooltipHorizontalShift(120, 300, 390)).toBe(0);
  });

  it('pulls a bubble back from the right edge', () => {
    expect(tooltipHorizontalShift(121, 421, 390)).toBe(-39);
  });

  it('pushes a bubble away from the left edge', () => {
    expect(tooltipHorizontalShift(-12, 180, 390)).toBe(20);
  });
});

describe('InfoTip', () => {
  it('renders the info trigger with an accessible tooltip bubble', () => {
    const html = renderToString(
      <MemoryRouter>
        <InfoTip text="Runs the closed loop on the selected engine." />
      </MemoryRouter>
    );
    expect(html).toContain('info-tip');
    expect(html).toContain('aria-label="Runs the closed loop on the selected engine."');
    expect(html).toContain('role="tooltip"');
    expect(html).toContain('info-tip-bubble');
    expect(html).not.toContain('info-tip-label');
  });

  it('renders an optional visible label next to the icon', () => {
    const html = renderToString(
      <MemoryRouter>
        <InfoTip text="Never both." label="Library rules" align="end" />
      </MemoryRouter>
    );
    expect(html).toContain('info-tip-label');
    expect(html).toContain('Library rules');
    expect(html).toContain('info-tip-bubble--end');
  });
});
