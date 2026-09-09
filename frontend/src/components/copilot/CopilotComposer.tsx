/**
 * Chat composer of the copilot panel: attachment tray + @-mention menu +
 * plus-menu, ghost-completion overlay + picker, autogrow textarea and the
 * send/stop button.
 *
 * Composition follows the Vercel ai-chatbot MultimodalInput pattern: all
 * durable state stays in the modal (the keyboard map, mention caret math and
 * send flow live there), and this component is purely the input surface.
 * The ~30 values it needs arrive as five named prop groups — draft, mention,
 * completion, attachments, turn — instead of one flat list (react.dev: object
 * props vs individual props are equivalent; named groups document the slices
 * and keep the modal call site readable). No spread.
 */
import type { Dispatch, KeyboardEvent as ReactKeyboardEvent, MutableRefObject, RefObject, SetStateAction } from 'react';
import { LoaderCircle, Plus, Send, Square } from 'lucide-react';
import { CopilotAttachmentTray } from './CopilotAttachmentTray';
import type { CopilotUploadedAttachment } from './ProjectCopilotModal';
import type { InputHistoryNav } from './copilotInputHistory';
import './CopilotComposer.css';

/** Active @-mention query derived by the modal from draft + caret. */
export interface CopilotAttachmentMentionState {
  start: number;
  end: number;
  query: string;
  options: CopilotUploadedAttachment[];
}

export interface CopilotComposerDraftProps {
  draft: string;
  setDraft: Dispatch<SetStateAction<string>>;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  /** Syncs the ghost-overlay scroll position with the textarea. */
  syncGhostScroll: () => void;
  /** Any manual edit exits history-recall mode — reset the nav cursor. */
  historyNavRef: MutableRefObject<InputHistoryNav | null>;
}

export interface CopilotComposerMentionProps {
  attachmentMentionState: CopilotAttachmentMentionState | null;
  mentionActiveIndex: number;
  insertAttachmentMentionAtCaret: (attachment: CopilotUploadedAttachment) => void;
  setMentionDismissedDraft: Dispatch<SetStateAction<string | null>>;
  setMentionCaret: Dispatch<SetStateAction<number>>;
  syncMentionCaretFromTextarea: () => void;
}

export interface CopilotComposerCompletionProps {
  completions: string[];
  /** Ghost suffix — the top-ranked prediction rendered over the textarea. */
  completion: string;
  completionPickerIndex: number | null;
  acceptCompletion: (suffix: string) => void;
  ghostOverlayInnerRef: RefObject<HTMLDivElement | null>;
}

export interface CopilotComposerAttachmentsProps {
  uploadedAttachments: CopilotUploadedAttachment[];
  insertAttachmentMention: (attachment: CopilotUploadedAttachment) => void;
  removeUploadedAttachment: (attachmentId: string) => void;
  addUploadedFiles: (files: FileList | File[]) => void;
  plusMenuRef: RefObject<HTMLDivElement | null>;
  plusMenuOpen: boolean;
  setPlusMenuOpen: Dispatch<SetStateAction<boolean>>;
  fileInputRef: RefObject<HTMLInputElement | null>;
}

export interface CopilotComposerTurnProps {
  sending: boolean;
  applyingActionKey: string | null;
  bulkAction: 'apply' | 'cancel' | null;
  cancelSending: () => void;
  sendMessage: (overrideContent?: string) => Promise<void>;
}

interface CopilotComposerProps {
  draft: CopilotComposerDraftProps;
  mention: CopilotComposerMentionProps;
  completion: CopilotComposerCompletionProps;
  attachments: CopilotComposerAttachmentsProps;
  turn: CopilotComposerTurnProps;
  /** Keyboard state machine from useCopilotKeymap (mention menu → picker → history → send). */
  onKeyDown: (event: ReactKeyboardEvent<HTMLTextAreaElement>) => void;
}

