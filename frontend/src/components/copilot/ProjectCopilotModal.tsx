import { Bot, Check, Clock3, LoaderCircle, MessageSquarePlus, MessageSquareText, PanelLeft, Send, Trash2, Upload, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { deleteProjectCopilotMessagesBySession, insertProjectCopilotMessage, listProjectCopilotMessages } from '../../api/supabaseLite';
import { requestCopilotAssistant, requestCopilotPlanActions } from '../../api/copilotApi';
import type { CopilotContextType, CopilotPlanAction, ProjectCopilotMessage } from '../../types/models';
import { formatDateTime } from '../../utils/date';
import './ProjectCopilotModal.css';

interface ProjectCopilotModalProps {
  open: boolean;
  title: string;
  subtitle: string;
  contextType: CopilotContextType;
  projectId?: string | null;
  projectTaskId?: string | null;
  currentUserId: string;
  currentUsername: string;
  contextPayload: Record<string, unknown>;
  buildPlanActions?: (content: string) => CopilotPlanAction[];
  onApplyPlanAction?: (action: CopilotPlanAction) => void | Promise<void>;
  attachmentActions?: Array<{
    id: string;
    label: string;
    accept: string;
    ready?: boolean;
    disabled?: boolean;
    onFile: (file: File) => void | Promise<void>;
  }>;
  onOpen: () => void;
  onClose: () => void;
}

function author(message: ProjectCopilotMessage): string {
  if (message.role === 'assistant') return 'V-Bio Copilot';
  return message.user_name || message.username || 'User';
}

function createSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function readSessionId(message: ProjectCopilotMessage): string {
  const value = message.metadata?.session_id;
  const normalized = typeof value === 'string' ? value.trim() : '';
  return normalized || 'default';
}

function readPlanActions(value: unknown): CopilotPlanAction[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (item && typeof item === 'object' ? (item as CopilotPlanAction) : null))
    .filter((item): item is CopilotPlanAction => Boolean(item?.id && item.label));
}

function hasParameterPatch(actions: CopilotPlanAction[]): boolean {
  return actions.some((action) => {
    const patch = action.payload?.parameterPatch;
    return Boolean(patch && typeof patch === 'object' && !Array.isArray(patch) && Object.keys(patch as Record<string, unknown>).length > 0);
  });
}

function preferFallbackWhenItHasConcretePatch(
  fallbackActions: CopilotPlanAction[],
  structuredActions: CopilotPlanAction[]
): CopilotPlanAction[] {
  if (structuredActions.length === 0) return fallbackActions;
  if (hasParameterPatch(fallbackActions) && !hasParameterPatch(structuredActions)) return fallbackActions;
  const fallbackSubmitAction =
    fallbackActions.find((action) => action.id === 'task_detail:apply_patch_and_submit') ||
    fallbackActions.find((action) => action.id === 'task_detail:apply_parameter_patch');
  const fallbackPatch = fallbackSubmitAction?.payload?.parameterPatch;
  if (!fallbackPatch || typeof fallbackPatch !== 'object' || Array.isArray(fallbackPatch)) return structuredActions;
  const fallbackPatchRecord = fallbackPatch as Record<string, unknown>;
  if (Object.keys(fallbackPatchRecord).length === 0) return structuredActions;
  return structuredActions.map((action) => {
    if (action.id !== 'task_detail:apply_patch_and_submit' && action.id !== 'task_detail:apply_parameter_patch') return action;
    const structuredPatch =
      action.payload?.parameterPatch && typeof action.payload.parameterPatch === 'object' && !Array.isArray(action.payload.parameterPatch)
        ? (action.payload.parameterPatch as Record<string, unknown>)
        : {};
    return {
      ...action,
      payload: {
        ...(action.payload || {}),
        parameterPatch: {
          ...fallbackPatchRecord,
          ...structuredPatch
        }
      }
    };
  });
}

function getSessionTitle(messages: ProjectCopilotMessage[], sessionId: string): string {
  const firstUserMessage = messages.find((message) => readSessionId(message) === sessionId && message.role === 'user');
  const content = String(firstUserMessage?.content || '').replace(/\s+/g, ' ').trim();
  if (!content) return sessionId === 'default' ? 'Previous chat' : 'New chat';
  return content.length > 32 ? `${content.slice(0, 32)}...` : content;
}

