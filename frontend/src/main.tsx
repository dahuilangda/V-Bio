import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import { AuthProvider } from './hooks/useAuth';
import { OverlayProvider } from './components/ui/OverlayContext';
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
  <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
    <AuthProvider>
      <OverlayProvider>
        <App />
      </OverlayProvider>
    </AuthProvider>
  </BrowserRouter>
);
