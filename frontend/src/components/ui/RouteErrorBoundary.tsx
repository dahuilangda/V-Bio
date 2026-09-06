import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

/**
 * Route-level error boundary.
 *
 * Primary job: survive deploys that happen under long-lived SPA tabs. A rebuild
 * replaces dist with new content hashes, so a tab opened before the deploy
 * still runs the old bundle and its lazy route chunks (`ProjectsPage-<old>.js`)
 * 404. React Router v7 transitions then freeze the previous view while the URL
 * has already changed — the "nav click does nothing" symptom.
 *
 * Handling: on a chunk-load failure, reload the page ONCE (fresh index.html →
 * fresh chunks, navigation works again). A sessionStorage marker bounds the
 * reload to one attempt per 10s so a genuinely broken deploy cannot loop.
 * Any other render error lands on an explicit fallback with a manual reload —
 * better than a white screen, and the error still reaches the console.
 */

const CHUNK_ERROR_PATTERNS = [
  'failed to fetch dynamically imported module',
  'error loading dynamically imported module',
  'importing a module script failed',
  'failed to load module script'
];

const RELOAD_MARKER_KEY = 'vbio:chunk-error-reload-at';
const RELOAD_GUARD_WINDOW_MS = 10_000;

export function isStaleChunkError(error: unknown): boolean {
  const parts: string[] = [];
  if (error instanceof Error) {
    parts.push(error.message);
    if (error.stack) parts.push(error.stack);
  } else {
    parts.push(String(error));
  }
  const haystack = parts.join(' ').toLowerCase();
  return CHUNK_ERROR_PATTERNS.some((pattern) => haystack.includes(pattern));
}

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class RouteErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    if (isStaleChunkError(error) && this.reloadOnceForStaleChunks(error)) {
      return;
    }
    console.error('[RouteErrorBoundary] route render failed:', error, info.componentStack);
  }

  private reloadOnceForStaleChunks(error: Error): boolean {
    if (typeof window === 'undefined' || typeof window.sessionStorage === 'undefined') {
      return false;
    }
    try {
      const last = Number(window.sessionStorage.getItem(RELOAD_MARKER_KEY) || 0);
      if (Number.isFinite(last) && Date.now() - last < RELOAD_GUARD_WINDOW_MS) {
        return false;
      }
      window.sessionStorage.setItem(RELOAD_MARKER_KEY, String(Date.now()));
      console.warn(
        '[RouteErrorBoundary] a new deployment invalidated this tab’s code chunks — reloading to pick up the current build.',
        error.message
      );
      window.location.reload();
      return true;
    } catch {
      // Storage unavailable (private mode etc.) — reload without the loop guard;
      // a single reload for a chunk error is still the correct recovery.
      window.location.reload();
      return true;
    }
  }

  render(): ReactNode {
    const { error } = this.state;
    if (error) {
      return (
        <div className="centered-page">
          <div className="alert error" role="alert">
            This page could not finish loading. A new deployment may have replaced the app
            assets, or the page hit an unexpected error.
          </div>
          <button type="button" className="btn btn-primary" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
