/**
 * In-flight assistant bubble — meta + empty body + steering inserts + live trace.
 * Same shape as a finished message so completion fills the body without jitter.
 */
import { formatDateTime } from '../../utils/date';
import { CopilotThinkingCard } from './CopilotMessageCards';

interface CopilotStreamingBubbleProps {
  isSending: boolean;
  streamStartedAt: string;
  steeredTurnTexts: string[];
  liveTrace: Parameters<typeof CopilotThinkingCard>[0]['steps'];
}

export function CopilotStreamingBubble(p: CopilotStreamingBubbleProps) {
  if (!p.isSending) return null;
  return (
    <article className="copilot-message is-assistant">
      <div className="copilot-message-meta">
        <strong>V-Bio Copilot</strong>
        <span>{formatDateTime(p.streamStartedAt)}</span>
      </div>
      <div className="copilot-message-body copilot-thinking" />
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
