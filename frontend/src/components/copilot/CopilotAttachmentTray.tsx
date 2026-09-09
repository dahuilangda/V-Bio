/**
 * Composer attachment tray — file chips with insert/remove actions.
 * Zero coupling: array + 2 callbacks (agent plan cut #7).
 */
import { X } from 'lucide-react';
import type { CopilotUploadedAttachment } from './ProjectCopilotModal';

interface CopilotAttachmentTrayProps {
  attachments: CopilotUploadedAttachment[];
  onInsert: (attachment: CopilotUploadedAttachment) => void;
  onRemove: (id: string) => void;
}

export function CopilotAttachmentTray(p: CopilotAttachmentTrayProps) {
  if (p.attachments.length === 0) return null;
  return (
    <div className="copilot-attachment-tray" aria-label="Attached files">
      {p.attachments.map((attachment) => (
        <button
          className="copilot-attachment-chip"
          type="button"
          key={attachment.id}
          onClick={() => p.onInsert(attachment)}
          title={`Insert @${attachment.name}`}
        >
          <span className="copilot-attachment-name">{attachment.name}</span>
          <small>{Math.max(1, Math.round(attachment.size / 1024))} KB</small>
          <span
            className="copilot-attachment-remove"
            role="button"
            tabIndex={0}
            aria-label={`Remove ${attachment.name}`}
            onClick={(event) => {
              event.stopPropagation();
              p.onRemove(attachment.id);
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') {
                event.preventDefault();
                event.stopPropagation();
                p.onRemove(attachment.id);
              }
            }}
          >
            <X size={11} />
          </span>
        </button>
      ))}
    </div>
  );
}
