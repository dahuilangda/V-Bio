import { useCallback, useEffect, useRef, useState } from 'react';
import { bootstrapViewerHost } from '../bootstrap';
import { subscribePickEvents } from '../pick';
import type { MolstarResiduePick } from '../types';

interface UseMolstarBootstrapArgs {
  showSequence: boolean;
  interactionGranularity: 'residue' | 'element';
  onResiduePick?: (pick: MolstarResiduePick) => void;
  pickMode: 'click' | 'alt-left';
}

function tryEnableSelection(viewer: any) {
  try {
    const shouldEnableSelection = true;
    if (typeof viewer?.setSelectionMode === 'function') {
      viewer.setSelectionMode(shouldEnableSelection);
    } else if ('selectionMode' in (viewer || {})) {
      viewer.selectionMode = shouldEnableSelection;
    } else {
      viewer?.plugin?.behaviors?.interaction?.selectionMode?.next?.(shouldEnableSelection);
    }
  } catch {
    // no-op
  }
}

export function useMolstarBootstrap({
  showSequence,
  interactionGranularity,
  onResiduePick,
  pickMode
}: UseMolstarBootstrapArgs) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const viewerRef = useRef<any>(null);
  const cleanupRef = useRef<(() => void) | null>(null);
  const pickUnsubscribeRef = useRef<(() => void) | null>(null);
  const onResiduePickRef = useRef<typeof onResiduePick>(onResiduePick);
  const suppressPickEventsRef = useRef(false);
  const altPressedRef = useRef(false);
  const shiftPressedRef = useRef(false);
  const ctrlPressedRef = useRef(false);
  const recentModifiedPrimaryDownRef = useRef(0);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bootstrapMs, setBootstrapMs] = useState<number | null>(null);
  const hasResiduePickHandler = Boolean(onResiduePick);

  useEffect(() => {
    onResiduePickRef.current = onResiduePick;
  }, [onResiduePick]);

  const emitResiduePick = useCallback((pick: MolstarResiduePick) => {
    onResiduePickRef.current?.(pick);
  }, []);

  const isModifierPick = useCallback(() => {
    return (
      altPressedRef.current ||
      shiftPressedRef.current ||
      ctrlPressedRef.current ||
      Date.now() - recentModifiedPrimaryDownRef.current < 450
    );
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey || event.key === 'Alt') {
        altPressedRef.current = true;
      }
      if (event.shiftKey || event.key === 'Shift') {
        shiftPressedRef.current = true;
      }
      if (event.ctrlKey || event.metaKey || event.key === 'Control' || event.key === 'Meta') {
        ctrlPressedRef.current = true;
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (!event.altKey || event.key === 'Alt') {
        altPressedRef.current = false;
      }
      if (!event.shiftKey || event.key === 'Shift') {
        shiftPressedRef.current = false;
      }
      if ((!event.ctrlKey && !event.metaKey) || event.key === 'Control' || event.key === 'Meta') {
        ctrlPressedRef.current = false;
      }
    };
    const onPointerDown = (event: MouseEvent | PointerEvent) => {
      const isPrimary =
        event.button === 0 ||
        event.which === 1 ||
        (typeof event.buttons === 'number' && (event.buttons & 1) === 1);
      if (isPrimary && (event.altKey || event.shiftKey || event.ctrlKey || event.metaKey)) {
        recentModifiedPrimaryDownRef.current = Date.now();
      }
    };
    const onBlur = () => {
      altPressedRef.current = false;
      shiftPressedRef.current = false;
      ctrlPressedRef.current = false;
      recentModifiedPrimaryDownRef.current = 0;
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('pointerdown', onPointerDown, { capture: true, passive: true });
    window.addEventListener('blur', onBlur);
    document.addEventListener('visibilitychange', onBlur);

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('pointerdown', onPointerDown, { capture: true });
      window.removeEventListener('blur', onBlur);
      document.removeEventListener('visibilitychange', onBlur);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const bootstrap = async () => {
      try {
        if (cancelled || !hostRef.current) return;

        const bootStart = typeof performance !== 'undefined' ? performance.now() : Date.now();
        const bootstrappedViewer = await bootstrapViewerHost(hostRef.current, showSequence);
        if (cancelled) {
          // Unmount raced the bootstrap: the cleanup below already ran while viewerRef was
          // still null, so its dispose was a no-op. Dispose here or the plugin leaks — every
          // fast page switch that lands in this window strands one WebGL context, and a
          // phone caps those at ~8-16 before the whole page freezes.
          try {
            bootstrappedViewer?.plugin?.dispose?.();
          } catch {
            // no-op
          }
          return;
        }
        viewerRef.current = bootstrappedViewer;
        const bootEnd = typeof performance !== 'undefined' ? performance.now() : Date.now();
        setBootstrapMs(Math.round(bootEnd - bootStart));
        try {
          viewerRef.current?.plugin?.managers?.interactivity?.setProps?.({ granularity: interactionGranularity });
        } catch {
          // no-op
        }
        tryEnableSelection(viewerRef.current);
        pickUnsubscribeRef.current = subscribePickEvents(
          viewerRef.current,
          hasResiduePickHandler ? emitResiduePick : undefined,
          pickMode,
          isModifierPick,
          () => suppressPickEventsRef.current,
          false
        );
        setReady(true);

        // Watch for container size changes (e.g. CSS media queries on mobile) and tell Mol* to
        // re-layout. Without this, the Mol* canvas keeps its initial desktop size when the
        // container shrinks, making the structure invisible on mobile.
        const resizeTarget = hostRef.current;
        let resizeTimer: number | null = null;
        const doResize = () => {
          if (resizeTimer !== null) window.clearTimeout(resizeTimer);
          resizeTimer = window.setTimeout(() => {
            resizeTimer = null;
            try {
              const plugin = viewerRef.current?.plugin;
              if (!plugin) return;
              // Force Mol* to recalculate canvas dimensions from the host element's current size.
              // handleResize reads layout.root.offsetWidth/Height and resizes the WebGL canvas.
              if (plugin.canvas3d && typeof plugin.canvas3d.handleResize === 'function') {
                plugin.canvas3d.handleResize();
              }
              if (plugin.canvas3d && typeof plugin.canvas3d.requestResize === 'function') {
                plugin.canvas3d.requestResize();
              }
              // Re-trigger layout in case panels need to adjust.
              if (plugin.layout?.events?.update) {
                plugin.layout.events.update.next(plugin.layout.current);
              }
            } catch {
              // no-op — resize is best-effort
            }
          }, 100);
        };
        const resizeObserver = new ResizeObserver(doResize);
        if (resizeTarget) {
          resizeObserver.observe(resizeTarget);
        }
        // Fire an initial resize after a short delay — Mol* may have initialized before CSS
        // media queries applied the correct container size (especially on mobile).
        window.setTimeout(doResize, 200);
        window.setTimeout(doResize, 800);
        // Store cleanup on the cancel flag's closure
        const originalCleanup = cleanupRef.current;
        cleanupRef.current = () => {
          originalCleanup?.();
          resizeObserver.disconnect();
          if (resizeTimer !== null) window.clearTimeout(resizeTimer);
        };
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Unable to load Mol* viewer.');
      }
    };

    void bootstrap();

    return () => {
      cancelled = true;
      if (cleanupRef.current) {
        cleanupRef.current();
        cleanupRef.current = null;
      }
      const viewer = viewerRef.current;
      if (pickUnsubscribeRef.current) {
        pickUnsubscribeRef.current();
        pickUnsubscribeRef.current = null;
      }
      if (viewer?.plugin?.dispose) {
        viewer.plugin.dispose();
      }
      viewerRef.current = null;
      setReady(false);
    };
  }, [emitResiduePick, hasResiduePickHandler, interactionGranularity, isModifierPick, pickMode, showSequence]);

  useEffect(() => {
    if (!viewerRef.current) return;
    try {
      viewerRef.current?.plugin?.managers?.interactivity?.setProps?.({ granularity: interactionGranularity });
    } catch {
      // no-op
    }
  }, [interactionGranularity]);

  useEffect(() => {
    if (!viewerRef.current) return;
    tryEnableSelection(viewerRef.current);
    if (pickUnsubscribeRef.current) {
      pickUnsubscribeRef.current();
      pickUnsubscribeRef.current = null;
    }
    pickUnsubscribeRef.current = subscribePickEvents(
      viewerRef.current,
      hasResiduePickHandler ? emitResiduePick : undefined,
      pickMode,
      isModifierPick,
      () => suppressPickEventsRef.current,
      false
    );
    return () => {
      if (pickUnsubscribeRef.current) {
        pickUnsubscribeRef.current();
        pickUnsubscribeRef.current = null;
      }
    };
  }, [emitResiduePick, hasResiduePickHandler, isModifierPick, pickMode]);

  return {
    hostRef,
    viewerRef,
    ready,
    error,
    setError,
    suppressPickEventsRef,
    bootstrapMs
  };
}