function copilotOpenStorageKey(): string {
  return 'vbio:copilot-open:v1';
}

interface CopilotPanelState {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  historyOpen?: boolean;
}

function copilotPanelStateStorageKey(userId: string): string {
  return `vbio:copilot-panel:v1:${String(userId || 'anonymous').trim().toLowerCase() || 'anonymous'}`;
}

function copilotContinuationStorageKey(userId: string, projectId?: string | null): string {
  return `vbio:copilot-continuation:v1:${String(userId || 'anonymous').trim().toLowerCase() || 'anonymous'}:${String(projectId || 'project-null')}`;
}

function readCopilotContinuation(userId: string, projectId?: string | null): {
  sessionId: string;
  sourceProjectTaskId: string;
  createdAt: number;
} | null {
  if (typeof window === 'undefined') return null;
  try {
    const parsed = JSON.parse(window.localStorage.getItem(copilotContinuationStorageKey(userId, projectId)) || 'null') as {
      sessionId?: string;
      sourceProjectTaskId?: string;
      createdAt?: number;
    } | null;
    const sessionId = String(parsed?.sessionId || '').trim();
    const sourceProjectTaskId = String(parsed?.sourceProjectTaskId || '').trim();
    const createdAt = Number(parsed?.createdAt || 0);
    if (!sessionId || !sourceProjectTaskId || !Number.isFinite(createdAt)) return null;
    if (Date.now() - createdAt > 10 * 60 * 1000) return null;
    return { sessionId, sourceProjectTaskId, createdAt };
  } catch {
    return null;
  }
}

function writeCopilotContinuation(userId: string, projectId: string | null | undefined, value: {
  sessionId: string;
  sourceProjectTaskId: string;
}): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(
    copilotContinuationStorageKey(userId, projectId),
    JSON.stringify({ ...value, createdAt: Date.now() })
  );
}

function clearCopilotContinuation(userId: string, projectId?: string | null): void {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(copilotContinuationStorageKey(userId, projectId));
}

