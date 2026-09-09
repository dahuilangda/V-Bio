/**
 * Shared form field wrapper — standardizes the label.field pattern
 * used 116+ times across 14 files (audit finding #3a).
 * Eliminates copy-pasted <label className="field"><span>...</span>...</label>.
 */
import type { ReactNode } from 'react';
import { InfoTip } from './InfoTip';

interface FieldProps {
  label: ReactNode;
  /** Optional tooltip text (renders InfoTip next to the label). */
  hint?: string;
  /** InfoTip bubble alignment (use "end" when the field hugs the right edge). */
  hintAlign?: 'center' | 'start' | 'end';
  /** Compact variant for inline rows. */
  tight?: boolean;
  disabled?: boolean;
  children: ReactNode;
}

export function Field({ label, hint, hintAlign = 'start', tight = false, disabled = false, children }: FieldProps) {
  return (
    <label className={`field${tight ? ' field-tight' : ''}${disabled ? ' field-disabled' : ''}`}>
      <span>
        {label}
        {hint ? <InfoTip text={hint} align={hintAlign} /> : null}
      </span>
      {children}
    </label>
  );
}
