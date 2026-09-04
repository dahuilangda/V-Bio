import { Info } from 'lucide-react';

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

/**
 * Info circle with a hover/focus tooltip (pure CSS reveal, zero JS cost).
 * Inline explanatory copy should become one of these instead of visible text,
 * keeping panels compact.
 */
export function InfoTip({ text, label, size = 13, align = 'center' }: InfoTipProps) {
  const alignClass =
    align === 'start' ? ' info-tip-bubble--start' : align === 'end' ? ' info-tip-bubble--end' : '';
  return (
    <span className="info-tip">
      <span
        className="info-tip-trigger"
        tabIndex={0}
        role="note"
        aria-label={text}
      >
        <Info size={size} aria-hidden="true" />
      </span>
      {label ? <span className="info-tip-label">{label}</span> : null}
      <span role="tooltip" className={`info-tip-bubble${alignClass}`}>{text}</span>
    </span>
  );
}
