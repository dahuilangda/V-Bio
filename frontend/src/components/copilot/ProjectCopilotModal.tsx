import { Bot } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react';
import {
  deleteProjectCopilotMessagesBySession,
  deleteProjectCopilotState,
  fetchProjectCopilotMessageTrace,
  getProjectCopilotState,
  insertProjectCopilotMessage,
  readCachedProjectCopilotMessages,
  listProjectCopilotMessages,
  upsertProjectCopilotState
} from '../../api/supabaseLite';
import { getCopilotConfig, getCopilotSettings, saveCopilotSettings, streamCopilotTurn, submitCopilotSteering, testCopilotSettings } from '../../api/copilotApi';
import type { CopilotTestResult } from '../../api/copilotApi';
import type { CopilotContextType, CopilotPlanAction, CopilotTraceStep, ProjectCopilotMessage } from '../../types/models';
import { useAuth } from '../../hooks/useAuth';
import { useOverlayPresence } from '../ui/OverlayContext';
import { collectCopilotMemory, readActionResolutions, readSessionId, type CopilotActionResolutionStatus } from './copilotTraceUi';
import {
  appendInputHistory,
  readStoredInputHistory,
  writeStoredInputHistory,
  type InputHistoryNav
} from './copilotInputHistory';
import './ProjectCopilotModal.css';
import { createCopilotDraftStore } from './copilotDraftStore';
import { buildCopilotConversationContext } from './copilotConversationContext';
import { CopilotComposer } from './CopilotComposer';
import { CopilotPanelShell } from './CopilotPanelShell';

