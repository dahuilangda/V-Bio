/**
 * Chat composer: attachment tray, @-mention menu, plus-menu, ghost-completion
 * overlay, autogrow textarea, input history recall and the send/stop button.
 *
 * Owns the typing-domain state (draft store, mention caret/menu, inline
 * completions, keyboard state machine) so a keystroke re-renders only this
 * component. The modal reads and writes the draft imperatively via the store.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type MutableRefObject,
  type RefObject,
  type SetStateAction
} from 'react';
import { LoaderCircle, Plus, Send, Square } from 'lucide-react';
import { CopilotAttachmentTray } from './CopilotAttachmentTray';
import type { CopilotUploadedAttachment } from './ProjectCopilotModal';
import type { InputHistoryNav } from './copilotInputHistory';
import { nextInputHistoryNav, shouldNavigateHistory } from './copilotInputHistory';
import { fuzzyRank } from '../../utils/fuzzyScore';
import { useCopilotKeymap } from './useCopilotKeymap';
import { requestCopilotCompletions } from '../../api/copilotApi';
import type { ProjectCopilotMessage } from '../../types/models';
import { buildCopilotConversationContext } from './copilotConversationContext';
import { completionCacheKey, createCompletionCache } from './copilotCompletionCache';
import type { CopilotDraftStore } from './copilotDraftStore';
import { useCopilotDraft } from './copilotDraftStore';
import './CopilotComposer.css';

/** Active @-mention query derived from draft + caret. */
export interface CopilotAttachmentMentionState {
  start: number;
  end: number;
  query: string;
  options: CopilotUploadedAttachment[];
}

export interface CopilotComposerDraftProps {
  /** Single source of truth for the draft text (typed + programmatic edits). */
  store: CopilotDraftStore;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  /** Fires on every draft change: localStorage now, debounced DB write. */
  onDraftPersist: (value: string) => void;
  /** Any manual edit exits history-recall mode — reset the nav cursor. */
  historyNavRef: MutableRefObject<InputHistoryNav | null>;
  /** Sent-input history (read-only here; the modal appends on send). */
  inputHistoryRef: MutableRefObject<string[]>;
}

export interface CopilotComposerCompletionProps {
  /** Backend inline-completion availability (one cheap GET per panel open). */
  completionEnabled: boolean;
  contextType: string;
  currentUserId: string;
  currentUsername: string;
  /** Latest page context for the completer; a ref — identity is irrelevant. */
  contextPayloadRef: MutableRefObject<Record<string, unknown>>;
  /** Latest conversation for the completer; a ref — identity is irrelevant. */
  conversationRef: MutableRefObject<ProjectCopilotMessage[]>;
}

export interface CopilotComposerAttachmentsProps {
  uploadedAttachments: CopilotUploadedAttachment[];
  onRemoveAttachment: (attachmentId: string) => void;
  onAddFiles: (files: FileList | File[]) => void;
  plusMenuRef: RefObject<HTMLDivElement | null>;
  isPlusMenuOpen: boolean;
  setPlusMenuOpen: Dispatch<SetStateAction<boolean>>;
  fileInputRef: RefObject<HTMLInputElement | null>;
}

export interface CopilotComposerTurnProps {
  isSending: boolean;
  applyingActionKey: string | null;
  bulkAction: 'apply' | 'cancel' | null;
  onCancelSending: () => void;
  sendMessageAction: (overrideContent?: string) => Promise<void>;
}

interface CopilotComposerProps {
  draft: CopilotComposerDraftProps;
  completion: CopilotComposerCompletionProps;
  attachments: CopilotComposerAttachmentsProps;
  turn: CopilotComposerTurnProps;
}

// The completer is a real LLM round-trip: debounce it, require some content,
// and cache exact-prefix hits.
const COMPLETION_DEBOUNCE_MS = 900;
const COMPLETION_MIN_DRAFT_CHARS = 2;

