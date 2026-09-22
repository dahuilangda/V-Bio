/**
 * Chat session history sidebar.
 * Stateless; sessions are computed by the parent.
 */
import { MessageSquarePlus, Trash2 } from 'lucide-react';
import { formatDateTime } from '../../utils/date';

export interface CopilotSessionSummary {
  id: string;
  title: string;
  updatedAt: string;
}

interface CopilotHistorySidebarProps {
  sessions: CopilotSessionSummary[];
  activeSessionId: string;
  onSelect: (sessionId: string) => void;
  deleteAction: (sessionId: string) => void;
  onNewChat: () => void;
}

export function CopilotHistorySidebar(p: CopilotHistorySidebarProps) {
  return (
          <aside className="copilot-history">
            <div className="copilot-history-head">
              <span className="copilot-history-label">Chats</span>
              <button type="button" className="copilot-history-new" onClick={p.onNewChat} title="New chat">
                <MessageSquarePlus size={14} />
              </button>
            </div>
            <div className="copilot-history-list">
              {p.sessions.length === 0 ? (
                <div className="copilot-history-empty">No previous chats</div>
              ) : (
                p.sessions.map((session) => (
                  <div className={`copilot-history-item${session.id === p.activeSessionId ? ' active' : ''}`} key={session.id}>
                    <button type="button" className="copilot-history-btn" onClick={() => p.onSelect(session.id)}>
                      <span className="copilot-history-title">{session.title}</span>
                      {session.updatedAt ? (
                        <small className="copilot-history-time">{formatDateTime(session.updatedAt)}</small>
                      ) : null}
                    </button>
                    <button
                      className="copilot-history-delete"
                      type="button"
                      onClick={() => void p.deleteAction(session.id)}
                      aria-label="Delete chat"
                      title="Delete chat"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))
              )}
            </div>
          </aside>

  );
}