interface ProjectCopilotModalProps {
  isOpen: boolean;
  title: string;
  subtitle: string;
  contextType: CopilotContextType;
  projectId?: string | null;
  projectTaskId?: string | null;
  currentUserId: string;
  currentUsername: string;
  contextPayload: Record<string, unknown>;
  applyPlanAction?: (action: CopilotPlanAction) => void | Promise<void | string>;
  sendAttachmentsAction?: (
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

// Re-exported from copilotConversationContext.ts; shared with the composer's completer.
export { buildCopilotConversationContext };

// localStorage is immediate; this debounce only bounds the cross-device DB write.
const COPILOT_DRAFT_DB_DEBOUNCE_MS = 1500;

function createSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `session-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function readPlanActions(value: unknown): CopilotPlanAction[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const actions: CopilotPlanAction[] = [];
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const action = item as CopilotPlanAction;
    const key = planActionKey(action);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    actions.push(action);
  }
  return actions.sort(comparePlanActions);
}


function planActionKey(action: CopilotPlanAction): string {
  const planId = String(action.plan_id || '').trim();
  const operationId = String(action.operation_id || '').trim();
  return planId && operationId ? `${planId}:${operationId}` : '';
}

function comparePlanActions(left: CopilotPlanAction, right: CopilotPlanAction): number {
  const leftSequence = Number(left.sequence);
  const rightSequence = Number(right.sequence);
  if (Number.isFinite(leftSequence) && Number.isFinite(rightSequence) && leftSequence !== rightSequence) {
    return leftSequence - rightSequence;
  }
  return planActionKey(left).localeCompare(planActionKey(right));
}


function readSessionResolutionMap(messages: ProjectCopilotMessage[], sessionId: string): Map<string, CopilotActionResolutionStatus> {
  const resolutions = new Map<string, CopilotActionResolutionStatus>();
  for (const message of messages) {
    if (readSessionId(message) !== sessionId) continue;
    for (const resolution of readActionResolutions(message)) {
      resolutions.set(`${resolution.plan_id}:${resolution.operation_id}`, resolution.status);
    }
  }
  return resolutions;
}

function filterResolvedPlanActions(messages: ProjectCopilotMessage[], sessionId: string, actions: CopilotPlanAction[]): CopilotPlanAction[] {
  if (actions.length === 0) return [];
  const resolutions = readSessionResolutionMap(messages, sessionId);
  return actions.filter((action) => !resolutions.has(planActionKey(action)));
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

// Settings form persistence (proxy / LLM URL / model — NOT api_key).

const COPILOT_SETTINGS_FORM_KEY = 'vbio:copilot-settings-form:v1';

export interface SettingsFormValues {
  proxy: string;
  api_url: string;
  api_key: string;
  model: string;
}

function readStoredSettingsForm(): SettingsFormValues {
  if (typeof window === 'undefined') return { proxy: '', api_url: '', api_key: '', model: '' };
  try {
    const raw = window.localStorage.getItem(COPILOT_SETTINGS_FORM_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<SettingsFormValues>;
      // Never restore api_key from localStorage — it's a secret.
      return { proxy: parsed.proxy || '', api_url: parsed.api_url || '', api_key: '', model: parsed.model || '' };
    }
  } catch {
    // ignore parse errors
  }
  return { proxy: '', api_url: '', api_key: '', model: '' };
}

function writeStoredSettingsForm(form: SettingsFormValues): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(
      COPILOT_SETTINGS_FORM_KEY,
      JSON.stringify({ proxy: form.proxy, api_url: form.api_url, model: form.model })
    );
  } catch {
    // ignore quota errors
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
  void upsertProjectCopilotState(userId, copilotActiveSessionStateDbKey(), { session_id: normalizedSessionId }).catch(() => {});
}

function clearStoredCopilotActiveSession(userId: string): void {
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(copilotActiveSessionStorageKey(userId));
  }
  void deleteProjectCopilotState(userId, copilotActiveSessionStateDbKey());
}

// Auto-continuation handoff across page navigation: an apply that navigates unmounts
// this page before the continuation effect fires, so the armed entry is ALSO written
// to sessionStorage (dies with the tab, TTL-bounded) for the next page's modal to
// pick up on mount.

interface CopilotContinuation {
  planId: string;
  /** The arming action's operation id — the receipt the continuation continues from. */
  operationId: string;
  sessionId: string;
  outcome: 'applied' | 'failed';
  at: number;
}

const COPILOT_CONTINUATION_STORAGE_PREFIX = 'vbio:copilot-continuation:';
const COPILOT_CONTINUATION_TTL_MS = 120_000;
// Hard cap per mounted panel so a runaway planner loop hands control back to the user.
const AUTO_CONTINUATION_CAP = 5;

// Synthetic user turns that resume the agent loop: applied → continue,
// failed → diagnose-and-recover, never claim success on a failed receipt.
const COPILOT_CONTINUATION_MESSAGE_APPLIED =
  'The confirmed actions were applied (see the receipt); do not repeat them. Continue the plan. If the goal is met, summarize honestly from the receipt and stop.';
const COPILOT_CONTINUATION_MESSAGE_FAILED =
  'Some of the confirmed actions failed (see the error field in the receipt). Diagnose the failure first, fix prerequisites or parameters, then re-propose those actions; use a valid alternative if unfixable; only report the blocker honestly if no path remains. Do not repeat failed actions verbatim, and never claim success when the receipt says failed.';

function copilotContinuationStorageKey(userId: string): string {
  return `${COPILOT_CONTINUATION_STORAGE_PREFIX}${userId}`;
}

function readStoredCopilotContinuation(userId: string): CopilotContinuation | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.sessionStorage.getItem(copilotContinuationStorageKey(userId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<CopilotContinuation>;
    const planId = String(parsed.planId || '').trim();
    const operationId = String(parsed.operationId || '').trim();
    const sessionId = String(parsed.sessionId || '').trim();
    const outcome = parsed.outcome === 'failed' ? 'failed' : 'applied';
    const at = Number(parsed.at);
    if (!planId || !operationId || !sessionId || !Number.isFinite(at)) return null;
    if (Date.now() - at > COPILOT_CONTINUATION_TTL_MS) {
      window.sessionStorage.removeItem(copilotContinuationStorageKey(userId));
      return null;
    }
    return { planId, operationId, sessionId, outcome, at };
  } catch {
    return null;
  }
}

function writeStoredCopilotContinuation(userId: string, continuation: CopilotContinuation): void {
  if (typeof window === 'undefined' || !continuation.planId || !continuation.operationId || !continuation.sessionId) return;
  try {
    window.sessionStorage.setItem(copilotContinuationStorageKey(userId), JSON.stringify(continuation));
  } catch {
    // quota errors lose only the cross-page handoff, never the current page's continuation
  }
}

function clearStoredCopilotContinuation(userId: string): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.removeItem(copilotContinuationStorageKey(userId));
  } catch {
    // ignore
  }
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
  void upsertProjectCopilotState(userId, copilotPanelStateDbKey(), next as Record<string, unknown>).catch(() => {});
}

type CopilotTaskPrefillState = {
  sessionId: string;
  projectId: string;
  sourceActionId: string;
  components: unknown[];
  createdAt: number;
};

function copilotTaskPrefillStorageKey(userId: string, projectId?: string | null): string {
  return `vbio:copilot-task-prefill:v1:${String(userId || 'anonymous').trim().toLowerCase() || 'anonymous'}:${String(projectId || 'project-null')}`;
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

// Height of the sticky .top-nav; the panel must never slide underneath it.
export const TOP_CHROME_PX = 64;

function clampPanelPosition(pos: { x: number; y: number }): { x: number; y: number } {
  // Clamp so a position persisted on a large screen can't strand the panel
  // off-viewport or hide the header under the top-nav.
  if (typeof window === 'undefined') return pos;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  return {
    x: Math.min(Math.max(8, pos.x), Math.max(8, vw - 280)),
    y: Math.min(Math.max(TOP_CHROME_PX + 8, pos.y), Math.max(TOP_CHROME_PX + 8, vh - 180))
  };
}

function getInitialPanelPosition(stored: CopilotPanelState): { x: number; y: number } | null {
  const x = Number(stored.x);
  const y = Number(stored.y);
  if (Number.isFinite(x) && Number.isFinite(y)) return clampPanelPosition({ x, y });
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
  // A plan spans several host pages: a page renders only the actions whose source
  // contextType matches it, so an unrelated page's pending action never leaks here.
  const actionContext = String(action.payload?.contextType || '').trim();
  if (actionContext) {
    return actionContext === contextType;
  }
  // Legacy actions carry no contextType; show them only on project_list so they don't leak.
  return contextType === 'project_list';
}

export function ProjectCopilotModal({
  isOpen,
  title,
  subtitle,
  contextType,
  projectId = null,
  projectTaskId = null,
  currentUserId,
  currentUsername,
  contextPayload,
  applyPlanAction,
  sendAttachmentsAction,
  onOpen,
  onClose
}: ProjectCopilotModalProps) {
  // Collapse to a dock chip while a host-page modal overlay is open; null without a provider.
  const overlayPresence = useOverlayPresence();
  const suppressedByOverlay = Boolean(overlayPresence?.hasOpenOverlay);
  // Mobile-width flag: suppresses the persisted desktop size/position inline styles.
  const [isMobileViewport, setIsMobileViewport] = useState(
    typeof window !== 'undefined' ? window.innerWidth < 768 : false
  );
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const mql = window.matchMedia('(max-width: 767px)');
    const handler = (e: MediaQueryListEvent) => setIsMobileViewport(e.matches);
    mql.addEventListener('change', handler);
    setIsMobileViewport(mql.matches);
    return () => mql.removeEventListener('change', handler);
  }, []);
  // Soft-keyboard handling via visualViewport (dvh alone is unreliable): bind the
  // panel's height/top to the visible region so the composer sits above the keyboard.
  const [visualViewportHeight, setVisualViewportHeight] = useState<number | null>(null);
  const [visualViewportTop, setVisualViewportTop] = useState(0);
  useEffect(() => {
    // Mobile-only: visualViewport events fire on desktop scroll/pinch for nothing; reset when leaving mobile.
    if (typeof window === 'undefined' || !isMobileViewport) {
      setVisualViewportHeight(null);
      setVisualViewportTop(0);
      return;
    }
    const vv = window.visualViewport;
    if (!vv) return;
    let pending = false;
    // Coalesce rapid resize frames into one update per rAF.
    const update = () => {
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(() => {
        pending = false;
        setVisualViewportHeight(vv.height);
        setVisualViewportTop(vv.offsetTop ?? vv.pageTop ?? 0);
      });
    };
    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => {
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
    };
  }, [isMobileViewport]);
  const storedPanelState = useMemo(() => readStoredCopilotPanelState(currentUserId), [currentUserId]);
  const draftScope = useMemo(
    () => ({ userId: currentUserId }),
    [currentUserId]
  );
  const messageScope = useMemo(() => globalCopilotMessageScope(currentUserId), [currentUserId]);
  const { session: authSession, ensureManagementSession } = useAuth();
  // Messages hydrate lazily inside loadMessages; parsing the cache at mount would freeze navigation.
  const [messages, setMessages] = useState<ProjectCopilotMessage[]>([]);
  const [activeSessionId, setActiveSessionId] = useState(() => readStoredCopilotActiveSessionLocal(currentUserId) || createSessionId());
  const [historyOpen, setHistoryOpen] = useState(Boolean(storedPanelState.historyOpen));
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsForm, setSettingsForm] = useState<SettingsFormValues>(() => readStoredSettingsForm());
  const [settingsMaskedKey, setSettingsMaskedKey] = useState('');
  const [settingsHasKey, setSettingsHasKey] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsTesting, setSettingsTesting] = useState(false);
  const [settingsTestResult, setSettingsTestResult] = useState<CopilotTestResult | null>(null);
  const [settingsError, setSettingsError] = useState('');
  const [settingsSaved, setSettingsSaved] = useState(false);
  // The draft lives in the store (copilotDraftStore.ts), not modal state; a keystroke
  // must not re-render this panel.
  const [draftStore] = useState(() => createCopilotDraftStore(readStoredCopilotDraftLocal(draftScope)));
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const [plusMenuOpen, setPlusMenuOpen] = useState(false);
  const plusMenuRef = useRef<HTMLDivElement | null>(null);
  const [liveTrace, setLiveTrace] = useState<CopilotTraceStep[]>([]);
  // Stable timestamp so the meta header doesn't pop in when the live bubble is replaced.
  const [streamStartedAt, setStreamStartedAt] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pendingActions, setPendingActions] = useState<CopilotPlanAction[]>([]);
  const [applyingActionKey, setApplyingActionKey] = useState<string | null>(null);
  const [bulkAction, setBulkAction] = useState<'apply' | 'cancel' | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Auto-scroll only when already near the bottom, so reading history isn't yanked away.
  const stickToBottomRef = useRef(true);
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
  // ↑/↓ sent-input history + navigation cursor; the composer drives the interaction.
  const inputHistoryRef = useRef<string[]>([]);
  const historyNavRef = useRef<InputHistoryNav | null>(null);
  // In-flight turn identity + steering texts.
  const activeTurnKeyRef = useRef<string | null>(null);
  const [steeredTurnTexts, setSteeredTurnTexts] = useState<string[]>([]);
  const [completionEnabled, setCompletionEnabled] = useState(false);
  const contextPayloadRef = useRef(contextPayload);

  const sourceContext = useMemo(
    () => currentContextMetadata({ contextType, projectId: projectId || null, projectTaskId: projectTaskId || null }),
    [contextType, projectId, projectTaskId]
  );

  const sessionMessages = useMemo(
    () => messages.filter((message) => readSessionId(message) === activeSessionId),
    [activeSessionId, messages]
  );

  // Render only the newest slice; older messages load in chunks (markdown re-parse
  // on remount is expensive on a long session).
  const MESSAGE_WINDOW = 40;
  const [visibleMessageCount, setVisibleMessageCount] = useState(MESSAGE_WINDOW);
  useEffect(() => {
    setVisibleMessageCount(MESSAGE_WINDOW);
  }, [activeSessionId]);
  const visibleSessionMessages = useMemo(() => {
    if (sessionMessages.length <= visibleMessageCount) return sessionMessages;
    return sessionMessages.slice(sessionMessages.length - visibleMessageCount);
  }, [sessionMessages, visibleMessageCount]);

  const chatSessions = useMemo(() => {
    // Single pass over the chronological transcript.
    const sessions = new Map<string, { firstUserContent: string; updatedAt: string }>();
    for (const message of messages) {
      const sessionId = readSessionId(message);
      const entry = sessions.get(sessionId) || { firstUserContent: '', updatedAt: '' };
      if (!entry.firstUserContent && message.role === 'user') {
        entry.firstUserContent = String(message.content || '');
      }
      entry.updatedAt = String(message.created_at || '');
      sessions.set(sessionId, entry);
    }
    return Array.from(sessions.entries())
      .map(([id, { firstUserContent, updatedAt }]) => {
        const content = firstUserContent.replace(/\s+/g, ' ').trim();
        const title = content
          ? content.length > 32
            ? `${content.slice(0, 32)}...`
            : content
          : id === 'default'
            ? 'Previous chat'
            : 'New chat';
        return { id, title, updatedAt };
      })
      .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  }, [messages]);

  const restoreSessionActions = useCallback((nextMessages: ProjectCopilotMessage[], sessionId: string) => {
    const latestAssistant = [...nextMessages]
      .reverse()
      .find((message) => readSessionId(message) === sessionId && message.role === 'assistant');
    setPendingActions(
      filterResolvedPlanActions(nextMessages, sessionId, readPlanActions(latestAssistant?.metadata?.candidate_plan_actions))
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
      if (!isOpen || sending || applyingActionKey || bulkAction) return;
      textareaRef.current?.focus({ preventScroll: true });
    });
  }, [applyingActionKey, bulkAction, isOpen, sending]);

  useEffect(() => {
    return () => {
      if (focusComposerFrameRef.current) {
        window.cancelAnimationFrame(focusComposerFrameRef.current);
      }
    };
  }, []);

  // Set on startNewChat/selectSession so loadMessages doesn't override the user's choice.
  const sessionSwitchRef = useRef(false);
  // Why a send was aborted: only 'user' restores the draft; the others would leak
  // it into a different session.
  const abortReasonRef = useRef<'user' | 'session-switch' | 'close' | 'unmount'>('user');

  // Read the session id through a ref so loadMessages stays stable and loads once per open.
  const activeSessionIdRef = useRef(activeSessionId);
  activeSessionIdRef.current = activeSessionId;

  const loadMessages = useCallback(async () => {
    if (!isOpen) return;
    const activeSessionIdNow = activeSessionIdRef.current;
    // Hydrate from the cache first, then refresh from the server; the cache is an
    // accelerator, not a source of truth.
    let cached: ProjectCopilotMessage[] = [];
    try {
      cached = await readCachedProjectCopilotMessages(messageScope);
    } catch (cacheError) {
      console.error('[copilot] transcript cache unavailable — loading from server', cacheError);
    }
    if (cached.length > 0) {
      setMessages(cached);
      setLoading(false);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      const loaded = await listProjectCopilotMessages(messageScope);
      setMessages(loaded);
      // A just-switched session wins; don't override it back to an old one.
      if (sessionSwitchRef.current) {
        sessionSwitchRef.current = false;
        restoreSessionActions(loaded, activeSessionIdRef.current);
        return;
      }
      const storedActiveSessionId = await readStoredCopilotActiveSession(currentUserId);
      // Resolve the session id OUTSIDE the setState updater.
      const sessionIds = Array.from(new Set(loaded.map(readSessionId)));
      const latestLoadedSessionId = loaded.length > 0 ? readSessionId(loaded[loaded.length - 1]) : '';
      const currentSessionId = activeSessionIdNow;
      const nextSessionId =
        (storedActiveSessionId && sessionIds.includes(storedActiveSessionId) ? storedActiveSessionId : '') ||
        (sessionIds.includes(currentSessionId) ? currentSessionId : '') ||
        latestLoadedSessionId ||
        sessionIds[sessionIds.length - 1] ||
        currentSessionId ||
        createSessionId();
      writeStoredCopilotActiveSession(currentUserId, nextSessionId);
      setActiveSessionId(nextSessionId);
      restoreSessionActions(loaded, nextSessionId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load Copilot messages.');
    } finally {
      setLoading(false);
    }
  }, [currentUserId, messageScope, isOpen, restoreSessionActions]);

  useEffect(() => {
    if (!isOpen) return;
    void loadMessages();
  }, [loadMessages, isOpen]);

  useEffect(() => {
    if (!currentUserId) return;
    void upsertProjectCopilotState(currentUserId, copilotOpenStateDbKey(), { open: isOpen }).catch(() => {
      // Non-blocking UI preference persistence.
    });
  }, [currentUserId, isOpen]);

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
          const clamped = clampPanelPosition({ x: nextX, y: nextY });
          latestPositionRef.current = clamped;
          setPosition(clamped);
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

  // Pin to the newest content unless the user scrolled up; rAF-coalesced to avoid
  // forced layouts per trace step.
  const scrollRafRef = useRef<number | null>(null);
  const scheduleStickToBottom = useCallback(() => {
    if (scrollRafRef.current !== null) return;
    scrollRafRef.current = window.requestAnimationFrame(() => {
      scrollRafRef.current = null;
      if (!stickToBottomRef.current) return;
      const el = scrollRef.current;
      if (!el) return;
      el.scrollTo({ top: el.scrollHeight });
    });
  }, []);
  useEffect(() => {
    if (!isOpen) {
      setError(null);
      return;
    }
    scheduleStickToBottom();
  }, [sessionMessages.length, liveTrace.length, sending, isOpen, scheduleStickToBottom]);

  // Re-pin when the keyboard shrinks the panel so the composer doesn't cover the newest message.
  useEffect(() => {
    if (!isOpen || visualViewportHeight == null) return;
    scheduleStickToBottom();
  }, [visualViewportHeight, isOpen, scheduleStickToBottom]);

  useEffect(() => () => {
    if (scrollRafRef.current !== null) {
      window.cancelAnimationFrame(scrollRafRef.current);
      scrollRafRef.current = null;
    }
  }, []);

  const handleMessagesScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  useEffect(() => {
    if (!isOpen || sending || applyingActionKey || bulkAction) return;
    // Desktop-only: programmatic focus on mobile would re-open the soft keyboard.
    if (isMobileViewport) return;
    focusComposer();
  }, [applyingActionKey, bulkAction, focusComposer, isMobileViewport, isOpen, sending]);

  useEffect(() => {
    const localDraft = readStoredCopilotDraftLocal(draftScope);
    draftStore.set(localDraft);
    if (!currentUserId) return;
    let cancelled = false;
    void getProjectCopilotState(currentUserId, copilotDraftDbKey())
      .then((state) => {
        if (cancelled) return;
        const persistedDraft = typeof state?.draft === 'string' ? state.draft : '';
        if (persistedDraft && persistedDraft !== localDraft) {
          writeStoredCopilotDraftLocal(draftScope, persistedDraft);
          draftStore.set(persistedDraft);
        }
      })
      .catch(() => {
        // Local draft cache is enough when database state is unavailable.
      });
    return () => {
      cancelled = true;
    };
  }, [currentUserId, draftScope, draftStore]);

  // Draft persistence outside React state: localStorage immediately, DB debounced
  // (an emptied draft deletes the row). Build the request inside the timer so the
  // POST waits out the debounce.
  const draftPersistPendingRef = useRef<{ timer: number | null; value: string | null }>({ timer: null, value: null });
  const flushDraftToDb = useCallback((value: string) => {
    if (!currentUserId) return;
    const pending = draftPersistPendingRef.current;
    if (pending.timer !== null) window.clearTimeout(pending.timer);
    pending.value = value;
    pending.timer = window.setTimeout(() => {
      pending.timer = null;
      const v = pending.value;
      pending.value = null;
      if (v === null) return;
      void (v
        ? upsertProjectCopilotState(currentUserId, copilotDraftDbKey(), { draft: v })
        : deleteProjectCopilotState(currentUserId, copilotDraftDbKey())
      ).catch(() => {
        // Draft persistence should never block typing.
      });
    }, COPILOT_DRAFT_DB_DEBOUNCE_MS);
  }, [currentUserId]);
  const persistCopilotDraft = useCallback((value: string) => {
    writeStoredCopilotDraftLocal(draftScope, value);
    flushDraftToDb(value);
  }, [draftScope, flushDraftToDb]);
  useEffect(() => {
    return draftStore.subscribe((value) => persistCopilotDraft(value));
  }, [draftStore, persistCopilotDraft]);
  useEffect(() => () => {
    // Flush the pending write on unmount so a stale DB draft can't resurrect.
    const pending = draftPersistPendingRef.current;
    if (pending.timer !== null) {
      window.clearTimeout(pending.timer);
      pending.timer = null;
    }
    const v = pending.value;
    pending.value = null;
    if (v === null || !currentUserId) return;
    void (v
      ? upsertProjectCopilotState(currentUserId, copilotDraftDbKey(), { draft: v })
      : deleteProjectCopilotState(currentUserId, copilotDraftDbKey())
    ).catch(() => {
      // best-effort
    });
  }, [currentUserId]);

  // Persist settings form (minus api_key) to localStorage.
  useEffect(() => {
    writeStoredSettingsForm(settingsForm);
  }, [settingsForm]);

  // Load this user's sent-input history once the user is known; reset any navigation cursor.
  useEffect(() => {
    inputHistoryRef.current = readStoredInputHistory(currentUserId);
    historyNavRef.current = null;
  }, [currentUserId, draftScope]);

  // Discover whether inline completion is enabled on the backend (one cheap GET per open).
  useEffect(() => {
    if (!isOpen) return;
    let cancelled = false;
    void getCopilotConfig()
      .then((config) => {
        if (!cancelled) setCompletionEnabled(config.completionEnabled);
      })
      .catch(() => {
        // Completion is optional; absence is indistinguishable from disabled.
      });
    return () => {
      cancelled = true;
    };
  }, [isOpen]);

  // Read via ref so an unrelated message-list change doesn't clear the ghost or re-fire the fetch.
  const completionConversationRef = useRef(sessionMessages);
  completionConversationRef.current = sessionMessages;

  // Keep the contextPayload ref current (parents pass an inline object, so this is frequent + cheap).
  useEffect(() => {
    contextPayloadRef.current = contextPayload;
  }, [contextPayload]);

  useEffect(() => {
    if (!isOpen || position) return;
    if (typeof window === 'undefined') return;
    const nextPosition = {
      x: Math.max(12, window.innerWidth - 560 - 24),
      y: Math.max(12, window.innerHeight - 680 - 24)
    };
    latestPositionRef.current = nextPosition;
    setPosition(nextPosition);
    writeStoredCopilotPanelState(currentUserId, nextPosition);
  }, [currentUserId, isOpen, position]);

  useEffect(() => {
    if (!isOpen) return;
    writeStoredCopilotPanelState(currentUserId, { historyOpen });
  }, [currentUserId, historyOpen, isOpen]);

  // ⌘K palette handoff: open the panel on whichever page the user is on.
  useEffect(() => {
    const onPaletteOpen = () => onOpen();
    window.addEventListener('vbio:open-copilot', onPaletteOpen);
    return () => window.removeEventListener('vbio:open-copilot', onPaletteOpen);
  }, [onOpen]);

  useEffect(() => {
    if (!isOpen || !panelRef.current || typeof ResizeObserver === 'undefined') return;
    const initialWidth = Math.round(panelRef.current.getBoundingClientRect().width);
    const initialHeight = Math.round(panelRef.current.getBoundingClientRect().height);
    sizeReadyRef.current = false;
    let frame = 0;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      if (frame) window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const panel = panelRef.current;
        if (!panel) return;
        const rect = panel.getBoundingClientRect();
        const width = Math.round(rect.width);
        const height = Math.round(rect.height);
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
  }, [currentUserId, isOpen]);

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    // On mobile (full-screen mode), dragging is disabled — the panel is pinned to the viewport.
    if (isMobileViewport) return;
    const target = event.target as HTMLElement;
    if (target.closest('button, textarea, input, select, a')) return;
    // Primary pointer only, so drag doesn't block touch clicks on header buttons.
    if (event.button !== 0 && event.pointerType === 'mouse') return;
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

  const sendMessage = async (overrideContent?: string) => {
    const isChipAnswer = typeof overrideContent === 'string';
    // Read at send time so typing never re-renders the modal.
    const draft = draftStore.get();
    const content = (isChipAnswer ? overrideContent! : draft).trim();
    // Re-entrancy guard: a send while sending cancels the in-flight turn instead of orphaning it.
    if (!content || applyingActionKey || bulkAction) return;
    // A user-typed turn supersedes any armed auto-continuation.
    if (!isChipAnswer) {
      continuationArmedRef.current = null;
      clearStoredCopilotContinuation(currentUserId);
    }
    if (sending) {
      // Steering: an interjection queues to the in-flight stream instead of cancelling it.
      // Chip answers still cancel; a failed steer POST falls back to a fresh turn.
      if (!isChipAnswer && activeTurnKeyRef.current) {
        const steered = content;
        draftStore.set('');
        setSteeredTurnTexts((prev) => [...prev, steered]);
        const queued = await submitCopilotSteering({ turnKey: activeTurnKeyRef.current, text: steered });
        if (queued) return;
        setSteeredTurnTexts((prev) => prev.filter((item) => item !== steered));
        draftStore.set(steered);
        // fall through: the turn ended between the check and the POST — send as a new turn.
      } else {
        cancelSending();
        return;
      }
    }
    const attachmentMetadata = uploadedAttachments.map((attachment) => ({
      id: attachment.id,
      name: attachment.name,
      type: attachment.type,
      size: attachment.size,
      mention: `@${attachment.name}`,
      mentioned: content.includes(`@${attachment.name}`)
    }));
    const conversationContext = buildCopilotConversationContext(sessionMessages);
    const copilotMemory = collectCopilotMemory(sessionMessages, activeSessionId);
    setSending(true);
    // Local controller: abortRef may be nulled during the await below; re-reading it would orphan the stream.
    const controller = new AbortController();
    abortRef.current = controller;
    abortReasonRef.current = 'user';
    setStreamStartedAt(new Date().toISOString());
    setError(null);
    setLiveTrace([]);
    // Only a typed send clears the draft; a chip answer must not wipe what the user is composing.
    if (!isChipAnswer) {
      draftStore.set('');
      void deleteProjectCopilotState(currentUserId, copilotDraftDbKey());
    }
    // Record the sent input for ↑/↓ recall and exit any history navigation.
    inputHistoryRef.current = appendInputHistory(inputHistoryRef.current, content);
    writeStoredInputHistory(currentUserId, inputHistoryRef.current);
    historyNavRef.current = null;
    try {
      if (pendingActions.length > 0) {
        await persistActionResolutions([...pendingActions].sort(comparePlanActions), 'cancelled');
      }
      if (controller.signal.aborted) return; // session switched mid-await — drop this turn
      // Dedupe: a chip turn aborted by navigation already inserted its user row; reuse it.
      const orphanedSynthetic = isChipAnswer
        ? sessionMessages[sessionMessages.length - 1]
        : undefined;
      const reusesOrphanedTurn =
        Boolean(orphanedSynthetic) &&
        orphanedSynthetic!.role === 'user' &&
        String(orphanedSynthetic!.content || '').trim() === content;
      let userMessage: ProjectCopilotMessage;
      if (reusesOrphanedTurn) {
        userMessage = orphanedSynthetic!;
      } else {
        userMessage = await insertProjectCopilotMessage({
          ...messageScope,
          userId: currentUserId,
          role: 'user',
          content,
          metadata: { ...sourceContext, session_id: activeSessionId, attachments: attachmentMetadata }
        });
        setMessages((prev) => [...prev, { ...userMessage, username: currentUsername }]);
      }

      const turn = await streamCopilotTurn(
        {
          contextType,
          contextPayload: {
            ...contextPayload,
            copilot_conversation: conversationContext,
            ...(copilotMemory.length > 0 ? { copilot_memory: copilotMemory } : {}),
            ...(attachmentMetadata.length > 0 ? { copilot_attachments: attachmentMetadata } : {})
          },
          userId: currentUserId,
          username: currentUsername,
          content
        },
        (step) => setLiveTrace((prev) => [...prev, step]),
        controller.signal
      );
      if (controller.signal.aborted) return; // switched mid-stream — do not touch the new session
      const planActions = turn.actions;
      let assistantMessage: ProjectCopilotMessage;
      try {
        assistantMessage = await insertProjectCopilotMessage({
          ...messageScope,
          userId: null,
          role: 'assistant',
          content: turn.content,
          metadata: {
            ...sourceContext,
            session_id: activeSessionId,
            owner_user_id: currentUserId,
            candidate_plan_actions: planActions,
            plan_id: turn.planId,
            planner_state: turn.state,
            planner_questions: turn.questions,
            planner_trace: turn.trace,
            planner_observations: turn.observations
          }
        });
      } catch {
        // Receipt-DB failure must not discard a streamed turn; fall back to a local-only row.
        const nowIso = new Date().toISOString();
        assistantMessage = {
          id: `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
          created_at: nowIso,
          updated_at: nowIso,
          context_type: contextType,
          project_id: null,
          project_task_id: null,
          user_id: null,
          role: 'assistant',
          content: turn.content,
          metadata: {
            session_id: activeSessionId,
            owner_user_id: currentUserId,
            candidate_plan_actions: planActions,
            plan_id: turn.planId,
            planner_state: turn.state,
            planner_questions: turn.questions,
            planner_trace: turn.trace,
            planner_observations: turn.observations
          }
        };
      }
      setMessages((prev) => [...prev, assistantMessage]);
      // Same context filter as the restore path; a no-op turn must not wipe actions
      // restored by a mid-stream session load.
      setPendingActions((prev) => {
        const next = planActions.filter((action) => actionMatchesContext(action, contextType));
        return next.length > 0 ? next : prev;
      });
      // Desktop-only: re-focus on mobile would re-open the dismissed keyboard.
      if (!isMobileViewport) focusComposer();
    } catch (err) {
      // Restore the draft only on user Stop; navigation aborts must not leak it into a new session.
      if (err instanceof DOMException && err.name === 'AbortError') {
        if (abortReasonRef.current === 'user' && !isChipAnswer) draftStore.set(content);
        if (!isMobileViewport) focusComposer();
        return;
      }
      setError(err instanceof Error ? err.message : 'Failed to send Copilot message.');
      if (!isChipAnswer) {
        draftStore.set(content);
      }
      if (!isMobileViewport) focusComposer();
    } finally {
      // Identity-guarded: a newer turn's controller must not be clobbered.
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
      if (!controller.signal.aborted || abortReasonRef.current === 'user') {
        activeTurnKeyRef.current = null;
        setSteeredTurnTexts([]);
        setSending(false);
        setLiveTrace([]);
        setStreamStartedAt('');
      }
    }
  };
  // Latest sendMessage via ref keeps the memoized answerQuestion callback stable.
  const sendMessageRef = useRef(sendMessage);
  sendMessageRef.current = sendMessage;
  const answerQuestion = useCallback((answer: string) => {
    void sendMessageRef.current(answer);
  }, []);

  // Lazy per-message trace hydration; only that item re-renders.
  const loadMessageTrace = useCallback((messageId: string) => {
    void fetchProjectCopilotMessageTrace(messageId)
      .catch(() => [] as unknown)
      .then((trace) => {
        setMessages((prev) =>
          prev.map((item) =>
            item.id === messageId
              ? { ...item, metadata: { ...(item.metadata || {}), planner_trace: trace ?? [] } }
              : item
          )
        );
      });
  }, []);

  // AUTO-CONTINUATION: confirming an action is the "tool result" the agent loop resumes
  // on. Armed with the applied action's own plan id, fired once per plan id under a hard
  // cap, guarded by the busy states above; sessionStorage carries the arm across page
  // navigation, and FAILED receipts arm too — with the recovery prompt.
  const continuationArmedRef = useRef<CopilotContinuation | null>(null);
  const continuedPlansRef = useRef<Set<string>>(new Set());
  // An in-flight continuation unmounts to the next mount instead of dying with the request.
  const continuationInFlightRef = useRef<CopilotContinuation | null>(null);
  // Plans with at least one failed receipt.
  const failedPlansRef = useRef<Set<string>>(new Set());
  // Nudge the effect while waiting for the receipt to land; storage writes don't notify React.
  const continuationPollTimerRef = useRef<number | null>(null);
  const [continuationNudge, setContinuationNudge] = useState(0);
  useEffect(() => {
    if (!isOpen || bulkAction || pendingActions.length > 0 || applyingActionKey || sending) return;
    const armed =
      continuationArmedRef.current ??
      (() => {
        const stored = readStoredCopilotContinuation(currentUserId);
        return stored && stored.sessionId === activeSessionId ? stored : null;
      })();
    if (!armed) return;
    continuationArmedRef.current = null;
    // Session and TTL checks apply to both arms; a stale arm must not resurrect a dead plan.
    if (armed.sessionId !== activeSessionId || Date.now() - armed.at > COPILOT_CONTINUATION_TTL_MS) {
      clearStoredCopilotContinuation(currentUserId);
      return;
    }
    // Wait until the arming receipt is visible before firing; the TTL bounds the wait.
    const receiptVisible = sessionMessages.some((message) =>
      readActionResolutions(message).some((row) => row.plan_id === armed.planId && row.operation_id === armed.operationId)
    );
    if (!receiptVisible) {
      // Re-arm in memory so a later effect run picks this up.
      continuationArmedRef.current = armed;
      // The load may already be done; nothing re-runs this effect on its own — poll briefly.
      if (continuationPollTimerRef.current === null) {
        continuationPollTimerRef.current = window.setTimeout(() => {
          continuationPollTimerRef.current = null;
          setContinuationNudge((n) => n + 1);
        }, 1500);
      }
      return;
    }
    if (continuationPollTimerRef.current !== null) {
      window.clearTimeout(continuationPollTimerRef.current);
      continuationPollTimerRef.current = null;
    }
    clearStoredCopilotContinuation(currentUserId);
    if (continuedPlansRef.current.has(armed.planId)) return;
    if (continuedPlansRef.current.size >= AUTO_CONTINUATION_CAP) return;
    continuedPlansRef.current.add(armed.planId);
    continuationInFlightRef.current = armed;
    void Promise.resolve(
      sendMessageRef.current(
        armed.outcome === 'failed' ? COPILOT_CONTINUATION_MESSAGE_FAILED : COPILOT_CONTINUATION_MESSAGE_APPLIED
      )
    ).finally(() => {
      if (continuationInFlightRef.current?.planId === armed.planId) {
        continuationInFlightRef.current = null;
      }
    });
  }, [isOpen, activeSessionId, bulkAction, pendingActions.length, applyingActionKey, sending, currentUserId, sessionMessages, continuationNudge]);

  const cancelSending = useCallback(() => {
    abortReasonRef.current = 'user';
    abortRef.current?.abort();
  }, []);

  // Hand an in-flight continuation to the next mount; refresh the timestamp for the TTL.
  const rearmInFlightContinuation = () => {
    const inFlight = continuationInFlightRef.current;
    if (!inFlight) return;
    continuationInFlightRef.current = null;
    writeStoredCopilotContinuation(currentUserId, { ...inFlight, at: Date.now() });
  };

  // Abort the in-flight turn on close so it can't resolve into a stale session.
  // Closing isn't cancelling the plan: an aborted continuation is re-armed.
  useEffect(() => {
    if (isOpen) return;
    abortReasonRef.current = 'close';
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    setLiveTrace([]);
    setStreamStartedAt('');
    rearmInFlightContinuation();
  }, [isOpen]);

  // Re-clamp on viewport shrink so a persisted position never strands the panel.
  useEffect(() => {
    if (!isOpen || isMobileViewport) return;
    const onResize = () => {
      setPosition((prev) => {
        if (!prev) return prev;
        const clamped = clampPanelPosition(prev);
        return clamped.x === prev.x && clamped.y === prev.y ? prev : clamped;
      });
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [isOpen, isMobileViewport]);

  // Escape closes the panel — the last-resort exit when the close button is hard to reach.
  useEffect(() => {
    if (!isOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT' || target.isContentEditable)) {
        return; // let composers handle Escape first
      }
      onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isOpen, onClose]);

  // Cancel the fetch on unmount; an aborted continuation is re-armed for the next page.
  useEffect(() => () => {
    abortReasonRef.current = 'unmount';
    abortRef.current?.abort();
    if (continuationPollTimerRef.current !== null) {
      window.clearTimeout(continuationPollTimerRef.current);
      continuationPollTimerRef.current = null;
    }
    rearmInFlightContinuation();
  }, [currentUserId]);

  useEffect(() => {
    if (!plusMenuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (plusMenuRef.current && !plusMenuRef.current.contains(event.target as Node)) {
        setPlusMenuOpen(false);
      }
    };
    window.addEventListener('mousedown', onPointerDown);
    return () => window.removeEventListener('mousedown', onPointerDown);
  }, [plusMenuOpen]);

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
        textareaRef.current?.focus({ preventScroll: true });
      });
    }
  }, []);

  const removeUploadedAttachment = useCallback((attachmentId: string) => {
    setUploadedAttachments((prev) => prev.filter((item) => item.id !== attachmentId));
  }, []);

  const startNewChat = () => {
      continuationArmedRef.current = null;
      continuedPlansRef.current = new Set();
      failedPlansRef.current = new Set();
      clearStoredCopilotContinuation(currentUserId);
    // Abort the in-flight turn before swapping sessions so it can't land in the new one.
    abortReasonRef.current = 'session-switch';
    abortRef.current?.abort();
    abortRef.current = null;
    sessionSwitchRef.current = true;
    const nextSessionId = createSessionId();
    activateSession(nextSessionId);
    setSending(false);
    draftStore.set('');
    void deleteProjectCopilotState(currentUserId, copilotDraftDbKey());
    setPendingActions([]);
    setError(null);
    setLiveTrace([]);
    setStreamStartedAt('');
    setHistoryOpen(false);
    historyNavRef.current = null;
    focusComposer();
  };

  const selectSession = (sessionId: string) => {
      continuationArmedRef.current = null;
      continuedPlansRef.current = new Set();
      failedPlansRef.current = new Set();
      clearStoredCopilotContinuation(currentUserId);
    // Same stream-abort as startNewChat.
    abortReasonRef.current = 'session-switch';
    abortRef.current?.abort();
    abortRef.current = null;
    sessionSwitchRef.current = true;
    activateSession(sessionId);
    setSending(false);
    setLiveTrace([]);
    setStreamStartedAt('');
    draftStore.set('');
    void deleteProjectCopilotState(currentUserId, copilotDraftDbKey());
    setError(null);
    restoreSessionActions(messages, sessionId);
    historyNavRef.current = null;
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

  // Copilot runtime settings (proxy / LLM server / API key)

  // Swallow token errors and return null so handlers treat "no usable token" uniformly.
  const getSettingsToken = useCallback(async (): Promise<string | null> => {
    try {
      return await ensureManagementSession();
    } catch {
      return null;
    }
  }, [ensureManagementSession]);

  const loadCopilotSettings = useCallback(async () => {
    const token = await getSettingsToken();
    if (!token) {
      setSettingsError('Administrator sign-in required to view settings.');
      return;
    }
    try {
      const view = await getCopilotSettings(token);
      setSettingsForm({ proxy: view.proxy, api_url: view.api_url, api_key: '', model: view.model });
      setSettingsMaskedKey(view.api_key_masked);
      setSettingsHasKey(view.has_api_key);
      setSettingsError('');
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : 'Failed to load settings.');
    }
  }, [getSettingsToken]);

  const openSettings = useCallback(() => {
    setSettingsOpen(true);
    setSettingsError('');
    setSettingsTestResult(null);
    setSettingsSaved(false);
    void loadCopilotSettings();
  }, [loadCopilotSettings]);

  const handleSaveSettings = useCallback(async () => {
    const token = await getSettingsToken();
    if (!token) {
      setSettingsError('Administrator sign-in required to save settings.');
      return;
    }
    setSettingsSaving(true);
    setSettingsError('');
    setSettingsSaved(false);
    try {
      const view = await saveCopilotSettings(token, {
        proxy: settingsForm.proxy,
        api_url: settingsForm.api_url,
        api_key: settingsForm.api_key,
        model: settingsForm.model,
      });
      setSettingsMaskedKey(view.api_key_masked);
      setSettingsHasKey(view.has_api_key);
      setSettingsForm((prev) => ({ ...prev, api_key: '' }));
      setSettingsSaved(true);
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : 'Failed to save settings.');
    } finally {
      setSettingsSaving(false);
    }
  }, [getSettingsToken, settingsForm]);

  const handleTestSettings = useCallback(async () => {
    const token = await getSettingsToken();
    if (!token) {
      setSettingsError('Administrator sign-in required to test settings.');
      return;
    }
    setSettingsTesting(true);
    setSettingsError('');
    setSettingsTestResult(null);
    try {
      const result = await testCopilotSettings(token, {
        proxy: settingsForm.proxy,
        api_url: settingsForm.api_url,
        api_key: settingsForm.api_key,
        model: settingsForm.model,
      });
      setSettingsTestResult(result);
    } catch (err) {
      setSettingsError(err instanceof Error ? err.message : 'Settings test failed.');
    } finally {
      setSettingsTesting(false);
    }
  }, [getSettingsToken, settingsForm]);

  async function persistActionResolutions(
    actions: CopilotPlanAction[],
    status: CopilotActionResolutionStatus,
    detailMessage?: string | null,
    errorMessage?: string | null
  ): Promise<void> {
    const resolutions = actions.map((action) => {
      const planId = String(action.plan_id || '').trim();
      const operationId = String(action.operation_id || '').trim();
      if (!planId || !operationId) {
        throw new Error('Copilot confirmation operation is missing its plan identity.');
      }
      return {
        plan_id: planId,
        operation_id: operationId,
        skill: String(action.id || '').trim(),
        label: String(action.label || '').trim(),
        status,
        ...(detailMessage ? { detail: detailMessage } : {}),
        ...(errorMessage ? { error: errorMessage } : {}),
        // Arguments ride along so later recovery/summary turns can cite what was applied.
        arguments:
          action.arguments && typeof action.arguments === 'object' && !Array.isArray(action.arguments)
            ? (action.arguments as Record<string, unknown>)
            : {},
      };
    });
    const actionLabels = actions.map((a) => a.label).filter(Boolean);
    const defaultContent = status === 'applied'
      ? (actionLabels.length > 0 ? `${actionLabels.join(', ')} applied.` : `Applied ${actions.length} confirmed operation${actions.length === 1 ? '' : 's'}.`)
      : status === 'failed'
        ? (actionLabels.length > 0
            ? `Failed to apply "${actionLabels.join('", "')}": ${errorMessage || 'Unknown error'}`
            : `Failed: ${errorMessage || 'Unknown error'}`)
        : `Cancelled ${actions.length} pending operation${actions.length === 1 ? '' : 's'}.`;
    const receipt = await insertProjectCopilotMessage({
      ...messageScope,
      userId: currentUserId,
      role: 'system',
      content: detailMessage || defaultContent,
      metadata: {
        ...sourceContext,
        session_id: activeSessionId,
        owner_user_id: currentUserId,
        action_resolutions: resolutions
      }
    });
    // Record the plan as failed so arming speaks the failed outcome even when the last
    // action applies — but only after the receipt persisted, so a throwing insert
    // can't poison the retry.
    if (status === 'failed') {
      for (const resolution of resolutions) {
        if (resolution.plan_id) failedPlansRef.current.add(resolution.plan_id);
      }
    }
    const resolvedKeys = new Set(actions.map(planActionKey));
    setMessages((prev) => [...prev, receipt]);
    setPendingActions((prev) => prev.filter((item) => !resolvedKeys.has(planActionKey(item))));
  }

  const executeAction = async (action: CopilotPlanAction): Promise<string | null> => {
    if (action.id === 'task_detail:apply_copilot_attachments') {
      if (!sendAttachmentsAction) throw new Error('This page cannot apply Copilot file attachments.');
      const rawApplications = action.payload?.attachmentApplications;
      if (!Array.isArray(rawApplications) || rawApplications.length === 0) {
        throw new Error('Copilot attachment operation does not satisfy its declared contract.');
      }
      const attachmentsById = new Map(uploadedAttachments.map((attachment) => [attachment.id, attachment]));
      const applications = rawApplications.map((item) => {
        if (!item || typeof item !== 'object') {
          throw new Error('Copilot attachment operation does not satisfy its declared contract.');
        }
        const row = item as Record<string, unknown>;
        const attachmentId = String(row.attachmentId || '').trim();
        const fileName = String(row.fileName || '').trim();
        const role = String(row.role || '').trim();
        if (!attachmentId || !fileName || (role !== 'target' && role !== 'ligand' && role !== 'template')) {
          throw new Error('Copilot attachment operation does not satisfy its declared contract.');
        }
        return { attachmentId, fileName, role } as CopilotAttachmentApplication;
      });
      const selectedAttachments = applications.map((application) => {
        const attachment = attachmentsById.get(application.attachmentId);
        if (!attachment || attachment.name !== application.fileName) {
          throw new Error('A referenced Copilot attachment is no longer available.');
        }
        return attachment;
      });
      await sendAttachmentsAction(selectedAttachments, '', applications);
      return null;
    }
    if (!applyPlanAction) throw new Error('This Copilot action cannot be applied on the current page.');
    const result = await applyPlanAction(action);
    return typeof result === 'string' ? result : null;
  };

  const applyAction = async (action: CopilotPlanAction): Promise<boolean> => {
    // Entry mutex: no concurrent apply, bulk cancel, or streaming turn.
    if (applyingActionKey || bulkAction || sending) return false;
    const actionKey = planActionKey(action);
    setApplyingActionKey(actionKey);
    setError(null);
    const armedPlanId = String(action.plan_id || '').trim() || null;
    const armedOperationId = String(action.operation_id || '').trim();
    const isTerminalEffect = action.effect === 'execute' || action.payload?.destructive === true;
    // Arm the auto-continuation when this was the plan's LAST action (see the continuation
    // effect). A cleanly applied terminal effect (execute/destructive) ends the goal; any
    // failure arms the loop with the failed outcome.
    // Uses the closure's pendingActions — a navigating apply unmounts this page mid-await,
    // and React drops an unmounted component's state updater — and arms BEFORE awaiting
    // the receipt insert, so the sessionStorage handoff is on disk before the next page
    // mounts. The consuming effect still waits for the receipt to become visible.
    const armPlanContinuation = (appliedCleanly: boolean) => {
      if (!armedPlanId || !armedOperationId) return;
      const remaining = pendingActions.filter((item) => planActionKey(item) !== actionKey);
      if (remaining.length > 0) return;
      const planFailed = !appliedCleanly || failedPlansRef.current.has(armedPlanId);
      if (!planFailed && isTerminalEffect) return;
      const armed: CopilotContinuation = {
        planId: armedPlanId,
        operationId: armedOperationId,
        sessionId: activeSessionId,
        outcome: planFailed ? 'failed' : 'applied',
        at: Date.now()
      };
      continuationArmedRef.current = armed;
      // sessionStorage carries the handoff if this apply navigates away.
      writeStoredCopilotContinuation(currentUserId, armed);
    };
    try {
      const detailMessage = await executeAction(action);
      armPlanContinuation(true);
      try {
        await persistActionResolutions([action], 'applied', detailMessage);
      } catch (persistErr) {
        // The host change already happened; keep the action pending and surface the persist error.
        setError(persistErr instanceof Error ? persistErr.message : 'Applied, but saving the receipt failed. The operation stays listed — you can retry to record it.');
        return false;
      }
      return true;
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to apply Copilot action.';
      armPlanContinuation(false);
      // The failure surfaces in the receipt; the banner only appears if persisting it fails.
      try {
        await persistActionResolutions([action], 'failed', null, errorMsg);
      } catch {
        setError(errorMsg);
      }
      return false;
    } finally {
      setApplyingActionKey(null);
    }
  };

  const cancelPendingActions = async () => {
    if (pendingActions.length === 0 || applyingActionKey || bulkAction) return;
    setBulkAction('cancel');
    setError(null);
    // The user killed the plan; don't let an armed continuation revive it.
    continuationArmedRef.current = null;
    clearStoredCopilotContinuation(currentUserId);
    try {
      await persistActionResolutions([...pendingActions].sort(comparePlanActions), 'cancelled');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to cancel Copilot operations.');
    } finally {
      setBulkAction(null);
    }
  };

  if (!isOpen) {
    return (
      <button className="copilot-launcher" type="button" onClick={onOpen} aria-label="Open Copilot" title="Open Copilot">
        <Bot size={20} />
      </button>
    );
  }

  return (
    <CopilotPanelShell
      panel={{
        panelRef,
        isSuppressedByOverlay: suppressedByOverlay,
        isMobileViewport,
        visualViewportHeight,
        visualViewportTop,
        position,
        panelSize,
        onDragStart: startDrag,
        onDragMove: moveDrag,
        onDragEnd: endDrag
      }}
      header={{
        title,
        subtitle,
        authSession,
        onClose,
        onOpenSettings: openSettings
      }}
      history={{
        isHistoryOpen: historyOpen,
        setHistoryOpen,
        chatSessions,
        activeSessionId,
        onSelectSession: selectSession,
        deleteSessionAction: deleteSession,
        onStartNewChat: startNewChat
      }}
      settings={{
        isSettingsOpen: settingsOpen,
        setSettingsOpen,
        settingsForm,
        setSettingsForm,
        settingsError,
        settingsHasKey,
        settingsMaskedKey,
        isSettingsSaved: settingsSaved,
        isSettingsSaving: settingsSaving,
        settingsTestResult,
        isSettingsTesting: settingsTesting,
        saveSettingsAction: handleSaveSettings,
        testSettingsAction: handleTestSettings
      }}
      messages={{
        scrollRef,
        onMessagesScroll: handleMessagesScroll,
        isLoading: loading,
        sessionMessages,
        visibleMessageCount,
        setVisibleMessageCount,
        MESSAGE_WINDOW,
        visibleSessionMessages,
        onAnswerQuestion: answerQuestion,
        onLoadTrace: loadMessageTrace,
        streamStartedAt,
        steeredTurnTexts,
        liveTrace
      }}
      turn={{
        isSending: sending,
        applyingActionKey,
        bulkAction
      }}
      plan={{
        pendingActions,
        applyAction,
        cancelAction: cancelPendingActions
      }}
      error={error}
    >
      {<CopilotComposer
          draft={{
            store: draftStore,
            textareaRef,
            onDraftPersist: persistCopilotDraft,
            historyNavRef,
            inputHistoryRef
          }}
          completion={{
            completionEnabled,
            contextType,
            currentUserId,
            currentUsername,
            contextPayloadRef,
            conversationRef: completionConversationRef
          }}
          attachments={{
            uploadedAttachments,
            onRemoveAttachment: removeUploadedAttachment,
            onAddFiles: addUploadedFiles,
            plusMenuRef,
            isPlusMenuOpen: plusMenuOpen,
            setPlusMenuOpen,
            fileInputRef
          }}
          turn={{
            isSending: sending,
            applyingActionKey,
            bulkAction,
            onCancelSending: cancelSending,
            sendMessageAction: sendMessage
          }}
        />}
    </CopilotPanelShell>
  );
}
