import { useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import type { RefObject } from 'react';

interface UseResizablePaneOptions {
  storageKey: string;
  defaultWidth: number;
  minWidth: number;
  maxWidth: number;
  minAsideWidth: number;
  mediaQuery?: string;
  keyStep?: number;
  handleWidth?: number;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

export function useResizablePane(options: UseResizablePaneOptions): {
  mainWidth: number;
  isResizing: boolean;
  containerRef: RefObject<HTMLDivElement>;
  onResizerPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
} {
  const {
    storageKey,
    defaultWidth,
    minWidth,
    maxWidth,
    minAsideWidth,
    mediaQuery = '(max-width: 1100px)',
    keyStep = 1.5,
    handleWidth = 10,
  } = options;

  const [mainWidth, setMainWidth] = useState<number>(() => {
    if (typeof window === 'undefined') return defaultWidth;
    const savedRaw = window.localStorage.getItem(storageKey);
    const saved = savedRaw ? Number.parseFloat(savedRaw) : Number.NaN;
    if (!Number.isFinite(saved)) return defaultWidth;
    return clamp(saved, minWidth, maxWidth);
  });
  const [isResizing, setIsResizing] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const resizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(storageKey, mainWidth.toFixed(2));
  }, [storageKey, mainWidth]);

  useEffect(() => {
    if (!isResizing) return;

    const onPointerMove = (event: globalThis.PointerEvent) => {
      const state = resizeRef.current;
      const container = containerRef.current;
      if (!state || !container) return;

      const containerWidth = container.getBoundingClientRect().width;
      if (!Number.isFinite(containerWidth) || containerWidth <= 0) return;

      const minMainPx = containerWidth * (minWidth / 100);
      const maxMainPxByPct = containerWidth * (maxWidth / 100);
      const maxMainPxByAside = containerWidth - minAsideWidth - handleWidth;
      const maxMainPx = Math.min(maxMainPxByPct, maxMainPxByAside);
      if (maxMainPx <= minMainPx) return;

      const startMainPx = (state.startWidthPercent / 100) * containerWidth;
      const nextMainPx = startMainPx + (event.clientX - state.startX);
      const clampedMainPx = Math.min(maxMainPx, Math.max(minMainPx, nextMainPx));
      const nextPercent = (clampedMainPx / containerWidth) * 100;
      setMainWidth(clamp(nextPercent, minWidth, maxWidth));
    };

    const stopResizing = () => {
      setIsResizing(false);
      resizeRef.current = null;
    };

    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', stopResizing);
    window.addEventListener('pointercancel', stopResizing);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', stopResizing);
      window.removeEventListener('pointercancel', stopResizing);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isResizing, minAsideWidth, minWidth, maxWidth, handleWidth]);

  const onResizerPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      if (window.matchMedia(mediaQuery).matches) return;
      const container = containerRef.current;
      if (!container) return;

      resizeRef.current = {
        startX: event.clientX,
        startWidthPercent: mainWidth,
      };
      setIsResizing(true);
      event.preventDefault();
    },
    [mainWidth, mediaQuery]
  );

  const onResizerKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        setMainWidth((current) => clamp(current - keyStep, minWidth, maxWidth));
        return;
      }
      if (event.key === 'ArrowRight') {
        event.preventDefault();
        setMainWidth((current) => clamp(current + keyStep, minWidth, maxWidth));
        return;
      }
      if (event.key === 'Home') {
        event.preventDefault();
        setMainWidth(defaultWidth);
      }
    },
    [defaultWidth, keyStep, minWidth, maxWidth]
  );

  return {
    mainWidth,
    isResizing,
    containerRef: containerRef as RefObject<HTMLDivElement>,
    onResizerPointerDown,
    onResizerKeyDown,
  };
}
