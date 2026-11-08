/**
 * In-flight assistant bubble — meta + empty body + steering inserts + live trace +
 * live plan checklist (Codex update_plan 风格，状态由 harness 的 trace 推导).
 * Same shape as a finished message so completion fills the body without jitter.
 */
import { formatDateTime } from '../../utils/date';
import { CopilotThinkingCard } from './CopilotMessageCards';
import { readPlanProgress, type CopilotPlanProgress } from './copilotTraceUi';

interface CopilotStreamingBubbleProps {
  isSending: boolean;
  streamStartedAt: string;
  steeredTurnTexts: string[];
  liveTrace: Parameters<typeof CopilotThinkingCard>[0]['steps'];
}

function PlanProgressList({ progress }: { progress: CopilotPlanProgress }) {
  return (
    <ol className="copilot-plan-progress" aria-label="Plan progress">
      {progress.steps.map((step, index) => {
        const isDone = index < progress.current;
        const isCurrent = index === progress.current;
        return (
          <li
            key={`${index}-${step.slice(0, 32)}`}
            className={`copilot-plan-step${isDone ? ' is-done' : ''}${isCurrent ? ' is-current' : ''}`}
            aria-current={isCurrent ? 'step' : undefined}
          >
            <span className="copilot-plan-step-mark" aria-hidden="true">{isDone ? '✓' : isCurrent ? '▶' : '○'}</span>
            <span className="copilot-plan-step-text">{step}</span>
          </li>
        );
      })}
    </ol>
  );
}

export function CopilotStreamingBubble(p: CopilotStreamingBubbleProps) {
  if (!p.isSending) return null;
  const progress = readPlanProgress(p.liveTrace);
  return (
    <article className="copilot-message is-assistant">
      <div className="copilot-message-meta">
        <strong>V-Bio Copilot</strong>
        <span>{formatDateTime(p.streamStartedAt)}</span>
      </div>
      <div className="copilot-message-body copilot-thinking" />
      {progress ? <PlanProgressList progress={progress} /> : null}
      {p.steeredTurnTexts.length > 0 ? (
        <div className="copilot-steered-list" aria-label="Inserted steering messages">
          {p.steeredTurnTexts.map((text, index) => (
            <div className="copilot-steered-item" key={`${index}-${text.slice(0, 24)}`}>
              <span className="copilot-steered-badge">Inserted</span>
              <span className="copilot-steered-text">{text}</span>
            </div>
          ))}
        </div>
      ) : null}
      <CopilotThinkingCard steps={p.liveTrace} isLive />
    </article>
  );
}
