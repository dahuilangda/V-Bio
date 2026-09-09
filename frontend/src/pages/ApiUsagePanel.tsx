/**
 * API usage analytics panel — extracted from ApiAccessPage.
 * All 20 external references parameterized (values/callbacks/derived).
 */
import { BarChart3, ChevronLeft, ChevronRight, KeyRound, Plus } from 'lucide-react';
import { formatIso, type UsageSummary } from './apiAccessHelpers';
import type { ApiToken, ApiTokenUsage, ApiTokenUsageDaily } from '../types/models';

interface UsagePanelProps {
  tokens: ApiToken[];
  selectedTokenId: string;
  selectedProjectTokens: ApiToken[];
  tokenUsage: ApiTokenUsage[];
  tokenUsageDaily: ApiTokenUsageDaily[];
  tokenUsageTotal: number;
  selectedTokenUsageSummary: UsageSummary | null;
  eventPage: number;
  eventPageCount: number;
  usageBarsPage: number;
  usageBarsPageCount: number;
  pagedDailyUsage: ApiTokenUsageDaily[];
  maxDailyCount: number;
  onEventPageChange: (p: number) => void;
  onUsageBarsPageChange: (p: number) => void;
  onSelectedTokenIdChange: (id: string) => void;
  onOpenRegistry: () => void;
}

export function UsagePanel(p: UsagePanelProps) {
  return (
<section className="panel api-usage-panel">
  <div className="api-section-head">
    <h2><BarChart3 size={16} /> Usage</h2>
    <div className="api-usage-controls">
      <label className="api-token-inline" aria-label="Usage token">
        <span className="api-token-inline-label"><KeyRound size={12} /> Token</span>
        <select
          value={p.selectedTokenId}
          onChange={(e) => p.onSelectedTokenIdChange(e.target.value)}
          disabled={p.tokens.length === 0}
        >
          {p.selectedProjectTokens.length === 0 ? (
            <option value="">No p.tokens</option>
          ) : (
            p.selectedProjectTokens.map((token) => (
              <option key={token.id} value={token.id}>
                {token.name} ({token.token_prefix}...{token.token_last4})
              </option>
            ))
          )}
        </select>
      </label>
      <div className="api-builder-meta api-usage-meta">
        <span className="badge">Calls {p.selectedTokenUsageSummary?.total}</span>
        <span className="badge">Success {p.selectedTokenUsageSummary?.successRate.toFixed(1)}%</span>
      </div>
    </div>
  </div>

  {!p.selectedTokenId ? (
    <div className="api-empty-state">
      <p className="muted">No token selected.</p>
      <button className="btn btn-primary" type="button" onClick={() => p.onOpenRegistry()}>
        <Plus size={14} /> New Token
      </button>
    </div>
  ) : (
    <>
      <div className="api-usage-bars">
        <div className="api-usage-bars-head">
          <span className="muted small">Daily traffic</span>
          {p.usageBarsPageCount > 1 && (
            <div className="api-pager">
              <button
                type="button"
                className="icon-btn"
                onClick={() => p.onUsageBarsPageChange(Math.max(1, p.usageBarsPage - 1))}
                disabled={p.usageBarsPage <= 1}
                title="Previous daily page"
                aria-label="Previous daily page"
              >
                <ChevronLeft size={14} />
              </button>
              <span className="muted small">{p.usageBarsPage} / {p.usageBarsPageCount}</span>
              <button
                type="button"
                className="icon-btn"
                onClick={() => p.onUsageBarsPageChange(Math.min(p.usageBarsPageCount, p.usageBarsPage + 1))}
                disabled={p.usageBarsPage >= p.usageBarsPageCount}
                title="Next daily page"
                aria-label="Next daily page"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          )}
        </div>
        {p.tokenUsageDaily.length === 0 ? (
          <div className="muted">No usage data.</div>
        ) : (
          p.pagedDailyUsage.map((item) => {
            const width = Math.max(4, (item.total_count / p.maxDailyCount) * 100);
            return (
              <div className="api-usage-bar-row" key={`${item.token_id}-${item.usage_day}`}>
                <span className="api-usage-day">{item.usage_day}</span>
                <div className="api-usage-bar-track">
                  <span className="api-usage-bar-fill" style={{ width: `${width}%` }} />
                </div>
                <span className="api-usage-count">{item.total_count}</span>
              </div>
            );
          })
        )}
      </div>

      <div className="table-wrap api-usage-table-wrap">
        <table className="table api-usage-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Action</th>
              <th>Status</th>
              <th>Path</th>
            </tr>
          </thead>
          <tbody>
            {p.tokenUsage.map((item) => (
              <tr key={item.id}>
                <td>{formatIso(item.created_at)}</td>
                <td>{item.action || `${item.method} ${item.path}`}</td>
                <td>{item.succeeded ? 'OK' : `Error (${item.status_code})`}</td>
                <td><code>{item.path}</code></td>
              </tr>
            ))}
            {p.tokenUsageTotal === 0 && (
              <tr>
                <td colSpan={4} className="muted">No events.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {p.eventPageCount > 1 && (
        <div className="api-pager">
          <button
            type="button"
            className="icon-btn"
            onClick={() => p.onEventPageChange(Math.max(1, p.eventPage - 1))}
            disabled={p.eventPage <= 1}
            title="Previous page"
            aria-label="Previous page"
          >
            <ChevronLeft size={14} />
          </button>
          <span className="muted small">{p.eventPage} / {p.eventPageCount}</span>
          <button
            type="button"
            className="icon-btn"
            onClick={() => p.onEventPageChange(Math.min(p.eventPageCount, p.eventPage + 1))}
            disabled={p.eventPage >= p.eventPageCount}
            title="Next page"
            aria-label="Next page"
          >
            <ChevronRight size={14} />
          </button>
        </div>
      )}
    </>
  )}
</section>
  );
}
