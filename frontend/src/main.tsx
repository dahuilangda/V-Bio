import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { AuthProvider } from './hooks/useAuth';
import { OverlayProvider } from './components/ui/OverlayContext';
import { RouteErrorBoundary } from './components/ui/RouteErrorBoundary';
import './styles/global.css';

// Main-thread jank recorder: intermittent "whole tab freezes" (e.g. task-list transitions)
// leave no error behind, so keep a rolling window of long tasks (>200ms) with their
// attribution. After a freeze, inspect `window.__vbioLongTasks` in devtools to see which
// script blocked the last ticks — no reproduction needed.
interface RecordedLongTask {
  at: string;
  durationMs: number;
  name: string;
  attribution: string;
}

declare global {
  interface Window {
    __vbioLongTasks?: RecordedLongTask[];
  }
}

try {
  if (typeof PerformanceObserver !== 'undefined' && window.__vbioLongTasks === undefined) {
    const recorded: RecordedLongTask[] = (window.__vbioLongTasks = []);
    const observer = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        const item = entry as PerformanceEntry & {
          attribution?: Array<{ name?: string; containerName?: string; containerSrc?: string }>;
        };
        const first = item.attribution?.[0];
        recorded.push({
          at: new Date().toISOString(),
          durationMs: Math.round(entry.duration),
          name: entry.name,
          attribution: first ? `${first.name || 'unknown'}:${first.containerName || first.containerSrc || ''}` : 'unknown'
        });
      }
      if (recorded.length > 50) recorded.splice(0, recorded.length - 50);
    });
    observer.observe({ type: 'longtask', buffered: false });
  }
} catch {
  // Browsers without longtask support just skip the recorder.
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <BrowserRouter
    // v7_startTransition was REMOVED on purpose: it wraps navigations in a
    // transition, and a suspended route render (slow/hung lazy chunk) then keeps
    // the previous view committed with zero feedback — observed as "URL changes
    // but the page never switches" on Safari. v6 default behavior shows the
    // PageLoading fallback immediately instead; visible feedback beats a silent
    // freeze. v7_relativeSplatPath stays enabled (unrelated fix).
    future={{ v7_relativeSplatPath: true }}
  >
    <AuthProvider>
      <OverlayProvider>
        <RouteErrorBoundary>
          <App />
        </RouteErrorBoundary>
      </OverlayProvider>
    </AuthProvider>
  </BrowserRouter>
);
