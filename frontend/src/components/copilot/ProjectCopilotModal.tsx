import { Bot, Check, Clock3, LoaderCircle, MessageSquarePlus, MessageSquareText, PanelLeft, Plus, Send, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  deleteProjectCopilotMessagesBySession,
  deleteProjectCopilotState,
  getProjectCopilotState,
  insertProjectCopilotMessage,
  readCachedProjectCopilotMessages,
  listProjectCopilotMessages,
  upsertProjectCopilotState
} from '../../api/supabaseLite';
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
  onApplyPlanAction?: (action: CopilotPlanAction) => void | Promise<void>;
  onSendAttachments?: (
    attachments: CopilotUploadedAttachment[],
    content: string,
    applications?: CopilotAttachmentApplication[]
  ) => void | Promise<void>;
  onOpen: () => void;
  onClose: () => void;
}

export interface CopilotUploadedAttachment {
  id: string;
  file: File;
  name: string;
  type: string;
  size: number;
}

export interface CopilotAttachmentApplication {
  attachmentId: string;
  fileName: string;
  role: 'target' | 'ligand' | 'template';
}

const COPILOT_RECENT_CONTEXT_MESSAGES = 6;
const COPILOT_SUMMARY_SOURCE_MESSAGES = 12;
const COPILOT_CONTEXT_MESSAGE_CHARS = 700;
const COPILOT_CONTEXT_SUMMARY_CHARS = 1800;

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
  const seen = new Set<string>();
  return value
    .map((item) => (item && typeof item === 'object' ? (item as CopilotPlanAction) : null))
    .filter((item): item is CopilotPlanAction => {
      if (!item?.id || !item.label) return false;
      const key = `${item.id}:${JSON.stringify(item.payload || {})}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
}

function readAppliedActionKey(message: ProjectCopilotMessage): string {
  const applied = message.metadata?.applied_action;
  if (!applied || typeof applied !== 'object') return '';
  const action = applied as CopilotPlanAction;
  return `${String(action.id || '').trim()}:${JSON.stringify(action.payload || {})}`;
}

function filterAppliedPlanActions(messages: ProjectCopilotMessage[], sessionId: string, actions: CopilotPlanAction[]): CopilotPlanAction[] {
  if (actions.length === 0) return [];
  const appliedKeys = new Set(
    messages
      .filter((message) => readSessionId(message) === sessionId)
      .map(readAppliedActionKey)
      .filter(Boolean)
  );
  if (appliedKeys.size === 0) return actions;
  return actions.filter((action) => !appliedKeys.has(`${action.id}:${JSON.stringify(action.payload || {})}`));
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

function copilotStateLocalStorageKey(userId: string, stateKey: string): string {
  return [
    'vbio:project-copilot-state:v1',
    String(userId || 'anonymous').trim().toLowerCase() || 'anonymous',
    String(stateKey || 'default').trim() || 'default'
  ].join(':');
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

function copilotPanelStateDbKey(): string {
  return 'panel';
}

function copilotOpenStateDbKey(): string {
  return 'open';
}

function copilotActiveSessionStateDbKey(): string {
  return 'active_session:global';
}

function copilotActiveSessionStorageKey(userId: string): string {
  return `vbio:copilot-active-session:v1:${String(userId || 'anonymous').trim().toLowerCase() || 'anonymous'}`;
}

function copilotContinuationStorageKey(userId: string, projectId?: string | null): string {
  return `vbio:copilot-continuation:v1:${String(userId || 'anonymous').trim().toLowerCase() || 'anonymous'}:${String(projectId || 'project-null')}`;
}

function copilotContinuationDbKey(projectId?: string | null): string {
  return `continuation:${String(projectId || 'project-null')}`;
}

function copilotTaskPrefillStorageKey(userId: string, projectId?: string | null): string {
  return `vbio:copilot-task-prefill:v1:${String(userId || 'anonymous').trim().toLowerCase() || 'anonymous'}:${String(projectId || 'project-null')}`;
}

function copilotDraftStorageKey(input: {
  userId: string;
}): string {
  return [
    'vbio:copilot-draft:v1',
    String(input.userId || 'anonymous').trim().toLowerCase() || 'anonymous',
    'global'
  ].join(':');
}

function copilotDraftDbKey(): string {
  return 'draft:global';
}

function readStoredCopilotDraftLocal(input: {
  userId: string;
}): string {
  if (typeof window === 'undefined') return '';
  try {
    return String(window.localStorage.getItem(copilotDraftStorageKey(input)) || '');
  } catch {
    return '';
  }
}

function writeStoredCopilotDraftLocal(input: {
  userId: string;
}, draft: string): void {
  if (typeof window === 'undefined') return;
  const key = copilotDraftStorageKey(input);
  if (draft) {
    window.localStorage.setItem(key, draft);
  } else {
    window.localStorage.removeItem(key);
  }
}

function readStoredCopilotActiveSessionLocal(userId: string): string {
  if (typeof window === 'undefined') return '';
  try {
    return String(window.localStorage.getItem(copilotActiveSessionStorageKey(userId)) || '').trim();
  } catch {
    return '';
  }
}

async function readStoredCopilotActiveSession(userId: string): Promise<string> {
  const local = readStoredCopilotActiveSessionLocal(userId);
  if (local) return local;
  const persisted = await getProjectCopilotState(userId, copilotActiveSessionStateDbKey());
  return String(persisted?.session_id || '').trim();
}

function writeStoredCopilotActiveSession(userId: string, sessionId: string): void {
  const normalizedSessionId = String(sessionId || '').trim();
  if (!normalizedSessionId) return;
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(copilotActiveSessionStorageKey(userId), normalizedSessionId);
  }
  void upsertProjectCopilotState(userId, copilotActiveSessionStateDbKey(), { session_id: normalizedSessionId });
}

function clearStoredCopilotActiveSession(userId: string): void {
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(copilotActiveSessionStorageKey(userId));
  }
  void deleteProjectCopilotState(userId, copilotActiveSessionStateDbKey());
}

type CopilotContinuationState = {
  sessionId: string;
  sourceContextType: CopilotContextType;
  sourceProjectTaskId: string;
  createdAt: number;
  appliedAction: CopilotPlanAction | null;
  followUpActions: CopilotPlanAction[];
};

type CopilotTaskPrefillState = {
  sessionId: string;
  projectId: string;
  sourceActionId: string;
  components: unknown[];
  createdAt: number;
};

function parseCopilotContinuation(value: unknown): CopilotContinuationState | null {
  const parsed = value && typeof value === 'object' ? (value as {
    sessionId?: string;
    sourceContextType?: CopilotContextType;
    sourceProjectTaskId?: string;
    createdAt?: number;
    appliedAction?: CopilotPlanAction;
    followUpActions?: unknown;
  }) : null;
  const sessionId = String(parsed?.sessionId || '').trim();
  const sourceProjectTaskId = String(parsed?.sourceProjectTaskId || '').trim();
  const sourceContextType = parsed?.sourceContextType === 'task_list' || parsed?.sourceContextType === 'project_list'
    ? parsed.sourceContextType
    : 'task_detail';
  const createdAt = Number(parsed?.createdAt || 0);
  if (!sessionId || !sourceProjectTaskId || !Number.isFinite(createdAt)) return null;
  if (Date.now() - createdAt > 10 * 60 * 1000) return null;
  const appliedAction = parsed?.appliedAction && typeof parsed.appliedAction === 'object' ? parsed.appliedAction : null;
  const followUpActions = readPlanActions(parsed?.followUpActions);
  return { sessionId, sourceContextType, sourceProjectTaskId, createdAt, appliedAction, followUpActions };
}

function readCopilotContinuationLocal(userId: string, projectId?: string | null): CopilotContinuationState | null {
  if (typeof window === 'undefined') return null;
  try {
    return parseCopilotContinuation(JSON.parse(window.localStorage.getItem(copilotContinuationStorageKey(userId, projectId)) || 'null'));
  } catch {
    return null;
  }
}

async function readCopilotContinuation(userId: string, projectId?: string | null): Promise<CopilotContinuationState | null> {
  const local = readCopilotContinuationLocal(userId, projectId);
  if (local) return local;
  const persisted = await getProjectCopilotState(userId, copilotContinuationDbKey(projectId));
  return parseCopilotContinuation(persisted);
}

function writeCopilotContinuation(userId: string, projectId: string | null | undefined, value: {
  sessionId: string;
  sourceContextType: CopilotContextType;
  sourceProjectTaskId: string;
  appliedAction?: CopilotPlanAction;
  followUpActions?: CopilotPlanAction[];
}): void {
  if (typeof window === 'undefined') return;
  const payload = { ...value, createdAt: Date.now() };
  window.localStorage.setItem(
    copilotContinuationStorageKey(userId, projectId),
    JSON.stringify(payload)
  );
  void upsertProjectCopilotState(userId, copilotContinuationDbKey(projectId), payload);
}

function buildSubmitCurrentFollowUpAction(params?: {
  description?: string;
  sourceActionId?: string;
  sequence?: unknown;
}): CopilotPlanAction {
  const { description, sourceActionId = 'copilot:follow_up', sequence } = params || {};
  const normalizedSequence = String(sequence || '').trim().toUpperCase();
  return {
    id: 'task_detail:submit_current',
    label: '开始运行',
    description: description || (normalizedSequence
      ? `新任务已填写序列 ${normalizedSequence}，确认后开始结构预测。`
      : '当前任务内容已填写完成，确认后开始运行。'),
    payload: {
      schemaVersion: 'vbio-copilot-action-v2',
      contextType: 'task_detail',
      workflowKey: 'prediction',
      sourceActionId,
      destructive: false
    },
    needs_confirmation: true,
    execute_now: false
  };
}

function clearCopilotContinuation(userId: string, projectId?: string | null): void {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(copilotContinuationStorageKey(userId, projectId));
  void deleteProjectCopilotState(userId, copilotContinuationDbKey(projectId));
}

function parseCopilotTaskPrefill(value: unknown, projectId?: string | null): CopilotTaskPrefillState | null {
  const parsed = value && typeof value === 'object' ? (value as {
    sessionId?: string;
    projectId?: string;
    sourceActionId?: string;
    components?: unknown;
    createdAt?: number;
  }) : null;
  const sessionId = String(parsed?.sessionId || '').trim();
  const normalizedProjectId = String(parsed?.projectId || '').trim();
  const expectedProjectId = String(projectId || '').trim();
  const createdAt = Number(parsed?.createdAt || 0);
  if (!sessionId || !normalizedProjectId || !Array.isArray(parsed?.components) || !Number.isFinite(createdAt)) return null;
  if (expectedProjectId && normalizedProjectId !== expectedProjectId) return null;
  if (Date.now() - createdAt > 10 * 60 * 1000) return null;
  return {
    sessionId,
    projectId: normalizedProjectId,
    sourceActionId: String(parsed?.sourceActionId || '').trim(),
    components: parsed.components,
    createdAt
  };
}

export function readStoredCopilotTaskPrefill(userId: string, projectId?: string | null): CopilotTaskPrefillState | null {
  if (typeof window === 'undefined') return null;
  try {
    return parseCopilotTaskPrefill(JSON.parse(window.localStorage.getItem(copilotTaskPrefillStorageKey(userId, projectId)) || 'null'), projectId);
  } catch {
    return null;
  }
}

export function clearStoredCopilotTaskPrefill(userId: string, projectId?: string | null): void {
  if (typeof window === 'undefined') return;
  window.localStorage.removeItem(copilotTaskPrefillStorageKey(userId, projectId));
}

function writeStoredCopilotTaskPrefill(
  userId: string,
  projectId: string | null | undefined,
  value: Omit<CopilotTaskPrefillState, 'createdAt' | 'projectId'>
): void {
  if (typeof window === 'undefined') return;
  const normalizedProjectId = String(projectId || '').trim();
  if (!normalizedProjectId || !Array.isArray(value.components) || value.components.length === 0) return;
  window.localStorage.setItem(
    copilotTaskPrefillStorageKey(userId, normalizedProjectId),
    JSON.stringify({ ...value, projectId: normalizedProjectId, createdAt: Date.now() })
  );
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
  const next = { ...prev, ...patch };
  window.localStorage.setItem(copilotPanelStateStorageKey(userId), JSON.stringify(next));
  void upsertProjectCopilotState(userId, copilotPanelStateDbKey(), next as Record<string, unknown>);
}

export function readStoredCopilotOpen(input: {
  contextType: CopilotContextType;
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
}): boolean {
  if (typeof window === 'undefined') return false;
  const userId = String(input.userId || '').trim();
  if (userId) {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(copilotStateLocalStorageKey(userId, copilotOpenStateDbKey())) || 'null');
      if (parsed && typeof parsed === 'object' && typeof parsed.open === 'boolean') return parsed.open;
    } catch {
      // Fall back to the legacy open key below.
    }
  }
  return window.localStorage.getItem(copilotOpenStorageKey()) === 'true';
}

export function writeStoredCopilotOpen(
  input: { contextType: CopilotContextType; projectId?: string | null; projectTaskId?: string | null; userId?: string | null },
  open: boolean
): void {
  if (typeof window === 'undefined') return;
  window.localStorage.setItem(copilotOpenStorageKey(), open ? 'true' : 'false');
  const userId = String(input.userId || '').trim();
  if (userId) {
    window.localStorage.setItem(copilotStateLocalStorageKey(userId, copilotOpenStateDbKey()), JSON.stringify({ open }));
    void upsertProjectCopilotState(userId, copilotOpenStateDbKey(), { open }).catch(() => {
      // Non-blocking UI preference persistence.
    });
  }
}

function getInitialPanelPosition(stored: CopilotPanelState): { x: number; y: number } | null {
  const x = Number(stored.x);
  const y = Number(stored.y);
  if (Number.isFinite(x) && Number.isFinite(y)) return { x, y };
  if (typeof window === 'undefined') return null;
  return {
    x: Math.max(12, window.innerWidth - 560 - 24),
    y: Math.max(12, window.innerHeight - 680 - 24)
  };
}

function globalCopilotMessageScope(userId: string | null | undefined) {
  return {
    contextType: 'project_list' as const,
    projectId: null,
    projectTaskId: null,
    userId: userId || null,
    conversationScope: 'global'
  };
}

function currentContextMetadata(input: {
  contextType: CopilotContextType;
  projectId?: string | null;
  projectTaskId?: string | null;
}): Record<string, unknown> {
  return {
    source_context_type: input.contextType,
    source_project_id: input.projectId || null,
    source_project_task_id: input.projectTaskId || null
  };
}

function actionMatchesContext(action: CopilotPlanAction, contextType: CopilotContextType): boolean {
  const actionContext = String(action.payload?.contextType || '').trim();
  return !actionContext || actionContext === contextType;
}

function compactCopilotText(value: unknown, limit: number): string {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...`;
}

function buildCopilotConversationContext(messages: ProjectCopilotMessage[]): Record<string, unknown> {
  const visibleMessages = messages.filter((message) => message.role === 'user' || message.role === 'assistant');
  if (visibleMessages.length === 0) {
    return { compression: 'empty', recent_messages: [] };
  }
  const recent = visibleMessages.slice(-COPILOT_RECENT_CONTEXT_MESSAGES).map((message) => ({
    role: message.role,
    at: message.created_at,
    content: compactCopilotText(message.content, COPILOT_CONTEXT_MESSAGE_CHARS)
  }));
  const older = visibleMessages.slice(0, Math.max(0, visibleMessages.length - COPILOT_RECENT_CONTEXT_MESSAGES));
  const summarySource = older.slice(-COPILOT_SUMMARY_SOURCE_MESSAGES);
  const olderSummary = summarySource
    .map((message, index) => `${index + 1}. ${message.role}: ${compactCopilotText(message.content, 180)}`)
    .join('\n');
  return {
    compression: older.length > 0 ? 'summary_plus_recent' : 'recent_only',
    total_messages: visibleMessages.length,
    summarized_messages: older.length,
    summary_source_messages: summarySource.length,
    summary: older.length > 0 ? compactCopilotText(olderSummary, COPILOT_CONTEXT_SUMMARY_CHARS) : '',
    recent_messages: recent
  };
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
  onApplyPlanAction,
  onSendAttachments,
  onOpen,
  onClose
}: ProjectCopilotModalProps) {
  const storedPanelState = useMemo(() => readStoredCopilotPanelState(currentUserId), [currentUserId]);
  const draftScope = useMemo(
    () => ({ userId: currentUserId }),
    [currentUserId]
  );
  const messageScope = useMemo(() => globalCopilotMessageScope(currentUserId), [currentUserId]);
  const [messages, setMessages] = useState<ProjectCopilotMessage[]>(() =>
    readCachedProjectCopilotMessages(globalCopilotMessageScope(currentUserId))
  );
  const [activeSessionId, setActiveSessionId] = useState(() => readStoredCopilotActiveSessionLocal(currentUserId) || createSessionId());
  const [historyOpen, setHistoryOpen] = useState(Boolean(storedPanelState.historyOpen));
  const [draft, setDraft] = useState(() => readStoredCopilotDraftLocal(draftScope));
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingActions, setPendingActions] = useState<CopilotPlanAction[]>([]);
  const [applyingActionId, setApplyingActionId] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const focusComposerFrameRef = useRef<number | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    originX: number;
    originY: number;
  } | null>(null);
  const [position, setPosition] = useState<{ x: number; y: number } | null>(() => {
    return getInitialPanelPosition(storedPanelState);
  });
  const latestPositionRef = useRef<{ x: number; y: number } | null>(position);
  const sizeReadyRef = useRef(false);
  const [panelSize, setPanelSize] = useState<{ width: number; height: number } | null>(() => {
    const width = Number(storedPanelState.width);
    const height = Number(storedPanelState.height);
    return Number.isFinite(width) && Number.isFinite(height) ? { width, height } : null;
  });
  const [uploadedAttachments, setUploadedAttachments] = useState<CopilotUploadedAttachment[]>([]);
  const [mentionCaret, setMentionCaret] = useState(0);
  const [mentionActiveIndex, setMentionActiveIndex] = useState(0);
  const [mentionDismissedDraft, setMentionDismissedDraft] = useState<string | null>(null);

  const sourceContext = useMemo(
    () => currentContextMetadata({ contextType, projectId: projectId || null, projectTaskId: projectTaskId || null }),
    [contextType, projectId, projectTaskId]
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
    setPendingActions(
      filterAppliedPlanActions(nextMessages, sessionId, readPlanActions(latestAssistant?.metadata?.candidate_plan_actions))
        .filter((action) => actionMatchesContext(action, contextType))
    );
  }, [contextType]);

  const activateSession = useCallback((sessionId: string) => {
    const normalizedSessionId = String(sessionId || '').trim() || createSessionId();
    writeStoredCopilotActiveSession(currentUserId, normalizedSessionId);
    setActiveSessionId(normalizedSessionId);
    return normalizedSessionId;
  }, [currentUserId]);

  const focusComposer = useCallback(() => {
    if (typeof window === 'undefined') return;
    if (focusComposerFrameRef.current) {
      window.cancelAnimationFrame(focusComposerFrameRef.current);
    }
    focusComposerFrameRef.current = window.requestAnimationFrame(() => {
      focusComposerFrameRef.current = null;
      if (!open || sending || applyingActionId) return;
      textareaRef.current?.focus({ preventScroll: true });
    });
  }, [applyingActionId, open, sending]);

  useEffect(() => {
    return () => {
      if (focusComposerFrameRef.current) {
        window.cancelAnimationFrame(focusComposerFrameRef.current);
      }
    };
  }, []);

  const loadMessages = useCallback(async () => {
    if (!open) return;
    const cached = readCachedProjectCopilotMessages(messageScope);
    if (cached.length > 0) {
      setMessages(cached);
      setLoading(false);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      let loaded = await listProjectCopilotMessages(messageScope);
      const storedActiveSessionId = await readStoredCopilotActiveSession(currentUserId);
      const continuation = contextType === 'task_detail'
        ? await readCopilotContinuation(currentUserId, projectId)
        : null;
      let preferredSessionId = '';
      if (
        continuation &&
        projectTaskId &&
        continuation.sourceProjectTaskId &&
        continuation.sourceProjectTaskId !== (projectTaskId || 'task-detail:null')
      ) {
        preferredSessionId = continuation.sessionId;
        const alreadyInserted = loaded.some(
          (message) =>
            readSessionId(message) === continuation.sessionId &&
            String(message.metadata?.continued_into_project_task_id || '') === projectTaskId
        );
        if (!alreadyInserted && continuation.followUpActions.length > 0) {
          await insertProjectCopilotMessage({
            ...messageScope,
            userId: null,
            role: 'assistant',
            content: '新任务已经填写并保存为 draft。请检查当前 task 的组件和参数；如果没问题，可以点击下方按钮开始运行。',
            metadata: {
              ...sourceContext,
              session_id: continuation.sessionId,
              owner_user_id: currentUserId,
              candidate_plan_actions: continuation.followUpActions,
              continued_from_project_task_id: continuation.sourceProjectTaskId,
              continued_into_project_task_id: projectTaskId
            }
          });
        }
        loaded = await listProjectCopilotMessages(messageScope);
        clearCopilotContinuation(currentUserId, projectId);
      }
      setMessages(loaded);
      setActiveSessionId((currentSessionId) => {
        const sessionIds = Array.from(new Set(loaded.map(readSessionId)));
        const latestLoadedSessionId = loaded.length > 0 ? readSessionId(loaded[loaded.length - 1]) : '';
        const nextSessionId =
          (preferredSessionId && sessionIds.includes(preferredSessionId) ? preferredSessionId : '') ||
          (storedActiveSessionId && sessionIds.includes(storedActiveSessionId) ? storedActiveSessionId : '') ||
          (sessionIds.includes(currentSessionId) ? currentSessionId : '') ||
          latestLoadedSessionId ||
          sessionIds[sessionIds.length - 1] ||
          currentSessionId ||
          createSessionId();
        writeStoredCopilotActiveSession(currentUserId, nextSessionId);
        restoreSessionActions(loaded, nextSessionId);
        return nextSessionId;
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load Copilot messages.');
    } finally {
      setLoading(false);
    }
  }, [contextType, currentUserId, messageScope, open, projectId, projectTaskId, restoreSessionActions, sourceContext]);

  useEffect(() => {
    if (!open) return;
    void loadMessages();
  }, [loadMessages, open]);

  useEffect(() => {
    if (!currentUserId) return;
    void upsertProjectCopilotState(currentUserId, copilotOpenStateDbKey(), { open }).catch(() => {
      // Non-blocking UI preference persistence.
    });
  }, [currentUserId, open]);

  useEffect(() => {
    if (!currentUserId) return;
    let cancelled = false;
    void getProjectCopilotState(currentUserId, copilotPanelStateDbKey())
      .then((state) => {
        if (cancelled || !state) return;
        const nextX = Number(state.x);
        const nextY = Number(state.y);
        const nextWidth = Number(state.width);
        const nextHeight = Number(state.height);
        if (Number.isFinite(nextX) && Number.isFinite(nextY)) {
          latestPositionRef.current = { x: nextX, y: nextY };
          setPosition({ x: nextX, y: nextY });
        }
        if (Number.isFinite(nextWidth) && Number.isFinite(nextHeight) && nextWidth >= 300 && nextHeight >= 300) {
          setPanelSize({ width: nextWidth, height: nextHeight });
        }
        if (typeof state.historyOpen === 'boolean') {
          setHistoryOpen(state.historyOpen);
        }
      })
      .catch(() => {
        // Local cache is enough for smooth first paint.
      });
    return () => {
      cancelled = true;
    };
  }, [currentUserId]);

  useEffect(() => {
    if (!open) {
      setError(null);
      return;
    }
    window.setTimeout(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    }, 0);
  }, [sessionMessages.length, open]);

  useEffect(() => {
    if (!open || sending || applyingActionId) return;
    focusComposer();
  }, [applyingActionId, focusComposer, open, sending]);

  useEffect(() => {
    const localDraft = readStoredCopilotDraftLocal(draftScope);
    setDraft(localDraft);
    if (!currentUserId) return;
    let cancelled = false;
    void getProjectCopilotState(currentUserId, copilotDraftDbKey())
      .then((state) => {
        if (cancelled) return;
        const persistedDraft = typeof state?.draft === 'string' ? state.draft : '';
        if (persistedDraft && persistedDraft !== localDraft) {
          writeStoredCopilotDraftLocal(draftScope, persistedDraft);
          setDraft(persistedDraft);
        }
      })
      .catch(() => {
        // Local draft cache is enough when database state is unavailable.
      });
    return () => {
      cancelled = true;
    };
  }, [currentUserId, draftScope]);

  useEffect(() => {
    writeStoredCopilotDraftLocal(draftScope, draft);
    if (!currentUserId) return;
    const timer = window.setTimeout(() => {
      void upsertProjectCopilotState(currentUserId, copilotDraftDbKey(), { draft }).catch(() => {
        // Draft persistence should never block typing.
      });
    }, 350);
    return () => {
      window.clearTimeout(timer);
    };
  }, [currentUserId, draft, draftScope]);

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

  const syncMentionCaretFromTextarea = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    setMentionDismissedDraft(null);
    setMentionCaret(textarea.selectionStart ?? textarea.value.length);
  }, []);

  const sendMessage = async () => {
    const content = draft.trim();
    if (!content) return;
    const attachmentMetadata = uploadedAttachments.map((attachment) => ({
      id: attachment.id,
      name: attachment.name,
      type: attachment.type,
      size: attachment.size,
      mention: `@${attachment.name}`,
      mentioned: content.includes(`@${attachment.name}`)
    }));
    const conversationContext = buildCopilotConversationContext(sessionMessages);
    setSending(true);
    setError(null);
    setDraft('');
    writeStoredCopilotDraftLocal(draftScope, '');
    void deleteProjectCopilotState(currentUserId, copilotDraftDbKey());
    setPendingActions([]);
    try {
      const userMessage = await insertProjectCopilotMessage({
        ...messageScope,
        userId: currentUserId,
        role: 'user',
        content,
        metadata: { ...sourceContext, session_id: activeSessionId, attachments: attachmentMetadata }
      });
      setMessages((prev) => [...prev, { ...userMessage, username: currentUsername }]);

      let planActions: CopilotPlanAction[] = [];
      planActions = await requestCopilotPlanActions({
        contextType,
        contextPayload: {
          ...contextPayload,
          copilot_conversation: conversationContext,
          ...(attachmentMetadata.length > 0 ? { copilot_attachments: attachmentMetadata } : {})
        },
        userId: currentUserId,
        username: currentUsername,
        content
      });
      let assistantContent = '';
      try {
        assistantContent = await requestCopilotAssistant({
          contextType,
          contextPayload: {
              ...contextPayload,
              copilot_conversation: conversationContext,
              ...(attachmentMetadata.length > 0 ? { copilot_attachments: attachmentMetadata } : {}),
              ...(planActions.length > 0 ? { candidate_plan_actions: planActions } : {})
          },
          userId: currentUserId,
          username: currentUsername,
          content
        });
      } catch (err) {
        if (planActions.length === 0) throw err;
        assistantContent = '已生成确认操作。请检查下方按钮，确认后执行。';
      }
      const assistantMessage = await insertProjectCopilotMessage({
        ...messageScope,
        userId: null,
        role: 'assistant',
        content: assistantContent,
        metadata: { ...sourceContext, session_id: activeSessionId, owner_user_id: currentUserId, candidate_plan_actions: planActions }
      });
      setMessages((prev) => [...prev, assistantMessage]);
      setPendingActions(planActions.filter((action) => actionMatchesContext(action, contextType)));
      focusComposer();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send Copilot message.');
      setDraft(content);
      focusComposer();
    } finally {
      setSending(false);
    }
  };

  const addUploadedFiles = useCallback((files: FileList | File[]) => {
    const rows = Array.from(files);
    if (rows.length === 0) return;
    setUploadedAttachments((prev) => {
      const seen = new Set(prev.map((item) => `${item.name}:${item.size}:${item.type}`));
      const next = [...prev];
      for (const file of rows) {
        const key = `${file.name}:${file.size}:${file.type}`;
        if (seen.has(key)) continue;
        seen.add(key);
        next.push({
          id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
          file,
          name: file.name,
          type: file.type || 'application/octet-stream',
          size: file.size
        });
      }
      return next.slice(-8);
    });
    if (typeof window !== 'undefined') {
      window.requestAnimationFrame(() => {
        syncMentionCaretFromTextarea();
        textareaRef.current?.focus({ preventScroll: true });
      });
    }
  }, [syncMentionCaretFromTextarea]);

  const insertAttachmentMention = useCallback((attachment: CopilotUploadedAttachment) => {
    setDraft((prev) => {
      const mention = `@${attachment.name}`;
      if (prev.includes(mention)) return prev;
      const separator = prev.trim() ? ' ' : '';
      return `${prev}${separator}${mention}`;
    });
    focusComposer();
  }, [focusComposer]);

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
    const options = uploadedAttachments
      .filter((attachment) => {
        if (!normalizedQuery) return true;
        return attachment.name.toLowerCase().includes(normalizedQuery);
      })
      .slice(0, 6);
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
    setDraft(nextDraft);
    setMentionDismissedDraft(null);
    setMentionCaret(nextCaret);
    if (typeof window !== 'undefined') {
      window.requestAnimationFrame(() => {
        textareaRef.current?.focus({ preventScroll: true });
        textareaRef.current?.setSelectionRange(nextCaret, nextCaret);
      });
    }
  }, [attachmentMentionState, draft]);

  const removeUploadedAttachment = useCallback((attachmentId: string) => {
    setUploadedAttachments((prev) => prev.filter((item) => item.id !== attachmentId));
  }, []);

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
    activateSession(nextSessionId);
    setDraft('');
    writeStoredCopilotDraftLocal(draftScope, '');
    void deleteProjectCopilotState(currentUserId, copilotDraftDbKey());
    setPendingActions([]);
    setError(null);
    focusComposer();
  };

  const selectSession = (sessionId: string) => {
    activateSession(sessionId);
    setDraft('');
    writeStoredCopilotDraftLocal(draftScope, '');
    void deleteProjectCopilotState(currentUserId, copilotDraftDbKey());
    setError(null);
    restoreSessionActions(messages, sessionId);
    focusComposer();
  };

  const deleteSession = async (sessionId: string) => {
    if (!window.confirm('Delete this chat history?')) return;
    setError(null);
    try {
      const messageIds = messages
        .filter((message) => readSessionId(message) === sessionId)
        .map((message) => message.id);
      await deleteProjectCopilotMessagesBySession({ ...messageScope, sessionId, userId: currentUserId, messageIds });
      setMessages((prev) => prev.filter((message) => readSessionId(message) !== sessionId));
      if (sessionId === activeSessionId) {
        clearStoredCopilotActiveSession(currentUserId);
        startNewChat();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete chat history.');
    }
  };

  const applyAction = async (action: CopilotPlanAction) => {
    if (!onApplyPlanAction && !(action.id === 'task_detail:apply_copilot_attachments' && onSendAttachments)) return;
    setApplyingActionId(action.id);
    setError(null);
    try {
      if (
        (contextType === 'task_detail' &&
          projectTaskId &&
          (action.id === 'task_detail:submit_current' || action.id === 'task_detail:apply_patch_and_submit')) ||
        (contextType === 'task_list' && action.id === 'tasks:create_with_sequence')
      ) {
        const actionComponents = Array.isArray(action.payload?.components) ? action.payload.components : [];
        if (contextType === 'task_list' && action.id === 'tasks:create_with_sequence') {
          writeStoredCopilotTaskPrefill(currentUserId, projectId, {
            sessionId: activeSessionId,
            sourceActionId: action.id,
            components: actionComponents
          });
        }
        const firstProteinComponent = actionComponents.find((component) => {
          if (!component || typeof component !== 'object') return false;
          return String((component as Record<string, unknown>).type || '').trim() === 'protein';
        }) as Record<string, unknown> | undefined;
        const followUpActions = contextType === 'task_list'
          ? [buildSubmitCurrentFollowUpAction({
              sourceActionId: action.id,
              sequence: firstProteinComponent?.sequence ?? action.payload?.protein_sequence,
              description: '新任务内容已填写完成，确认后开始结构预测。'
            })]
          : [];
        writeCopilotContinuation(currentUserId, projectId, {
          sessionId: activeSessionId,
          sourceContextType: contextType,
          sourceProjectTaskId: projectTaskId || `__${contextType}__`,
          appliedAction: action,
          followUpActions
        });
      }
      if (action.id === 'task_detail:apply_copilot_attachments') {
        if (!onSendAttachments) throw new Error('This page cannot apply Copilot file attachments.');
        const rawApplications = Array.isArray(action.payload?.attachmentApplications)
          ? action.payload.attachmentApplications
          : [];
        const applications = rawApplications
          .map((item) => {
            if (!item || typeof item !== 'object') return null;
            const row = item as Record<string, unknown>;
            const role = String(row.role || '').trim();
            if (role !== 'target' && role !== 'ligand' && role !== 'template') return null;
            return {
              attachmentId: String(row.attachmentId || '').trim(),
              fileName: String(row.fileName || '').trim(),
              role
            } as CopilotAttachmentApplication;
          })
          .filter((item): item is CopilotAttachmentApplication => Boolean(item?.attachmentId || item?.fileName));
        if (applications.length === 0) throw new Error('Copilot did not provide a file-role plan to apply.');
        const selectedAttachments = uploadedAttachments.filter((attachment) =>
          applications.some((item) => item.attachmentId === attachment.id || item.fileName === attachment.name)
        );
        if (selectedAttachments.length === 0) throw new Error('The referenced uploaded files are no longer available in this chat composer.');
        await onSendAttachments(selectedAttachments, String(action.payload?.sourceContent || ''), applications);
      } else {
        if (!onApplyPlanAction) throw new Error('This Copilot action cannot be applied on the current page.');
        await onApplyPlanAction(action);
      }
      setPendingActions((prev) => prev.filter((item) => item.id !== action.id));
      const receipt = await insertProjectCopilotMessage({
        ...messageScope,
        userId: currentUserId,
        role: 'system',
        content: `Confirmed action: ${action.label}`,
        metadata: { ...sourceContext, session_id: activeSessionId, owner_user_id: currentUserId, applied_action: action }
      });
      let nextMessages = [...messages, receipt];
      if (contextType === 'task_detail' && action.id === 'task_detail:apply_parameter_patch') {
        const followUpAction = buildSubmitCurrentFollowUpAction({
          sourceActionId: action.id,
          description: '组件或参数已更新完成。请检查当前 task；如果没问题，可以点击下方按钮开始运行。'
        });
        const followUp = await insertProjectCopilotMessage({
          ...messageScope,
          userId: null,
          role: 'assistant',
          content: '组件或参数已更新完成。请检查当前 task；如果没问题，可以点击下方按钮开始运行。',
          metadata: { ...sourceContext, session_id: activeSessionId, owner_user_id: currentUserId, candidate_plan_actions: [followUpAction] }
        });
        nextMessages = [...nextMessages, followUp];
        setPendingActions([followUpAction]);
      }
      setMessages(nextMessages);
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
          <div className="copilot-action-stack">
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
          </div>
        ) : null}

        <div className="copilot-composer">
          <div className="copilot-input-shell">
            {uploadedAttachments.length > 0 ? (
              <div className="copilot-attachment-tray" aria-label="Attached files">
                {uploadedAttachments.map((attachment) => (
                  <button
                    className="copilot-attachment-chip"
                    type="button"
                    key={attachment.id}
                    onClick={() => insertAttachmentMention(attachment)}
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
                        removeUploadedAttachment(attachment.id);
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter' || event.key === ' ') {
                          event.preventDefault();
                          event.stopPropagation();
                          removeUploadedAttachment(attachment.id);
                        }
                      }}
                    >
                      <X size={11} />
                    </span>
                  </button>
                ))}
              </div>
            ) : null}
            {attachmentMentionState ? (
              <div className="copilot-mention-menu" role="listbox" aria-label="File mentions">
                {attachmentMentionState.options.map((attachment, index) => (
                  <button
                    key={attachment.id}
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
              <button
                className="copilot-attach-btn"
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={sending}
                aria-label="Attach file"
                title="Attach file"
              >
                <Plus size={16} />
              </button>
              <input
                ref={fileInputRef}
                className="copilot-file-input"
                type="file"
                multiple
                accept=".pdb,.ent,.cif,.mmcif,.sdf,.sd,.mol2,.mol,.txt,.csv,.tsv"
                onChange={(event) => {
                  if (event.target.files) addUploadedFiles(event.target.files);
                  event.currentTarget.value = '';
                }}
              />
              <textarea
                ref={textareaRef}
                value={draft}
                rows={1}
                onChange={(event) => {
                  setDraft(event.target.value);
                  setMentionDismissedDraft(null);
                  setMentionCaret(event.target.selectionStart ?? event.target.value.length);
                  if (typeof window !== 'undefined') {
                    window.requestAnimationFrame(syncMentionCaretFromTextarea);
                  }
                }}
                onKeyDown={(event) => {
                  if (attachmentMentionState) {
                    if (event.key === 'ArrowDown') {
                      event.preventDefault();
                      setMentionActiveIndex((index) => (index + 1) % attachmentMentionState.options.length);
                      return;
                    }
                    if (event.key === 'ArrowUp') {
                      event.preventDefault();
                      setMentionActiveIndex((index) =>
                        (index - 1 + attachmentMentionState.options.length) % attachmentMentionState.options.length
                      );
                      return;
                    }
                    if (event.key === 'Enter' || event.key === 'Tab') {
                      event.preventDefault();
                      insertAttachmentMentionAtCaret(attachmentMentionState.options[mentionActiveIndex] || attachmentMentionState.options[0]);
                      return;
                    }
                    if (event.key === 'Escape') {
                      event.preventDefault();
                      setMentionDismissedDraft(draft);
                      setMentionCaret(-1);
                      return;
                    }
                  }
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void sendMessage();
                  }
                }}
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
                placeholder="输入消息…"
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
      </div>
    </div>
  );
}