export function CopilotComposer({
  draft: draftGroup,
  mention,
  completion: completionGroup,
  attachments,
  turn,
  onKeyDown
}: CopilotComposerProps) {
  // Re-bind the group members to their historical local names so the JSX
  // below stayed byte-identical to its life in the modal.
  const {
    draft,
    setDraft,
    textareaRef,
    syncGhostScroll,
    historyNavRef
  } = draftGroup;
  const {
    attachmentMentionState,
    mentionActiveIndex,
    insertAttachmentMentionAtCaret,
    setMentionDismissedDraft,
    setMentionCaret,
    syncMentionCaretFromTextarea
  } = mention;
  const {
    completions,
    completion,
    completionPickerIndex,
    acceptCompletion,
    ghostOverlayInnerRef
  } = completionGroup;
  const {
    uploadedAttachments,
    insertAttachmentMention,
    removeUploadedAttachment,
    addUploadedFiles,
    plusMenuRef,
    plusMenuOpen,
    setPlusMenuOpen,
    fileInputRef
  } = attachments;
  const {
    sending,
    applyingActionKey,
    bulkAction,
    cancelSending,
    sendMessage
  } = turn;

  return (
    <div className="copilot-composer">
      <div className="copilot-input-shell">
        <CopilotAttachmentTray
          attachments={uploadedAttachments}
          onInsert={insertAttachmentMention}
          onRemove={removeUploadedAttachment}
        />
        {attachmentMentionState ? (
          <div
            className="copilot-mention-menu"
            role="listbox"
            id="copilot-mention-listbox"
            aria-label="File mentions"
          >
            {attachmentMentionState.options.map((attachment, index) => (
              <button
                key={attachment.id}
                id={`copilot-mention-option-${index}`}
                className={`copilot-mention-option${index === mentionActiveIndex ? ' active' : ''}`}
                type="button"
                role="option"
                aria-selected={index === mentionActiveIndex}
                onMouseDown={(event) => {
                  event.preventDefault();
                  insertAttachmentMentionAtCaret(attachment);
                }}
              >
                <span className="copilot-mention-file-icon">@</span>
                <span className="copilot-mention-file-text">
                  <strong>{attachment.name}</strong>
                  <small>{Math.max(1, Math.round(attachment.size / 1024))} KB</small>
                </span>
              </button>
            ))}
          </div>
        ) : null}
        <div className="copilot-input-row">
          <div className="copilot-plus-wrap" ref={plusMenuRef as RefObject<HTMLDivElement>}>
            {plusMenuOpen ? (
              <div className="copilot-plus-menu">
                <button
                  className="copilot-plus-menu-item"
                  type="button"
                  onClick={() => {
                    setPlusMenuOpen(false);
                    fileInputRef.current?.click();
                  }}
                >
                  <Plus size={15} />
                  <span>Attach file</span>
                </button>
              </div>
            ) : null}
            <button
              className="copilot-attach-btn"
              type="button"
              onClick={() => setPlusMenuOpen((prev) => !prev)}
              disabled={Boolean(sending || applyingActionKey || bulkAction)}
              aria-label="Add attachment"
              title="Add"
            >
              <Plus size={16} />
            </button>
          </div>
          <input
            ref={fileInputRef as RefObject<HTMLInputElement>}
            className="copilot-file-input"
            type="file"
            multiple
            accept=".pdb,.ent,.cif,.mmcif,.sdf,.sd,.mol2,.mol,.txt,.csv,.tsv"
            onChange={(event) => {
              if (event.target.files) addUploadedFiles(event.target.files);
              event.currentTarget.value = '';
            }}
          />
          <div className="copilot-input-wrap">
            {completionPickerIndex !== null && completions.length > 0 ? (
              <div className="copilot-mention-menu copilot-completion-menu" role="listbox" id="copilot-completion-listbox" aria-label="Suggested completions">
                {completions.map((suffix, index) => (
                  <button
                    key={`${index}-${suffix}`}
                    id={`copilot-completion-option-${index}`}
                    type="button"
                    role="option"
                    aria-selected={index === completionPickerIndex}
                    className={`copilot-mention-option${index === completionPickerIndex ? ' active' : ''}`}
                    onMouseDown={(event) => {
                      event.preventDefault();
                      acceptCompletion(suffix);
                    }}
                  >
                    <span className="copilot-mention-file-icon">↵</span>
                    <span className="copilot-mention-file-text">
                      <strong>{draft}</strong>
                      <small>{suffix}</small>
                    </span>
                  </button>
                ))}
              </div>
            ) : null}
            {completion ? (
              <div className="copilot-ghost-overlay" aria-hidden="true">
                <div className="copilot-ghost-overlay-inner" ref={ghostOverlayInnerRef as RefObject<HTMLDivElement>}>
                  <span className="copilot-ghost-spacer">{draft}</span>
                  <span className="copilot-ghost-suffix">{completion}</span>
                </div>
              </div>
            ) : null}
            <textarea
              ref={textareaRef as RefObject<HTMLTextAreaElement>}
              value={draft}
              rows={1}
              role="combobox"
              aria-controls={attachmentMentionState ? 'copilot-mention-listbox' : 'copilot-completion-listbox'}
              aria-expanded={Boolean(attachmentMentionState) || completionPickerIndex !== null}
              aria-autocomplete="list"
              aria-activedescendant={
                attachmentMentionState && attachmentMentionState.options[mentionActiveIndex]
                  ? `copilot-mention-option-${mentionActiveIndex}`
                  : completionPickerIndex !== null
                    ? `copilot-completion-option-${completionPickerIndex}`
                    : undefined
              }
              onChange={(event) => {
                setDraft(event.target.value);
                // Any manual edit exits history-recall mode so the next ↑ starts from the newest.
                historyNavRef.current = null;
                setMentionDismissedDraft(null);
                setMentionCaret(event.target.selectionStart ?? event.target.value.length);
                if (typeof window !== 'undefined') {
                  window.requestAnimationFrame(syncMentionCaretFromTextarea);
                }
              }}
              onKeyDown={onKeyDown}
              onInput={() => {
                if (typeof window !== 'undefined') {
                  window.requestAnimationFrame(syncMentionCaretFromTextarea);
                }
              }}
              onFocus={syncMentionCaretFromTextarea}
              onClick={syncMentionCaretFromTextarea}
              onSelect={syncMentionCaretFromTextarea}
              onKeyUp={(event) => {
                if (event.key === 'Escape') return;
                syncMentionCaretFromTextarea();
              }}
              onScroll={syncGhostScroll}
              placeholder="Type a message…"
              // readOnly (not disabled) while a turn/action runs: a disabled textarea immediately
              // loses focus on mobile and dismisses the soft keyboard, causing the panel to grow
              // back to full height and then re-shrink when focus returns — the "keyboard flicker".
              // readOnly keeps focus and the keyboard open while still blocking user input.
              readOnly={Boolean(sending || applyingActionKey || bulkAction)}
            />
          </div>
          <button
            className={`copilot-send-btn${sending ? ' is-sending' : ''}`}
            type="button"
            onClick={sending ? cancelSending : () => void sendMessage()}
            // Prevent the button from stealing focus on tap (mousedown): on mobile, moving focus
            // off the textarea dismisses the keyboard. preventDefault on mousedown keeps focus on
            // the textarea so the keyboard stays open during send. The click still fires normally.
            onMouseDown={(event) => event.preventDefault()}
            disabled={!sending && Boolean(applyingActionKey || bulkAction || !draft.trim())}
            aria-label={sending ? 'Stop' : 'Send'}
            title={sending ? 'Stop' : 'Send'}
          >
            {sending ? (
              // While sending: a spinning loader shows the turn is in progress. On hover the CSS
              // swaps it for a stop icon (and reddens the button) so the affordance to cancel is
              // obvious — the spinner means "working", hover means "click to cancel".
              <>
                <LoaderCircle size={15} className="spin copilot-send-spin" />
                <Square size={13} className="copilot-send-stop" />
              </>
            ) : (
              <Send size={15} />
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