export function CopilotComposer({
  draft: draftGroup,
  completion: completionGroup,
  attachments,
  turn
}: CopilotComposerProps) {
  const {
    store: draftStore,
    textareaRef,
    onDraftPersist,
    historyNavRef,
    inputHistoryRef
  } = draftGroup;
  const {
    completionEnabled,
    contextType,
    currentUserId,
    currentUsername,
    contextPayloadRef,
    conversationRef
  } = completionGroup;
  const {
    uploadedAttachments,
    onRemoveAttachment,
    onAddFiles,
    plusMenuRef,
    isPlusMenuOpen,
    setPlusMenuOpen,
    fileInputRef
  } = attachments;
  const {
    isSending,
    applyingActionKey,
    bulkAction,
    onCancelSending,
    sendMessageAction
  } = turn;

  const draft = useCopilotDraft(draftStore);
  const setDraft = useCallback((value: string) => draftStore.set(value), [draftStore]);
  const onDraftPersistRef = useRef(onDraftPersist);
  onDraftPersistRef.current = onDraftPersist;

  // localStorage immediately, DB via the modal's debounced callback; outside React state.
  useEffect(() => {
    return draftStore.subscribe((value) => onDraftPersistRef.current(value));
  }, [draftStore]);

  const [mentionCaret, setMentionCaret] = useState(0);
  const [mentionActiveIndex, setMentionActiveIndex] = useState(0);
  const [mentionDismissedDraft, setMentionDismissedDraft] = useState<string | null>(null);
  const [completions, setCompletions] = useState<string[]>([]);
  const [completionPickerIndex, setCompletionPickerIndex] = useState<number | null>(null);
  const ghostOverlayInnerRef = useRef<HTMLDivElement | null>(null);
  const completionTimerRef = useRef<number | null>(null);
  const completionAbortRef = useRef<AbortController | null>(null);
  const completionTokenRef = useRef(0);
  const completionCacheRef = useRef(createCompletionCache());
  const isBusy = Boolean(isSending || applyingActionKey || bulkAction);

  // Active @-mention computed from draft + caret; null when none.
  const attachmentMentionState = useMemo(() => {
    if (uploadedAttachments.length === 0) return null;
    if (mentionDismissedDraft === draft) return null;
    const caret = Math.max(0, Math.min(mentionCaret, draft.length));
    const beforeCaret = draft.slice(0, caret);
    const asciiAtIndex = beforeCaret.lastIndexOf('@');
    const fullwidthAtIndex = beforeCaret.lastIndexOf('＠');
    const atIndex = Math.max(asciiAtIndex, fullwidthAtIndex);
    if (atIndex < 0) return null;
    const prefix = atIndex > 0 ? beforeCaret[atIndex - 1] : '';
    if (prefix && !/\s|[(\[{,;:]/.test(prefix)) return null;
    const query = beforeCaret.slice(atIndex + 1);
    if (/[\r\n\t]/.test(query)) return null;
    if (query.includes('  ')) return null;
    const normalizedQuery = query.trim().toLowerCase();
    // cmdk-style fuzzy ranking; a bare @ with no query keeps upload order.
    const options = normalizedQuery
      ? fuzzyRank(uploadedAttachments, normalizedQuery, (attachment) => attachment.name).slice(0, 6)
      : uploadedAttachments.slice(0, 6);
    if (options.length === 0) return null;
    return { start: atIndex, end: caret, query, options };
  }, [draft, mentionCaret, mentionDismissedDraft, uploadedAttachments]);

  useEffect(() => {
    if (!attachmentMentionState) {
      setMentionActiveIndex(0);
      return;
    }
    setMentionActiveIndex((index) => Math.min(index, attachmentMentionState.options.length - 1));
  }, [attachmentMentionState]);

  const syncMentionCaretFromTextarea = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    setMentionDismissedDraft(null);
    setMentionCaret(textarea.selectionStart ?? textarea.value.length);
  }, [textareaRef]);

  // The ghost suffix is the top-ranked prediction; the picker exposes the rest (top-10).
  const completion = completions[0] || '';

  const acceptCompletion = useCallback((suffix: string) => {
    if (!suffix) return;
    const current = draftStore.get();
    const next = `${current}${suffix}`;
    setCompletions([]);
    setCompletionPickerIndex(null);
    historyNavRef.current = null;
    draftStore.set(next);
    setMentionCaret(next.length);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus({ preventScroll: true });
      textareaRef.current?.setSelectionRange(next.length, next.length);
    });
  }, [draftStore, historyNavRef, textareaRef]);

  // Debounced, best-effort completion fetch: cancel anything in flight, clear the ghost,
  // then fetch after a pause; stale or aborted results are dropped. Contexts are read via
  // refs (parents pass inline objects) so unrelated parent re-renders don't clear the ghost.
  useEffect(() => {
    if (completionTimerRef.current !== null) {
      window.clearTimeout(completionTimerRef.current);
      completionTimerRef.current = null;
    }
    completionAbortRef.current?.abort();
    completionAbortRef.current = null;
    setCompletions([]);
    setCompletionPickerIndex(null);

    if (!completionEnabled || isBusy || attachmentMentionState) {
      return;
    }
    const snapshot = draft;
    if (snapshot.trim().length < COMPLETION_MIN_DRAFT_CHARS) return;
    const cacheKey = completionCacheKey({ contextType, userId: currentUserId, draft: snapshot });
    const cached = completionCacheRef.current.get(cacheKey);
    if (cached) {
      setCompletions(cached);
      return;
    }
    const token = ++completionTokenRef.current;
    completionTimerRef.current = window.setTimeout(() => {
      completionTimerRef.current = null;
      const controller = new AbortController();
      completionAbortRef.current = controller;
      // Merge the recent conversation into the completer's context so it can predict follow-up
      // intent (e.g. after "aspirin SMILES" the user typing "its target" should complete toward
      // targets). The planner already gets this via copilot_conversation; the completer needs the
      // same recency window to anticipate what the user is most likely to say next.
      const conversationContext = buildCopilotConversationContext(conversationRef.current);
      const enrichedPayload = { ...contextPayloadRef.current, copilot_conversation: conversationContext };
      void requestCopilotCompletions(
        { contextType, contextPayload: enrichedPayload, userId: currentUserId, username: currentUsername, content: snapshot },
        controller.signal
      ).then((ranked) => {
        if (token !== completionTokenRef.current || controller.signal.aborted) return;
        completionCacheRef.current.put(cacheKey, ranked);
        if (ranked.length > 0 && draftStore.get() === snapshot) setCompletions(ranked);
      });
    }, COMPLETION_DEBOUNCE_MS);
    return () => {
      if (completionTimerRef.current !== null) {
        window.clearTimeout(completionTimerRef.current);
        completionTimerRef.current = null;
      }
      completionAbortRef.current?.abort();
      completionAbortRef.current = null;
    };
  }, [attachmentMentionState, completionEnabled, contextType, conversationRef, currentUserId, currentUsername, contextPayloadRef, draft, draftStore, isBusy]);

  // Abort any in-flight completion when the composer unmounts.
  useEffect(() => () => {
    completionAbortRef.current?.abort();
  }, []);

  // Autogrow the textarea with its content (capped by CSS / 220px).
  const resizeComposer = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
  }, [textareaRef]);

  useEffect(() => {
    resizeComposer();
  }, [draft, resizeComposer]);

  // Mirror the textarea's scroll so the ghost suffix stays glued to the caret.
  const syncGhostScroll = useCallback(() => {
    const inner = ghostOverlayInnerRef.current;
    const textarea = textareaRef.current;
    if (inner && textarea) {
      inner.style.transform = `translateY(${-textarea.scrollTop}px)`;
    }
  }, [textareaRef]);

  // Apply a recalled history value; caret parks at the end so the next ↑/↓ is one step.
  const applyHistoryValue = useCallback((value: string) => {
    draftStore.set(value);
    setMentionCaret(value.length);
    window.requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus({ preventScroll: true });
      textarea.setSelectionRange(value.length, value.length);
    });
  }, [draftStore, textareaRef]);

  const insertAttachmentMentionAtCaret = useCallback((attachment: CopilotUploadedAttachment) => {
    const state = attachmentMentionState;
    const textarea = textareaRef.current;
    const fallbackCaret = textarea?.selectionStart ?? draft.length;
    const start = state?.start ?? fallbackCaret;
    const end = state?.end ?? fallbackCaret;
    const mention = `@${attachment.name}`;
    const suffix = draft.slice(end);
    const needsSpace = suffix.length === 0 || !/^\s/.test(suffix);
    const nextDraft = `${draft.slice(0, start)}${mention}${needsSpace ? ' ' : ''}${suffix}`;
    const nextCaret = start + mention.length + (needsSpace ? 1 : 0);
    draftStore.set(nextDraft);
    setMentionDismissedDraft(null);
    setMentionCaret(nextCaret);
    if (typeof window !== 'undefined') {
      window.requestAnimationFrame(() => {
        textareaRef.current?.focus({ preventScroll: true });
        textareaRef.current?.setSelectionRange(nextCaret, nextCaret);
      });
    }
  }, [attachmentMentionState, draft, draftStore, textareaRef]);

  const insertAttachmentMention = useCallback((attachment: CopilotUploadedAttachment) => {
    const mention = `@${attachment.name}`;
    const prev = draftStore.get();
    if (!prev.includes(mention)) {
      const separator = prev.trim() ? ' ' : '';
      draftStore.set(`${prev}${separator}${mention}`);
    }
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus({ preventScroll: true });
      syncMentionCaretFromTextarea();
    });
  }, [draftStore, syncMentionCaretFromTextarea, textareaRef]);

  // Keyboard state machine (mention menu → picker → history → send).
  const handleComposerKeyDown = useCopilotKeymap({
    mentionState: attachmentMentionState,
    mentionActiveIndex,
    setMentionActiveIndex,
    insertMention: (option) => insertAttachmentMentionAtCaret(option as CopilotUploadedAttachment),
    dismissMention: () => { setMentionDismissedDraft(draft); setMentionCaret(-1); },
    completion,
    completions,
    completionPickerIndex,
    setCompletionPickerIndex,
    setCompletions,
    acceptCompletion,
    shouldNavigateHistory,
    nextHistory: (dir: 'up' | 'down') => {
      const result = nextInputHistoryNav(inputHistoryRef.current, historyNavRef.current, draft, dir);
      if (result) historyNavRef.current = result.nav;
      return result ? { value: result.value } : null;
    },
    applyHistoryValue,
    draft,
    sendMessage: sendMessageAction,
  });

  return (
    <div className="copilot-composer">
      <div className="copilot-input-shell">
        <CopilotAttachmentTray
          attachments={uploadedAttachments}
          onInsert={insertAttachmentMention}
          onRemove={onRemoveAttachment}
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
            {isPlusMenuOpen ? (
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
              disabled={Boolean(isSending || applyingActionKey || bulkAction)}
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
              if (event.target.files) onAddFiles(event.target.files);
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
              onKeyDown={handleComposerKeyDown}
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
              // readOnly (not disabled): disabling dismisses the mobile soft keyboard
              // ("keyboard flicker"); readOnly keeps focus while still blocking input.
              readOnly={Boolean(isSending || applyingActionKey || bulkAction)}
            />
          </div>
          <button
            className={`copilot-send-btn${isSending ? ' is-sending' : ''}`}
            type="button"
            onClick={isSending ? onCancelSending : () => void sendMessageAction()}
            // preventDefault keeps focus on the textarea so the mobile keyboard stays open.
            onMouseDown={(event) => event.preventDefault()}
            disabled={!isSending && Boolean(applyingActionKey || bulkAction || !draft.trim())}
            aria-label={isSending ? 'Stop' : 'Send'}
            title={isSending ? 'Stop' : 'Send'}
          >
            {isSending ? (
              // Spinner while working; CSS swaps in a stop icon on hover to afford cancel.
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
