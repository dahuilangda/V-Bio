import { useLayoutEffect, type RefObject } from 'react';

export function shrinkToFitScale(contentWidth: number, availableWidth: number): number {
  if (contentWidth <= 0 || availableWidth <= 0 || contentWidth <= availableWidth) return 1;
  return Math.floor((availableWidth / contentWidth) * 1000) / 1000;
}

export function useShrinkToFit<T extends HTMLElement>(ref: RefObject<T | null>, content: unknown): void {
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fit = () => {
      el.style.transform = '';
      const contentWidth = Array.from(el.children).reduce((sum, child) => sum + child.getBoundingClientRect().width, 0);
      const scale = shrinkToFitScale(contentWidth, el.clientWidth);
      el.style.transform = scale < 1 ? `scale(${scale})` : '';
    };
    fit();
    const observer = new ResizeObserver(fit);
    observer.observe(el);
    void document.fonts.ready.then(fit);
    return () => observer.disconnect();
  }, [ref, content]);
}
