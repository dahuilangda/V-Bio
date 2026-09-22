/** Shared wrapper for the standard label.field pattern. */
import type { ReactNode } from 'react';
import { InfoTip } from './InfoTip';

interface FieldProps {
  label: ReactNode;
  /** Optional tooltip text (renders InfoTip next to the label). */
  hint?: string;
  /** InfoTip bubble alignment (use "end" when the field hugs the right edge). */
  hintAlign?: 'center' | 'start' | 'end';
  children: ReactNode;
}

export function Field({ label, hint, hintAlign = 'start', children }: FieldProps) {
  return (
    <label className="field">
      <span>
        {label}
        {hint ? <InfoTip text={hint} align={hintAlign} /> : null}
      </span>
      {children}
    </label>
  );
}
