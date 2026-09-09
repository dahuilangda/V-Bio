/**
 * Recent command history — extracted from ApiAccessPage CommandPanel.
 * Shares clipboard state via useCommandClipboard (parent hook).
 */
import { Check, Copy } from 'lucide-react';
import { formatIso } from './apiAccessHelpers';
import type { CommandHistoryEntry } from './apiAccessHelpers';

interface CommandHistoryProps {
  history: CommandHistoryEntry[];
  copiedId: string;
  onCopy: (text: string, ok: string, label?: string, id?: string) => void;
  onApply: (entry: CommandHistoryEntry) => void;
  onClear: () => void;
}

export function CommandHistory(p: CommandHistoryProps) {
  return (
<section className="api-command-history">
  <div className="api-command-history-head">
    <h3>Recent Command History</h3>
    <button className="btn btn-ghost" type="button" onClick={() => p.onClear()} disabled={p.history.length === 0}>
      Clear
    </button>
  </div>
  {p.history.length === 0 ? (
    <div className="muted small">No history yet. Copy any command to add it here.</div>
  ) : (
    <div className="api-history-list">
      {p.history.map((entry) => (
        <div key={entry.id} className="api-history-item">
          <div className="api-history-item-main">
            <strong>{entry.label}</strong>
            <span className="muted small">
              {entry.projectName || '-'} · {entry.workflow}/{entry.backend || '-'} · {entry.tokenName || '-'} · {formatIso(entry.createdAt)}
            </span>
          </div>
          <div className="api-history-item-actions">
            <button className="icon-btn" type="button" aria-label="Use command context" onClick={() => p.onApply(entry)}>
              <Check size={14} />
            </button>
            <button className={`icon-btn ${p.copiedId === `copy-history-${entry.id}` ? 'is-copied' : ''}`} type="button" aria-label="Copy command from history" onClick={() => { void p.onCopy(entry.command, 'History command copied.', undefined, `copy-history-${entry.id}`); }}>
              <Copy size={14} />
            </button>
          </div>
        </div>
      ))}
    </div>
  )}
</section>
  );
}
