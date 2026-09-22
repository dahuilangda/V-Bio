/**
 * Plan-action confirmation card — one progressive-reveal step at a time.
 * Pure display, zero local state.
 */
import type { CopilotPlanAction } from '../../types/models';
import { Check, LoaderCircle, X } from 'lucide-react';
import { planActionKey, formatActionSummary } from './copilotPlanHelpers';

interface CopilotPlanActionCardProps {
  action: CopilotPlanAction;
  isApplying: boolean;
  isBlocked: boolean;
  bulkAction: 'apply' | 'cancel' | null;
  applyAction: (action: CopilotPlanAction) => void;
  cancelAction: () => void;
}

export function CopilotPlanActionCard(p: CopilotPlanActionCardProps) {
  const actionKey = planActionKey(p.action);
  const summary = formatActionSummary(p.action);
  const isDestructive = p.action.payload?.destructive === true;
  return (

            <div className="copilot-action-stack" aria-label="Pending confirmation step">
              {/* The pending step has NOT executed, whatever the message above says. */}
              <div className="copilot-plan-pending-hint">
                Not run yet — it takes effect only after you click Apply; the returned receipt is the actual result.
              </div>
              <div className="copilot-plan-actions">
                <div
                  className={`copilot-plan-action${isDestructive ? ' is-destructive' : ''}${p.isApplying ? ' is-applying' : ''}`}
                  key={actionKey}
                >
                  <div className="copilot-plan-action-main">
                    <strong>{p.action.label}</strong>
                    <small className="copilot-plan-action-desc">{p.action.description}</small>
                    {summary.length > 0 ? (
                      <dl className="copilot-plan-action-summary">
                        {summary.map((entry) => (
                          <div className="copilot-plan-action-summary-row" key={entry.label}>
                            <dt>{entry.label}</dt>
                            <dd>{entry.value}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                  </div>
                  <div className="copilot-plan-action-buttons">
                    <button
                      className="copilot-plan-action-cancel"
                      type="button"
                      onClick={() => void p.cancelAction()}
                      disabled={Boolean(p.isApplying || p.bulkAction)}
                      title="Cancel"
                    >
                      {p.bulkAction === 'cancel' ? <LoaderCircle size={14} className="spin" /> : <X size={14} />}
                      <span>Cancel</span>
                    </button>
                    <button
                      className="copilot-plan-action-apply"
                      type="button"
                      onClick={() => void p.applyAction(p.action)}
                      disabled={p.isBlocked}
                      title="Apply this step"
                    >
                      {p.isApplying ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
                      <span>{p.isApplying ? 'Applying' : 'Apply'}</span>
                    </button>
                  </div>
                </div>
              </div>
            </div>
          
  );
}
