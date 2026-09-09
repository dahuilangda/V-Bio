import { describe, expect, it, vi } from 'vitest';
import { renderToString } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { isStaleChunkError, RouteErrorFallback } from './RouteErrorBoundary';

describe('isStaleChunkError', () => {
  it.each([
    'Failed to fetch dynamically imported module: https://app/assets/ProjectsPage-old.js',
    'Error loading dynamically imported module',
    'Importing a module script failed.',
    'Failed to load module script: expected a JavaScript module'
  ])('detects stale-chunk failure: %s', (message) => {
    expect(isStaleChunkError(new Error(message))).toBe(true);
  });

  it('detects chunk failures buried in the stack trace', () => {
    const error = new Error('chunk failed');
    error.stack = 'TypeError: failed to fetch dynamically imported module\n at ...';
    expect(isStaleChunkError(error)).toBe(true);
  });

  it('does not classify ordinary render errors as stale chunks', () => {
    expect(isStaleChunkError(new Error('Cannot read properties of undefined'))).toBe(false);
    expect(isStaleChunkError('a string failure')).toBe(false);
    expect(isStaleChunkError(undefined)).toBe(false);
  });
});

describe('RouteErrorFallback', () => {
  it('offers in-app recovery as the primary action and reload as the fallback', () => {
    const onRecover = vi.fn();
    const html = renderToString(
      <MemoryRouter>
        <RouteErrorFallback onRecover={onRecover} />
      </MemoryRouter>
    );
    expect(html).toContain('role="alert"');
    expect(html.indexOf('Back to Projects')).toBeGreaterThan(-1);
    expect(html.indexOf('Back to Projects')).toBeLessThan(html.indexOf('Reload'));
  });
});
