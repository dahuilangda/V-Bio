import { useCallback, useEffect, useRef } from 'react';
import { Info } from 'lucide-react';

const TIP_VIEWPORT_MARGIN = 8;

interface InfoTipProps {
  /** Tooltip content shown on hover/focus. Keep it to one short sentence. */
  text: string;
  /** Optional visible label rendered after the icon. */
  label?: string;
  /** Icon size in px. */
  size?: number;
  /** Bubble alignment relative to the trigger. */
  align?: 'center' | 'start' | 'end';
}

export function tooltipHorizontalShift(left: number, right: number, viewportWidth: number): number {
  if (right > viewportWidth - TIP_VIEWPORT_MARGIN) return Math.round(viewportWidth - TIP_VIEWPORT_MARGIN - right);
  if (left < TIP_VIEWPORT_MARGIN) return Math.round(TIP_VIEWPORT_MARGIN - left);
  return 0;
}

export function InfoTip({ text, label, size = 13, align = 'center' }: InfoTipProps) {
  const bubbleRef = useRef<HTMLSpanElement | null>(null);

  const keepBubbleInViewport = useCallback(() => {
    const bubble = bubbleRef.current;
    if (!bubble) return;
    bubble.style.translate = '0px';
    const rect = bubble.getBoundingClientRect();
    bubble.style.translate = `${tooltipHorizontalShift(rect.left, rect.right, document.documentElement.clientWidth)}px`;
  }, []);

  useEffect(() => {
    keepBubbleInViewport();
    window.addEventListener('resize', keepBubbleInViewport);
    return () => window.removeEventListener('resize', keepBubbleInViewport);
  }, [keepBubbleInViewport]);

  const alignClass =
    align === 'start' ? ' info-tip-bubble--start' : align === 'end' ? ' info-tip-bubble--end' : '';
  return (
    <span className="info-tip" onPointerEnter={keepBubbleInViewport} onFocus={keepBubbleInViewport}>
      <span
        className="info-tip-trigger"
        tabIndex={0}
        role="note"
        aria-label={text}
      >
        <Info size={size} aria-hidden="true" />
      </span>
      {label ? <span className="info-tip-label">{label}</span> : null}
      <span role="tooltip" ref={bubbleRef} className={`info-tip-bubble${alignClass}`}>{text}</span>
    </span>
  );
}
