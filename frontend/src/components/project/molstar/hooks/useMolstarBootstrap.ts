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
  const pickUnsubscribeRef = useRef<(() => void) | null>(null);
  const onResiduePickRef = useRef<typeof onResiduePick>(onResiduePick);
  const suppressPickEventsRef = useRef(false);
  const altPressedRef = useRef(false);
  const shiftPressedRef = useRef(false);
  const ctrlPressedRef = useRef(false);
  const recentModifiedPrimaryDownRef = useRef(0);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
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

        viewerRef.current = await bootstrapViewerHost(hostRef.current, showSequence);
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
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Unable to load Mol* viewer.');
      }
    };

    void bootstrap();

    return () => {
      cancelled = true;
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
    suppressPickEventsRef
  };
}
