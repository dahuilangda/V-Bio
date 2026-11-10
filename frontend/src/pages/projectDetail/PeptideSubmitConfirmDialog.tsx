import { useEffect, useState } from 'react';
import { InfoTip } from '../../components/common/InfoTip';
import { useModalDialog } from '../../components/ui/useModalDialog';
import { isValidNotifyEmail } from '../../utils/projectInputs';

export interface PeptideSubmitSummary {
  backend: string;
  proposerBackend?: string;
  designMode: string;
  chirality: string;
  binderLengthLabel: string;
  iterations: number;
  populationSize: number;
  eliteSize: number;
  hasPocket: boolean;
}

interface PeptideSubmitConfirmDialogProps {
  isOpen: boolean;
  /** True while the submission promise is in flight: dialog stays open, actions locked. */
  isSubmitting: boolean;
  /** Prefill for the optional completion-notification email. */
  defaultNotifyEmail: string;
  onCancel: () => void;
  onConfirm: (notifyEmail: string) => void;
  summary: PeptideSubmitSummary;
}

const BACKEND_LABELS: Record<string, string> = {
  protenix2dock: 'Protenix2Dock',
  alphafold3: 'AlphaFold3',
  protenix: 'Protenix',
  boltz: 'Boltz-2',
  nesso: 'Nesso-1',
  mol1: 'Mol-1',
  esm3: 'ESM3',
  rfd4: 'RFD4-Proteina',
};

function backendLabel(backend: string): string {
  return BACKEND_LABELS[backend.trim().toLowerCase()] || backend;
}

/** Two-step submit confirmation: parameter summary + resource estimate before consuming the GPU pool. */
export function PeptideSubmitConfirmDialog({
  isOpen,
  isSubmitting,
  defaultNotifyEmail,
  onCancel,
  onConfirm,
  summary
}: PeptideSubmitConfirmDialogProps) {
  const [notifyEmail, setNotifyEmail] = useState(defaultNotifyEmail);
  const estimatedTasks = summary.iterations * summary.populationSize;
  const dialog = useModalDialog(isOpen, isSubmitting ? () => {} : onCancel);

  useEffect(() => {
    if (!isOpen) return;
    setNotifyEmail(defaultNotifyEmail);
  }, [isOpen, defaultNotifyEmail]);

  if (!isOpen) return null;

  const rows: Array<[string, string]> = [
    ['Design engine', backendLabel(summary.proposerBackend || 'esm3')],
    ['Score engine', backendLabel(summary.backend)],
    ['Design mode', summary.designMode],
    ['Chirality', summary.chirality === 'd' ? 'D-peptide (mirror workflow)' : 'L-peptide'],
    ['Binder length', summary.binderLengthLabel],
    ['Generations × Population', `${summary.iterations} × ${summary.populationSize} (elite ${summary.eliteSize})`],
    ['Pocket guidance', summary.hasPocket ? 'Enabled' : 'None (free exploration)']
  ];

  return (
    <div className="modal-mask" role="dialog" aria-modal="true" aria-label="Confirm submission" onClick={isSubmitting ? undefined : onCancel}>
      <div
        className="modal modal-sm"
        onClick={(event) => event.stopPropagation()}
        {...dialog}
      >
        <h2>Review &amp; Submit</h2>
        <table className="peptide-submit-confirm-table">
          <tbody>
            {rows.map(([key, value]) => (
              <tr key={key}>
                <td className="muted">{key}</td>
                <td>{value}</td>
              </tr>
            ))}
            <tr>
              <td className="muted">Estimated tasks</td>
              <td>{estimatedTasks} candidate predictions</td>
            </tr>
            <tr>
              <td className="muted">Estimated time</td>
              <td>
                ~2–15 min per candidate on the GPU pool
                <span className="muted small"> (total depends on queue load and pool occupancy)</span>
              </td>
            </tr>
          </tbody>
        </table>
        {notifyEmail.trim().length > 0 && !isValidNotifyEmail(notifyEmail) ? (
          <p className="muted small" style={{ color: 'var(--danger)' }}>
            Enter a valid email address, or clear the field to skip notifications.
          </p>
        ) : null}
        <label className="field peptide-submit-confirm-email">
          <span>
            Notify email (optional)
            <InfoTip text="Send a completion email for this long-running job. Prefilled from your account." align="start" />
          </span>
          <input
            type="email"
            value={notifyEmail}
            onChange={(event) => setNotifyEmail(event.target.value)}
            placeholder="you@example.com"
            disabled={isSubmitting}
          />
        </label>
        <div className="peptide-submit-confirm-actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel} disabled={isSubmitting}>
            Back
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={() => onConfirm(notifyEmail.trim())}
            disabled={isSubmitting || (notifyEmail.trim().length > 0 && !isValidNotifyEmail(notifyEmail))}
          >
            {isSubmitting ? 'Submitting…' : 'Confirm & Submit'}
          </button>
        </div>
      </div>
    </div>
  );
}
