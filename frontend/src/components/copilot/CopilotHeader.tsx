/**
 * Copilot panel header — title + actions + drag handle.
 * Drag handlers stay in the parent (they own position state); this is the
 * JS-moves-out controlled pattern (agent plan cut #6).
 */
import type { PointerEvent as ReactPointerEvent } from 'react';
import { MessageSquarePlus, MessageSquareText, PanelLeft, Settings, X } from 'lucide-react';

interface CopilotHeaderProps {
  title: string;
  subtitle: string;
  isAdmin: boolean;
  historyOpen: boolean;
  onToggleHistory: () => void;
  onNewChat: () => void;
  onOpenSettings: () => void;
  onClose: () => void;
  onDragStart: (e: ReactPointerEvent<HTMLDivElement>) => void;
  onDragMove: (e: ReactPointerEvent<HTMLDivElement>) => void;
  onDragEnd: (e: ReactPointerEvent<HTMLDivElement>) => void;
}

export function CopilotHeader(p: CopilotHeaderProps) {
  return (
    <div
      className={`copilot-head copilot-drag-handle${p.historyOpen ? ' history-open' : ''}`}
      onPointerDown={p.onDragStart}
      onPointerMove={p.onDragMove}
      onPointerUp={p.onDragEnd}
      onPointerCancel={p.onDragEnd}
    >
      <div className="copilot-title">
        <MessageSquareText size={18} />
        <div>
          <h2>{p.title}</h2>
          <span>{p.subtitle}</span>
        </div>
      </div>
      <div className="copilot-head-actions">
        <button
          className="task-row-action-btn"
          type="button"
          onClick={p.onToggleHistory}
          aria-label="Chat history"
          title="Chat history"
        >
          <PanelLeft size={15} />
        </button>
        <button className="task-row-action-btn" type="button" onClick={p.onNewChat} aria-label="New chat" title="New chat">
          <MessageSquarePlus size={15} />
        </button>
        {p.isAdmin ? (
          <button className="task-row-action-btn" type="button" onClick={p.onOpenSettings} aria-label="Copilot settings" title="Copilot settings">
            <Settings size={15} />
          </button>
        ) : null}
        <button className="task-row-action-btn" type="button" onClick={p.onClose} aria-label="Close Copilot" title="Close">
          <X size={15} />
        </button>
      </div>
    </div>
  );
}
