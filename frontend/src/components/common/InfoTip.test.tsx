import { describe, expect, it } from 'vitest';
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { InfoTip } from './InfoTip';

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
