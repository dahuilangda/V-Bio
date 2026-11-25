import { describe, it, expect } from 'vitest';
import { renderToString } from 'react-dom/server';
import { MetricsPanel } from './MetricsPanel';

function confidenceHtml(data: Record<string, unknown>): string {
  return renderToString(<MetricsPanel title="Confidence" data={data} />);
}

describe('MetricsPanel pair ipSAE list', () => {
  it('marks virtual-molecule rows and keeps both directions', () => {
    const html = confidenceHtml({
      pair_ipsae: [
        { a: 'A', b: 'C', a_to_b: 0.3, b_to_a: 0.2 },
        { a: 'A+B', b: 'C', a_to_b: 0.38, b_to_a: 0.43, grouped: true }
      ]
    });
    expect(html).toContain('A+B → C');
    expect(html).toContain('C → A+B');
    expect(html).toContain('0.4300');
    expect(html.match(/confidence-pair-line-grouped/g)).toHaveLength(2);
  });

  it('leaves plain chain pairs unmarked', () => {
    const html = confidenceHtml({
      pair_ipsae: [{ a: 'A', b: 'C', a_to_b: 0.3, b_to_a: 0.2 }]
    });
    expect(html).toContain('A → C');
    expect(html).not.toContain('confidence-pair-line-grouped');
  });
});