function readStoredCopilotPanelState(userId: string): CopilotPanelState {
  if (typeof window === 'undefined') return {};
  try {
    const parsed = JSON.parse(window.localStorage.getItem(copilotPanelStateStorageKey(userId)) || '{}') as CopilotPanelState;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function writeStoredCopilotPanelState(userId: string, patch: CopilotPanelState): void {
  if (typeof window === 'undefined') return;
  const prev = readStoredCopilotPanelState(userId);
  window.localStorage.setItem(copilotPanelStateStorageKey(userId), JSON.stringify({ ...prev, ...patch }));
}

export function readStoredCopilotOpen(_input: { contextType: CopilotContextType; projectId?: string | null; projectTaskId?: string | null }): boolean {
  if (typeof window === 'undefined') return false;
  return window.localStorage.getItem(copilotOpenStorageKey()) === 'true';
}

export function writeStoredCopilotOpen(
  _input: { contextType: CopilotContextType; projectId?: string | null; projectTaskId?: string | null },
  open: boolean
): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(copilotOpenStorageKey(), open ? 'true' : 'false');
}

export function ProjectCopilotModal({
  open,
  title,
  subtitle,
  contextType,
  projectId = null,
  projectTaskId = null,
  currentUserId,
  currentUsername,
  contextPayload,
  buildPlanActions,
  onApplyPlanAction,
  attachmentActions = [],
  onOpen,
  onClose
}: ProjectCopilotModalProps) {
  const storedPanelState = useMemo(() => readStoredCopilotPanelState(currentUserId), [currentUserId]);
  const [messages, setMessages] = useState<ProjectCopilotMessage[]>([]);
  const [activeSessionId, setActiveSessionId] = useState(createSessionId);
  const [historyOpen, setHistoryOpen] = useState(Boolean(storedPanelState.historyOpen));
  const [draft, setDraft] = useState('');
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingActions, setPendingActions] = useState<CopilotPlanAction[]>([]);
  const [applyingActionId, setApplyingActionId] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  const [position, setPosition] = useState<{ x: number; y: number } | null>(() => {
    const x = Number(storedPanelState.x);
    const y = Number(storedPanelState.y);
    return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
  });
  const latestPositionRef = useRef<{ x: number; y: number } | null>(position);
  const sizeReadyRef = useRef(false);
  const [panelSize, setPanelSize] = useState<{ width: number; height: number } | null>(() => {
    const width = Number(storedPanelState.width);
    const height = Number(storedPanelState.height);
    return Number.isFinite(width) && Number.isFinite(height) ? { width, height } : null;
  });

  const scope = useMemo(
    () => ({ contextType, projectId: projectId || null, projectTaskId: projectTaskId || null, userId: currentUserId || null }),
    [contextType, currentUserId, projectId, projectTaskId]
  );

  const sessionMessages = useMemo(
    () => messages.filter((message) => readSessionId(message) === activeSessionId),
    [activeSessionId, messages]
  );

  const chatSessions = useMemo(() => {
    const sessionIds = Array.from(new Set(messages.map(readSessionId)));
    return sessionIds
      .map((sessionId) => {
        const sessionMessagesLocal = messages.filter((message) => readSessionId(message) === sessionId);
        const lastMessage = sessionMessagesLocal[sessionMessagesLocal.length - 1];
        return {
          id: sessionId,
          title: getSessionTitle(messages, sessionId),
          updatedAt: lastMessage?.created_at || ''
        };
      })
      .sort((a, b) => String(b.updatedAt).localeCompare(String(a.updatedAt)));
  }, [messages]);

  const restoreSessionActions = useCallback((nextMessages: ProjectCopilotMessage[], sessionId: string) => {
    const latestAssistant = [...nextMessages]
      .reverse()
      .find((message) => readSessionId(message) === sessionId && message.role === 'assistant');
    setPendingActions(readPlanActions(latestAssistant?.metadata?.candidate_plan_actions));
  }, []);

  const loadMessages = useCallback(async () => {
    if (!open) return;
    setLoading(true);
    setError(null);
    try {
      let loaded = await listProjectCopilotMessages(scope);
      const continuation = contextType === 'task_detail'
        ? readCopilotContinuation(currentUserId, projectId)
        : null;
      if (
        continuation &&
        projectTaskId &&
        continuation.sourceProjectTaskId &&
        continuation.sourceProjectTaskId !== projectTaskId
      ) {
        const copiedOriginalIds = new Set(
          loaded
            .map((message) => String(message.metadata?.continued_from_message_id || '').trim())
            .filter(Boolean)
        );
        const sourceMessages = (await listProjectCopilotMessages({
          contextType: 'task_detail',
          projectId: projectId || null,
          projectTaskId: continuation.sourceProjectTaskId,
          userId: currentUserId || null
        })).filter((message) => readSessionId(message) === continuation.sessionId);
        for (const sourceMessage of sourceMessages) {
          if (!sourceMessage.id || copiedOriginalIds.has(sourceMessage.id)) continue;
          await insertProjectCopilotMessage({
            ...scope,
            userId: sourceMessage.user_id,
            role: sourceMessage.role,
            content: sourceMessage.content,
            metadata: {
              ...(sourceMessage.metadata || {}),
              session_id: continuation.sessionId,
              owner_user_id: currentUserId,
              continued_from_project_task_id: continuation.sourceProjectTaskId,
              continued_from_message_id: sourceMessage.id
            }
          });
        }
        loaded = await listProjectCopilotMessages(scope);
        clearCopilotContinuation(currentUserId, projectId);
      }
      setMessages(loaded);
      setActiveSessionId((currentSessionId) => {
        const sessionIds = Array.from(new Set(loaded.map(readSessionId)));
        const nextSessionId = sessionIds.includes(currentSessionId) ? currentSessionId : sessionIds[0] || currentSessionId || createSessionId();
        restoreSessionActions(loaded, nextSessionId);
        return nextSessionId;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load Copilot messages.');
    } finally {
      setLoading(false);
    }
  }, [contextType, currentUserId, open, projectId, projectTaskId, restoreSessionActions, scope]);

  useEffect(() => {
    if (!open) return;
    void loadMessages();
  }, [loadMessages, open]);

  useEffect(() => {
    if (!open) {
      setDraft('');
      setError(null);
      return;
    }
    window.setTimeout(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    }, 0);
  }, [sessionMessages.length, open]);

  useEffect(() => {
    if (!open || position) return;
    if (typeof window === 'undefined') return;
    const nextPosition = {
      x: Math.max(12, window.innerWidth - 560 - 24),
      y: Math.max(12, window.innerHeight - 680 - 24)
    };
    latestPositionRef.current = nextPosition;
    setPosition(nextPosition);
    writeStoredCopilotPanelState(currentUserId, nextPosition);
  }, [currentUserId, open, position]);

  useEffect(() => {
    if (!open) return;
    writeStoredCopilotPanelState(currentUserId, { historyOpen });
  }, [currentUserId, historyOpen, open]);

  useEffect(() => {
    if (!open || !panelRef.current || typeof ResizeObserver === 'undefined') return;
    const initialWidth = Math.round(panelRef.current.getBoundingClientRect().width);
    const initialHeight = Math.round(panelRef.current.getBoundingClientRect().height);
    sizeReadyRef.current = false;
    let frame = 0;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      if (frame) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const width = Math.round(entry.contentRect.width);
        const height = Math.round(entry.contentRect.height);
        if (width < 300 || height < 300) return;
        if (!sizeReadyRef.current) {
          if (Math.abs(width - initialWidth) < 4 && Math.abs(height - initialHeight) < 4) {
            return;
          }
          sizeReadyRef.current = true;
        }
        setPanelSize((prev) => {
          if (prev && Math.abs(prev.width - width) < 2 && Math.abs(prev.height - height) < 2) return prev;
          writeStoredCopilotPanelState(currentUserId, { width, height });
          return { width, height };
        });
      });
    });
    observer.observe(panelRef.current);
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      observer.disconnect();
      sizeReadyRef.current = false;
    };
  }, [currentUserId, open]);

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement;
    if (target.closest('button, textarea, input, select, a')) return;
    const current = position || { x: Math.max(12, window.innerWidth - 560 - 24), y: Math.max(12, window.innerHeight - 680 - 24) };
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      originX: current.x,
      originY: current.y
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const moveDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const nextX = Math.min(Math.max(8, drag.originX + event.clientX - drag.startX), Math.max(8, window.innerWidth - 280));
    const nextY = Math.min(Math.max(8, drag.originY + event.clientY - drag.startY), Math.max(8, window.innerHeight - 180));
    const nextPosition = { x: nextX, y: nextY };
    latestPositionRef.current = nextPosition;
    setPosition(nextPosition);
  };

  const endDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    if (latestPositionRef.current) {
      writeStoredCopilotPanelState(currentUserId, latestPositionRef.current);
    }
    event.currentTarget.releasePointerCapture(event.pointerId);
  };

  const sendMessage = async () => {
    const content = draft.trim();
    if (!content) return;
    setSending(true);
    setError(null);
    setDraft('');
    setPendingActions([]);
    try {
      const userMessage = await insertProjectCopilotMessage({
        ...scope,
        userId: currentUserId,
        role: 'user',
        content,
        metadata: { session_id: activeSessionId }
      });
      setMessages((prev) => [...prev, { ...userMessage, username: currentUsername }]);

      const fallbackPlanActions = buildPlanActions ? buildPlanActions(content) : [];
      let planActions = fallbackPlanActions;
      try {
        const structuredPlanActions = await requestCopilotPlanActions({
          contextType,
          contextPayload,
          userId: currentUserId,
          username: currentUsername,
          content
        });
        planActions = preferFallbackWhenItHasConcretePatch(fallbackPlanActions, structuredPlanActions);
      } catch {
        planActions = fallbackPlanActions;
      }
      const assistantContent = await requestCopilotAssistant({
        contextType,
        contextPayload: {
          ...contextPayload,
          candidate_plan_actions: planActions
        },
        userId: currentUserId,
        username: currentUsername,
        content
      });
      const assistantMessage = await insertProjectCopilotMessage({
        ...scope,
        userId: null,
        role: 'assistant',
        content: assistantContent,
        metadata: { session_id: activeSessionId, owner_user_id: currentUserId, candidate_plan_actions: planActions }
      });
      setMessages((prev) => [...prev, assistantMessage]);
      setPendingActions(planActions);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send Copilot message.');
      setDraft(content);
    } finally {
      setSending(false);
    }
  };

  const resizeComposer = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 132)}px`;
  }, []);

  useEffect(() => {
    resizeComposer();
  }, [draft, resizeComposer]);

  const startNewChat = () => {
    const nextSessionId = createSessionId();
    setActiveSessionId(nextSessionId);
    setDraft('');
    setPendingActions([]);
    setError(null);
  };

  const selectSession = (sessionId: string) => {
    setActiveSessionId(sessionId);
    setDraft('');
    setError(null);
    restoreSessionActions(messages, sessionId);
  };

  const deleteSession = async (sessionId: string) => {
    if (!window.confirm('Delete this chat history?')) return;
    setError(null);
    try {
      const messageIds = messages
        .filter((message) => readSessionId(message) === sessionId)
        .map((message) => message.id);
      await deleteProjectCopilotMessagesBySession({ ...scope, sessionId, userId: currentUserId, messageIds });
      setMessages((prev) => prev.filter((message) => readSessionId(message) !== sessionId));
      if (sessionId === activeSessionId) {
        startNewChat();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete chat history.');
    }
  };

  const applyAction = async (action: CopilotPlanAction) => {
    if (!onApplyPlanAction) return;
    setApplyingActionId(action.id);
    setError(null);
    try {
      if (
        contextType === 'task_detail' &&
        projectTaskId &&
        (action.id === 'task_detail:submit_current' || action.id === 'task_detail:apply_patch_and_submit')
      ) {
        writeCopilotContinuation(currentUserId, projectId, {
          sessionId: activeSessionId,
          sourceProjectTaskId: projectTaskId
        });
      }
      await onApplyPlanAction(action);
      setPendingActions((prev) => prev.filter((item) => item.id !== action.id));
      const receipt = await insertProjectCopilotMessage({
        ...scope,
        userId: currentUserId,
        role: 'system',
        content: `Confirmed action: ${action.label}`,
        metadata: { session_id: activeSessionId, owner_user_id: currentUserId, applied_action: action }
      });
      setMessages((prev) => [...prev, receipt]);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to apply Copilot action.');
    } finally {
      setApplyingActionId(null);
    }
  };

  if (!open) {
    return (
      <button className="copilot-launcher" type="button" onClick={onOpen} aria-label="Open Copilot" title="Open Copilot">
        <Bot size={20} />
      </button>
    );
  }

  return (
    <div
      ref={panelRef}
      className="copilot-floating-panel"
      style={{
        ...(position ? { left: position.x, top: position.y } : {}),
        ...(panelSize ? { width: panelSize.width, height: panelSize.height } : {})
      }}
      role="dialog"
      aria-modal="false"
      aria-label={title}
    >
      <div className={`copilot-modal copilot-chat-window${historyOpen ? ' history-open' : ''}`}>
        <div
          className="copilot-head copilot-drag-handle"
          onPointerDown={startDrag}
          onPointerMove={moveDrag}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
        >
          <div className="copilot-title">
            <MessageSquareText size={18} />
            <div>
              <h2>{title}</h2>
              <span>{subtitle}</span>
            </div>
          </div>
          <div className="copilot-head-actions">
            <button
              className="task-row-action-btn"
              type="button"
              onClick={() => setHistoryOpen((prev) => !prev)}
              aria-label="Chat history"
              title="Chat history"
            >
              <PanelLeft size={15} />
            </button>
            <button className="task-row-action-btn" type="button" onClick={startNewChat} aria-label="New chat" title="New chat">
              <MessageSquarePlus size={15} />
            </button>
            <button className="task-row-action-btn" type="button" onClick={onClose} aria-label="Close Copilot" title="Close">
              <X size={15} />
            </button>
          </div>
        </div>

        {error ? <div className="alert error copilot-error">{error}</div> : null}

        {historyOpen ? (
          <aside className="copilot-history">
            <div className="copilot-history-head">
              <strong>History</strong>
              <button type="button" onClick={startNewChat}>
                <MessageSquarePlus size={14} />
                New chat
              </button>
            </div>
            <div className="copilot-history-list">
              {chatSessions.length === 0 ? (
                <div className="copilot-history-empty">No previous chats</div>
              ) : (
                chatSessions.map((session) => (
                  <div className={`copilot-history-item${session.id === activeSessionId ? ' active' : ''}`} key={session.id}>
                    <button type="button" onClick={() => selectSession(session.id)}>
                      <span>{session.title}</span>
                      {session.updatedAt ? (
                        <small>
                          <Clock3 size={11} />
                          {formatDateTime(session.updatedAt)}
                        </small>
                      ) : null}
                    </button>
                    <button
                      className="copilot-history-delete"
                      type="button"
                      onClick={() => void deleteSession(session.id)}
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
        ) : null}

        <div className="copilot-messages" ref={scrollRef}>
          {loading ? (
            <div className="copilot-empty">
              <LoaderCircle size={16} className="spin" />
              Loading messages
            </div>
          ) : sessionMessages.length === 0 ? (
            null
          ) : (
            sessionMessages.map((message) => (
              <article key={message.id} className={`copilot-message is-${message.role}`}>
                <div className="copilot-message-meta">
                  <strong>{author(message)}</strong>
                  <span>{formatDateTime(message.created_at)}</span>
                </div>
                <div className="copilot-message-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
                    {message.content}
                  </ReactMarkdown>
                </div>
              </article>
            ))
          )}
          {sending ? (
            <article className="copilot-message is-assistant">
              <div className="copilot-message-body copilot-thinking">
                <LoaderCircle size={14} className="spin" />
                Planning
              </div>
            </article>
          ) : null}
        </div>

        {pendingActions.length > 0 ? (
          <div className="copilot-plan-actions">
            {pendingActions.map((action) => (
              <button key={action.id} type="button" onClick={() => void applyAction(action)} disabled={Boolean(applyingActionId)}>
                {applyingActionId === action.id ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
                <span>
                  <strong>{action.label}</strong>
                  <small>{action.description}</small>
                </span>
              </button>
            ))}
          </div>
        ) : null}

        <div className={`copilot-composer${attachmentActions.length > 0 ? ' has-attachments' : ''}`}>
          {attachmentActions.length > 0 ? (
            <div className="copilot-attachment-actions" aria-label="Upload files">
              {attachmentActions.map((action) => (
                <label
                  className={`copilot-attachment-action${action.ready ? ' ready' : ''}${action.disabled ? ' disabled' : ''}`}
                  key={action.id}
                  title={action.label}
                  aria-label={action.label}
                >
                  {action.ready ? <Check size={15} /> : <Upload size={15} />}
                  <span className="copilot-attachment-action-label">{action.label}</span>
                  <input
                    type="file"
                    accept={action.accept}
                    disabled={action.disabled}
                    onClick={(event) => {
                      (event.currentTarget as HTMLInputElement).value = '';
                    }}
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) void action.onFile(file);
                    }}
                  />
                </label>
              ))}
            </div>
          ) : null}
          <textarea
            ref={textareaRef}
            value={draft}
            rows={1}
            onChange={(event) => {
              setDraft(event.target.value);
            }}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                void sendMessage();
              }
            }}
            placeholder="Ask Copilot to analyze, filter, sort, or plan a confirmed action"
            disabled={sending}
          />
          <button
            className="copilot-send-btn"
            type="button"
            onClick={() => void sendMessage()}
            disabled={sending || !draft.trim()}
            aria-label="Send"
            title="Send"
          >
            {sending ? <LoaderCircle size={15} className="spin" /> : <Send size={15} />}
          </button>
        </div>
      </div>
    </div>
  );
}
