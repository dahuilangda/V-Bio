/**
 * Number input that only commits on blur/Enter — draft typing doesn't
 * trigger parent state updates (avoids re-render storms in large forms).
 * Extracted from WorkflowRuntimeSettingsSection for reuse (audit: was
 * file-private while 10+ files hand-rolled raw number inputs).
 */
import { useEffect, useState, type ChangeEvent, type FocusEvent } from 'react';

function clampCommittedNumber(value: number, minValue: number, maxValue: number, fallback: number, step?: number): number {
  const parsed = Number.isFinite(value) ? value : fallback;
  const clamped = Math.max(minValue, Math.min(maxValue, parsed));
  if (step && step > 0) return Number((Math.round(clamped / step) * step).toFixed(6));
  return Math.floor(clamped);
}

interface CommitNumberInputProps {
  value: number;
  min: number;
  max: number;
  step?: number;
  disabled?: boolean;
  onCommit: (value: number) => void;
}

export function CommitNumberInput({ value, min, max, step, disabled, onCommit }: CommitNumberInputProps) {
  const [draftValue, setDraftValue] = useState(String(value));

  useEffect(() => {
    setDraftValue(String(value));
  }, [value]);

  const commit = (rawValue: string) => {
    const next = clampCommittedNumber(Number(rawValue), min, max, value, step);
    setDraftValue(String(next));
    if (next !== value) onCommit(next);
  };

  return (
    <input
      type="number"
      min={min}
      max={max}
      step={step}
      value={draftValue}
      onChange={(event: ChangeEvent<HTMLInputElement>) => setDraftValue(event.target.value)}
      onBlur={(event: FocusEvent<HTMLInputElement>) => commit(event.target.value)}
      onKeyDown={(event) => {
        if (event.key !== 'Enter') return;
        commit(event.currentTarget.value);
        event.currentTarget.blur();
      }}
      disabled={disabled}
    />
  );
}
