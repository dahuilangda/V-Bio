import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react';

/**
 * Coordinates overlapping floating UI (the persistent Copilot panel and modal-mask dialogs) so they
 * never visually collide.
 *
 * Problem this solves: the Copilot panel is an always-open floating window, while host pages render
 * modal-mask dialogs (new-project, sharing, ...). When both are visible they overlap with no z-index
 * or focus coordination. Rather than a strict single-overlay stack (which would force the Copilot
 * closed — undesirable for a persistent assistant), this context lets a dialog announce it is open;
 * the Copilot panel listens and collapses itself to a small dock chip while any dialog is active,
 * restoring once the dialog closes. This is generic — any component that opts in via
 * useOverlayHost is coordinated, with no per-dialog wiring.
 */

interface OverlayEntry {
  id: string;
  kind: string;
}

interface OverlayContextValue {
  /** The set of currently-open overlays, in registration order. */
  openOverlays: OverlayEntry[];
  /** True when at least one overlay (modal dialog) is open. */
  hasOpenOverlay: boolean;
  /** Register an overlay as open; returns a function to close (deregister) it. */
  openOverlay: (id: string, kind?: string) => () => void;
}

const OverlayContext = createContext<OverlayContextValue | null>(null);

export function OverlayProvider({ children }: { children: ReactNode }) {
  const [openOverlays, setOpenOverlays] = useState<OverlayEntry[]>([]);

  const openOverlay = useCallback((id: string, kind: string = 'dialog') => {
    setOpenOverlays((prev) => {
      if (prev.some((entry) => entry.id === id)) return prev;
      return [...prev, { id, kind }];
    });
    return () => {
      setOpenOverlays((prev) => prev.filter((entry) => entry.id !== id));
    };
  }, []);

  const value = useMemo<OverlayContextValue>(
    () => ({
      openOverlays,
      hasOpenOverlay: openOverlays.length > 0,
      openOverlay,
    }),
    [openOverlays, openOverlay]
  );

  return <OverlayContext.Provider value={value}>{children}</OverlayContext.Provider>;
}

/**
 * Hook for a component that can host/guard an overlay (e.g. a modal-mask dialog). Calling the
 * returned opener registers the overlay while it is open; the returned closer deregisters it. The
 * hook is a no-op when no provider is present, so callers work with or without the provider mounted.
 */
export function useOverlayHost() {
  const context = useContext(OverlayContext);
  return useCallback(
    (id: string, kind?: string) => {
      if (!context) return () => {};
      return context.openOverlay(id, kind);
    },
    [context]
  );
}

/**
 * Hook for a persistent floating panel (the Copilot) to learn whether any overlay is open, so it can
 * collapse out of the way. Returns null when no provider is present (the panel stays as-is).
 */
export function useOverlayPresence() {
  const context = useContext(OverlayContext);
  if (!context) return null;
  return { hasOpenOverlay: context.hasOpenOverlay, openOverlays: context.openOverlays };
}
