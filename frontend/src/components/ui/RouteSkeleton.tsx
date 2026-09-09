import './RouteSkeleton.css';

/**
 * Suspense fallback for lazily-loaded routes.
 *
 * Keeps the shell (nav/header) mounted while a route chunk streams in, and
 * mirrors the destination layout — header row, filter toolbar, content rows —
 * so the first navigation to a route reads as a progressive load instead of a
 * blank flash of text. Renders for a few hundred ms at most in practice; the
 * structure (not fidelity) carries the perception.
 */
export function RouteSkeleton() {
  return (
    <div className="route-skeleton" role="status" aria-live="polite" aria-label="Loading page">
      <span className="route-skeleton-sr">Loading page…</span>
      <div className="route-skeleton-header">
        <div className="skel skel-title" />
        <div className="skel skel-button" />
      </div>
      <div className="route-skeleton-panel">
        <div className="route-skeleton-toolbar">
          <div className="skel skel-input" />
          <div className="skel skel-select" />
          <div className="skel skel-select" />
        </div>
        <div className="route-skeleton-rows">
          {Array.from({ length: 6 }, (_, index) => (
            <div className="route-skeleton-row" key={index}>
              <div className="skel skel-cell skel-cell-name" style={{ animationDelay: `${index * 90}ms` }} />
              <div className="skel skel-cell" style={{ animationDelay: `${index * 90 + 45}ms` }} />
              <div className="skel skel-cell skel-cell-sm" style={{ animationDelay: `${index * 90 + 90}ms` }} />
              <div className="skel skel-cell skel-cell-sm" style={{ animationDelay: `${index * 90 + 135}ms` }} />
              <div className="skel skel-cell skel-cell-actions" style={{ animationDelay: `${index * 90 + 180}ms` }} />
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
