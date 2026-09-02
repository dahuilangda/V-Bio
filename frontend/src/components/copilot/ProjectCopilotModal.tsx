import { Bot, Check, ChevronRight, LoaderCircle, MessageSquarePlus, MessageSquareText, PanelLeft, Plus, Send, Settings, Sparkles, Square, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState, memo, type PointerEvent as ReactPointerEvent } from 'react';
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
import { getCopilotConfig, getCopilotSettings, parseCopilotQuestions, requestCopilotCompletions, saveCopilotSettings, streamCopilotTurn, submitCopilotSteering, testCopilotSettings } from '../../api/copilotApi';
import type { CopilotTestResult, CopilotTestSubResult } from '../../api/copilotApi';
import type { CopilotContextType, CopilotPlanAction, CopilotPlannerQuestion, CopilotTraceStep, ProjectCopilotMessage } from '../../types/models';
import { formatDateTime } from '../../utils/date';
import { useAuth } from '../../hooks/useAuth';
import { useOverlayPresence } from '../ui/OverlayContext';
import { collectCopilotMemory, formatTraceStep, readPlannerTrace, readSessionId } from './copilotTraceUi';
import {
  appendInputHistory,
  nextInputHistoryNav,
  readStoredInputHistory,
  shouldNavigateHistory,
  writeStoredInputHistory,
  type InputHistoryNav
} from './copilotInputHistory';
import './ProjectCopilotModal.css';
import { fuzzyRank } from '../../utils/fuzzyScore';

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
  onApplyPlanAction?: (action: CopilotPlanAction) => void | Promise<void | string>;
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

// Planner trace + memory helpers live in ./copilotTraceUi (pure + unit-tested).

// Reasoning steps — plain muted text, one short phrase per step (wording in formatTraceStep).
// The latest streaming step brightens; everything else stays quiet so the panel reads as part of
// the message instead of a debug log.
function TraceStepList({ steps, highlightLast }: { steps: CopilotTraceStep[]; highlightLast?: boolean }) {
  return (
    <ol className="copilot-trace-list">
      {steps.map((step, index) => {
        const isLast = highlightLast && index === steps.length - 1;
        return (
          <li
            key={`${step.round}-${step.event}-${index}`}
            className={`copilot-trace-item${isLast ? ' is-current' : ''}`}
          >
            {formatTraceStep(step)}
          </li>
        );
      })}
    </ol>
  );
}

// Collapsible "思考过程 / 思考中" disclosure — a quiet inline section of the message: a small
// animated sparkle toggle while live, muted step text below, smooth expand/collapse.
function CopilotThinkingCard({ steps, live }: { steps: CopilotTraceStep[]; live?: boolean }) {
  const [open, setOpen] = useState(live ?? true);
  // Live with no steps yet: a bare "Thinking…" indicator. No card chrome, no divider, no empty
  // expandable body — those would float above nothing and read as a stray line / empty box.
  if (live && steps.length === 0) {
    return (
      <span className="copilot-thinking-inline">
        <Sparkles className="copilot-thinking-spark" size={13} aria-hidden="true" />
        <span className="copilot-thinking-title">Thinking…</span>
      </span>
    );
  }
  const label = live ? 'Thinking' : 'Reasoning';
  return (
    <div className={`copilot-thinking-card${live ? ' is-live' : ''}${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="copilot-thinking-head"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
      >
        <Sparkles className="copilot-thinking-spark" size={13} aria-hidden="true" />
        <span className="copilot-thinking-title">{label}</span>
        <span className="copilot-thinking-meta">{steps.length} {steps.length === 1 ? 'step' : 'steps'}</span>
        <ChevronRight className="copilot-thinking-chev" size={12} aria-hidden="true" />
      </button>
      <div className="copilot-thinking-body">
        <div className="copilot-thinking-body-inner">
          <TraceStepList steps={steps} highlightLast={live} />
        </div>
      </div>
    </div>
  );
}

function readPlannerQuestions(value: unknown): CopilotPlannerQuestion[] {
  return parseCopilotQuestions(value);
}

interface ObservationRecord {
  source: string;
  fields: { key: string; value: string }[];
}

// Flatten the planner_observations metadata into displayable records. Each observation may contain
// multiple records (search results) or a single record (resolve). Only user-facing scalar fields
// are kept; long values (SMILES, sequences) are preserved in full so the user can copy them.
function readObservationRecords(value: unknown): ObservationRecord[] {
  if (!Array.isArray(value)) return [];
  const records: ObservationRecord[] = [];
  const META_KEYS = new Set(['source', 'query', 'count', 'ok', 'error', 'metadata', 'index']);
  for (const item of value) {
    if (!item || typeof item !== 'object') continue;
    const obs = item as Record<string, unknown>;
    // Observations may carry records in a 'results' array or be the record itself.
    const candidates: Record<string, unknown>[] = Array.isArray(obs.results)
      ? obs.results.filter((r): r is Record<string, unknown> => !!r && typeof r === 'object')
      : [obs];
    for (const rec of candidates.slice(0, 3)) {
      const fields: { key: string; value: string }[] = [];
      for (const [key, val] of Object.entries(rec)) {
        if (!key || META_KEYS.has(key) || key.startsWith('_') || key.endsWith('Url') || key.endsWith('url')) continue;
        if (val === null || val === undefined || typeof val === 'object') continue;
        const text = String(val).trim();
        if (!text) continue;
        fields.push({ key, value: text });
      }
      if (fields.length > 0) {
        records.push({ source: String(obs.source || ''), fields });
      }
    }
  }
  return records;
}

// Renders retrieved records in a collapsible section under the assistant message. The user always
// sees the authoritative data (sequence, SMILES, accession, ...) even when the model's message
// only summarizes it. Long values are shown in a scrollable <pre> so they don't break the layout.
function CopilotObservationCard({ records }: { records: ObservationRecord[] }) {
  // Auto-expand when any record contains a long field (sequence, SMILES) — the model's message
  // often says "Here is the sequence:" but truncates the actual value due to token limits. The user
  // needs to see the authoritative data without having to know to click "Retrieved data".
  const hasLongField = records.some((rec) => rec.fields.some((f) => f.value.length > 60));
  const [expanded, setExpanded] = useState(hasLongField);
  return (
    <div className="copilot-observation-card">
      <button
        type="button"
        className="copilot-observation-toggle"
        onClick={() => setExpanded((prev) => !prev)}
      >
        {expanded ? '▾' : '▸'} Retrieved data ({records.length} record{records.length === 1 ? '' : 's'})
      </button>
      {expanded ? (
        <div className="copilot-observation-records">
          {records.map((rec, i) => (
            <dl className="copilot-observation-record" key={`obs-${i}`}>
              {rec.fields.map((f) => (
                <div className="copilot-observation-field" key={f.key}>
                  <dt>{f.key}</dt>
                  <dd className={f.value.length > 80 ? 'is-long' : ''}>{f.value}</dd>
                </div>
              ))}
            </dl>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// Renders the planner's structured questions as clickable chips so the user resolves an ambiguity
// (task type, modeling backend, ...) with one click instead of typing. A choice question lists its
// options as chips plus an "Other ___" free-text answer (unless the planner set allowOther=false);
// confirm is yes/no; freeform just highlights the prompt above the composer.
function CopilotQuestionCard({
  questions,
  disabled,
  onAnswer
}: {
  questions: CopilotPlannerQuestion[];
  disabled: boolean;
  onAnswer: (answer: string) => void;
}) {
  // For a single question, answer immediately on chip click (no local state needed). For multiple
  // questions, accumulate answers locally so the user can fill them all in before submitting — this
  // avoids answering one question disabling the rest mid-stream.
  const isSingle = questions.length === 1;
  const [answers, setAnswers] = useState<Record<number, string>>({});
  // "Other ___" free-text state per choice question: which question has its input open, and the
  // draft text. The user's answer may fall outside the planner's options — the free-text escape
  // guarantees a choice question can always be answered, and the planner treats the reply as the
  // user's own resolution.
  const [otherOpen, setOtherOpen] = useState<Record<number, boolean>>({});
  const [otherText, setOtherText] = useState<Record<number, string>>({});
  const recordAnswer = (index: number, text: string) => {
    if (isSingle) {
      onAnswer(text);
      return;
    }
    setAnswers((prev) => ({ ...prev, [index]: text }));
  };
  const submitOther = (index: number, questionText: string) => {
    const text = String(otherText[index] || '').trim();
    if (!text) return;
    setOtherOpen((prev) => ({ ...prev, [index]: false }));
    setOtherText((prev) => ({ ...prev, [index]: '' }));
    recordAnswer(index, `${questionText} ${text}`);
  };
  const allAnswered = isSingle || questions.every((_, i) => answers[i]);
  const submit = () => {
    const lines = questions.map((_, i) => answers[i]).filter(Boolean);
    if (lines.length === 0) return;
    onAnswer(lines.join('\n'));
  };
  return (
    <div className="copilot-question-stack" aria-label="Copilot questions">
      {questions.map((question, questionIndex) => {
        const answeredValue = answers[questionIndex];
        const isAnswered = Boolean(answeredValue);
        const showOther = question.kind === 'choice' && question.allowOther !== false;
        return (
          <div className={`copilot-question${isAnswered ? ' is-answered' : ''}`} key={`q-${questionIndex}`}>
            <p className="copilot-question-text">{question.text}</p>
            {question.kind === 'choice' && Array.isArray(question.options) && question.options.length > 0 ? (
              <div className="copilot-question-options">
                {question.options.map((option, optionIndex) => {
                  const selected = answeredValue === `${question.text} ${option.value}`;
                  return (
                    <button
                      type="button"
                      className={`copilot-question-chip${selected ? ' is-selected' : ''}`}
                      key={`q-${questionIndex}-o-${optionIndex}`}
                      disabled={disabled}
                      onClick={() => recordAnswer(questionIndex, `${question.text} ${option.value}`)}
                      title={option.hint || option.label}
                    >
                      {option.label}
                    </button>
                  );
                })}
                {showOther ? (
                  <button
                    type="button"
                    className={`copilot-question-chip copilot-question-other-chip${otherOpen[questionIndex] ? ' is-open' : ''}`}
                    key={`q-${questionIndex}-other`}
                    disabled={disabled}
                    onClick={() => setOtherOpen((prev) => ({ ...prev, [questionIndex]: !prev[questionIndex] }))}
                  >
                    其他…
                  </button>
                ) : null}
              </div>
            ) : null}
            {showOther && otherOpen[questionIndex] ? (
              <div className="copilot-question-other">
                <input
                  className="copilot-question-other-input"
                  type="text"
                  value={otherText[questionIndex] || ''}
                  disabled={disabled}
                  placeholder="输入你的回答…"
                  onChange={(e) => setOtherText((prev) => ({ ...prev, [questionIndex]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      submitOther(questionIndex, question.text);
                    }
                  }}
                />
                <button
                  type="button"
                  className="copilot-question-other-submit"
                  disabled={disabled || !String(otherText[questionIndex] || '').trim()}
                  onClick={() => submitOther(questionIndex, question.text)}
                >
                  提交
                </button>
              </div>
            ) : null}
            {question.kind === 'confirm' ? (
              <div className="copilot-question-options">
                {[
                  { label: 'Yes', value: 'yes' },
                  { label: 'No', value: 'no' },
                ].map((opt) => {
                  const selected = answeredValue === `${question.text} ${opt.value}`;
                  return (
                    <button
                      type="button"
                      className={`copilot-question-chip${selected ? ' is-selected' : ''}`}
                      key={`q-${questionIndex}-${opt.value}`}
                      disabled={disabled}
                      onClick={() => recordAnswer(questionIndex, `${question.text} ${opt.value}`)}
                    >
                      {opt.label}
                    </button>
                  );
                })}
              </div>
            ) : null}
            {question.kind === 'freeform' ? (
              <p className="copilot-question-hint">Type your answer below.</p>
            ) : null}
            {isAnswered ? <small className="copilot-question-answered">✓ 已选择</small> : null}
          </div>
        );
      })}
      {!isSingle ? (
        <button
          type="button"
          className="copilot-question-submit"
          disabled={disabled || !allAnswered}
          onClick={submit}
        >
          {allAnswered ? '提交回答' : `还需回答 ${questions.length - Object.keys(answers).length} 个问题`}
        </button>
      ) : null}
    </div>
  );
}

// Message rendering runs ReactMarkdown (expensive). Memoize so a message only re-renders when its
// own content changes — not on every unrelated Copilot state update (typing, dragging, resize,
// caret moves), which otherwise re-parsed markdown for every message and froze the panel.
const CopilotMessageItem = memo(function CopilotMessageItem({
  message,
  disabled,
  onAnswerQuestion
}: {
  message: ProjectCopilotMessage;
  disabled: boolean;
  onAnswerQuestion: (answer: string) => void;
}) {
  const trace = message.role === 'assistant' ? readPlannerTrace(message.metadata?.planner_trace) : [];
  const questions = message.role === 'assistant' ? readPlannerQuestions(message.metadata?.planner_questions) : [];
  const plannerState = String(message.metadata?.planner_state || '').trim();
  const showQuestions = plannerState === 'needs_input' && questions.length > 0;
  // Retrieved records from read skills — shown in a collapsible section so the user always sees the
  // authoritative data even when the model's message only summarizes it (e.g. "Here is the sequence:"
  // without pasting 395 chars, which models routinely truncate in structured output).
  const observations = message.role === 'assistant' ? readObservationRecords(message.metadata?.planner_observations) : [];
  const showObservations = observations.length > 0;
  // Detect failed action receipts so they render with an error style.
  const hasFailedAction = message.role === 'system' && readActionResolutions(message).some((r) => r.status === 'failed');
  return (
    <article className={`copilot-message is-${message.role}${hasFailedAction ? ' is-action-failed' : ''}`}>
      <div className="copilot-message-meta">
        <strong>{author(message)}</strong>
        <span>{formatDateTime(message.created_at)}</span>
      </div>
      <div className="copilot-message-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
          {message.content}
        </ReactMarkdown>
      </div>
      {showObservations ? <CopilotObservationCard records={observations} /> : null}
      {showQuestions ? (
        <CopilotQuestionCard questions={questions} disabled={disabled} onAnswer={onAnswerQuestion} />
      ) : null}
      {trace.length > 0 && <CopilotThinkingCard steps={trace} />}
    </article>
  );
});

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

type CopilotActionResolutionStatus = 'applied' | 'cancelled' | 'failed';

interface CopilotActionResolution {
  plan_id: string;
  operation_id: string;
  status: CopilotActionResolutionStatus;
  /** The action skill id (e.g. the page operation that was confirmed). */
  skill?: string;
  /** The human-facing label shown on the confirmation chip. */
  label?: string;
  detail?: string;
  error?: string;
  /** The action's own arguments — what a recovery/summary turn may cite as actually applied. */
  arguments?: Record<string, unknown>;
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

function readActionResolutions(message: ProjectCopilotMessage): CopilotActionResolution[] {
  const value = message.metadata?.action_resolutions;
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const resolution = item as Partial<CopilotActionResolution>;
    const planId = String(resolution.plan_id || '').trim();
    const operationId = String(resolution.operation_id || '').trim();
    const status = resolution.status;
    if (!planId || !operationId || (status !== 'applied' && status !== 'cancelled' && status !== 'failed')) return [];
    return [{
      plan_id: planId,
      operation_id: operationId,
      status,
      ...(typeof resolution.skill === 'string' && resolution.skill ? { skill: resolution.skill } : {}),
      ...(typeof resolution.label === 'string' && resolution.label ? { label: resolution.label } : {}),
      ...(typeof resolution.detail === 'string' && resolution.detail ? { detail: resolution.detail } : {}),
      ...(typeof resolution.error === 'string' && resolution.error ? { error: resolution.error } : {}),
      ...(resolution.arguments && typeof resolution.arguments === 'object' && !Array.isArray(resolution.arguments)
        ? { arguments: resolution.arguments as Record<string, unknown> }
        : {}),
    }];
  });
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

// --- Settings form persistence (proxy / LLM URL / model — NOT api_key) ---

const COPILOT_SETTINGS_FORM_KEY = 'vbio:copilot-settings-form:v1';

interface SettingsFormValues {
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

// Map a connectivity sub-test to its display state: skipped fields are neutral,
// never a red failure — an unconfigured field simply isn't tested.
function settingsTestState(result: CopilotTestSubResult): 'ok' | 'fail' | 'skipped' {
  if (result.skipped) return 'skipped';
  return result.ok ? 'ok' : 'fail';
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

// --- Auto-continuation handoff across page navigation ---
// The modal is mounted per host page; an apply that navigates (tasks:create_docking and
// friends) unmounts this component before the continuation effect can fire. The armed
// continuation is therefore ALSO written to sessionStorage: the next page's modal picks it
// up on mount and resumes the loop there (the agent loop must survive its own actions'
// navigation). sessionStorage (not localStorage) so it dies with the tab, never leaks
// across sessions, and the TTL bounds it further.

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
// Hard cap on auto-continuations per mounted panel (per page / session switch) — a runaway
// planner loop must eventually hand control back to the user.
const AUTO_CONTINUATION_CAP = 5;

// Outcome-aware continuation prompts — the synthetic user turn that resumes the agent loop
// after a plan's actions resolved. Applied and failed receipts carry very different duties:
// continue vs. diagnose-and-recover, and NEVER claim completion on failed receipts.
const COPILOT_CONTINUATION_MESSAGE_APPLIED =
  '已应用刚才确认的操作（见回执），无需重复；请继续完成计划。若目标已达成，请基于回执如实总结后停止。';
const COPILOT_CONTINUATION_MESSAGE_FAILED =
  '刚才确认的部分操作执行失败（见回执中的 error 字段）。请先诊断失败原因，修复前提或修正参数后重新提议对应操作；无法修复时改用合法的替代路径；确实无路可走才向用户如实说明阻塞点。不要原样重复已失败的操作，也不要在回执为 failed 时宣称任务已完成。';

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

function clampPanelPosition(pos: { x: number; y: number }): { x: number; y: number } {
  // Keep the panel reachable on ANY viewport: a position persisted on a large screen (or
  // synced from another device) can otherwise land entirely off-screen on a smaller one,
  // with the close button unreachable and no way to dismiss the open panel.
  if (typeof window === 'undefined') return pos;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  return {
    x: Math.min(Math.max(8, pos.x), Math.max(8, vw - 280)),
    y: Math.min(Math.max(8, pos.y), Math.max(8, vh - 180))
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
  // A multi-step plan spans several host pages, driven page by page. Each confirmation action is
  // confirmed on the page the user is on when it becomes the active step — its source contextType —
  // and navigating to its target advances to the next page. So a host page renders only the actions
  // whose source contextType matches it: a project_list action shows on project_list (and navigating
  // away after confirming it moves the user to its target). This keeps an unrelated page's pending
  // action from leaking onto the current page.
  const actionContext = String(action.payload?.contextType || '').trim();
  if (actionContext) {
    return actionContext === contextType;
  }
  // Legacy actions (persisted before actions carried their own page) have no contextType. Show them
  // only on project_list — the canonical entry page where most turns originate — rather than on every
  // page, so a stale pending action does not leak onto an unrelated page (the original cross-talk bug).
  return contextType === 'project_list';
}

// Argument keys whose values are plumbing flags or filter tokens the label/description already
// conveys — showing them adds noise (e.g. {"create": true}, {"workflowFilter": ...}).
const TRIVIAL_ARGUMENT_KEYS = new Set([
  'create',
  'activityFilter',
  'workflowFilter',
  'sortBy',
  'backendFilter',
  'typeFilter',
  'stateFilter',
  'search',
  'pageSize',
  'updatedWithinDays',
  'minTaskCount',
]);

// Human-readable labels for the argument keys a confirmation action commonly carries, in display
// priority order. Drives the semantic key/value summary that replaces the old raw-JSON dump.
const ARGUMENT_LABELS: Record<string, string> = {
  smiles: 'SMILES',
  structureUrl: 'Structure',
  sequence: 'Sequence',
  accession: 'Accession',
  cid: 'CID',
  projectId: 'Project',
  projectName: 'Project',
  taskRowId: 'Task',
  taskName: 'Task',
  components: 'Components',
  screeningCompounds: 'Library',
  metadataPatch: 'Changes',
  parameterPatch: 'Parameters',
};

interface ActionSummaryEntry {
  label: string;
  value: string;
}

// Build a human-readable summary of the values an action will apply, as labeled rows instead of a
// raw JSON dump. Long values (SMILES, sequences) are truncated so the card stays scannable. Returns
// only entries with a meaningful value, in a stable display order.
function formatActionSummary(action: CopilotPlanAction): ActionSummaryEntry[] {
  const args = action.arguments;
  if (!args || typeof args !== 'object') return [];
  const rows: ActionSummaryEntry[] = [];
  const seenLabels = new Set<string>();
  for (const [key, value] of Object.entries(args)) {
    if (TRIVIAL_ARGUMENT_KEYS.has(key)) continue;
    const label = ARGUMENT_LABELS[key];
    if (!label) continue;
    let text: string;
    if (typeof value === 'string') {
      text = value.trim();
    } else if (typeof value === 'number' || typeof value === 'boolean') {
      text = String(value);
    } else if (Array.isArray(value)) {
      text = `${value.length} item${value.length === 1 ? '' : 's'}`;
    } else if (value && typeof value === 'object') {
      // For nested objects (e.g. parameterPatch), render friendly key=value
      // pairs with display labels so engine/chirality choices are readable.
      const FRIENDLY: Record<string, string> = {
        backend: '',
        boltz2dock: 'Boltz2Dock',
        protenix2dock: 'Protenix2Dock',
        peptideChirality: 'Chirality',
        d: 'D-peptide',
        l: 'L-peptide',
        peptideDesignMode: 'Mode',
        linear: 'Linear', cyclic: 'Cyclic', bicyclic: 'Bicyclic',
      };
      const parts = Object.entries(value as Record<string, unknown>).map(([k, v]) => {
        const kl = k.replace(/_/g, '');
        const labelEntry = Object.prototype.hasOwnProperty.call(FRIENDLY, k) ? FRIENDLY[k]
          : Object.prototype.hasOwnProperty.call(FRIENDLY, kl) ? FRIENDLY[kl] : k;
        const vl = typeof v === 'string' && Object.prototype.hasOwnProperty.call(FRIENDLY, v)
          ? FRIENDLY[v]
          : String(v);
        return labelEntry === '' || labelEntry === undefined ? vl : `${labelEntry}: ${vl}`;
      });
      text = parts.filter(Boolean).join(', ') || '{}';
    } else {
      continue;
    }
    if (!text) continue;
    const display = text.length > 64 ? `${text.slice(0, 61)}...` : text;
    if (seenLabels.has(label)) continue;
    seenLabels.add(label);
    rows.push({ label, value: display });
  }
  return rows;
}


function compactCopilotText(value: unknown, limit: number): string {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...`;
}

export function buildCopilotConversationContext(messages: ProjectCopilotMessage[]): Record<string, unknown> {
  const visibleMessages = messages.filter((message) => message.role === 'user' || message.role === 'assistant');
  // Confirmation receipts (system role) never enter the visible transcript, but their OUTCOME is
  // what the next planner turn needs: a failed apply means the plan must be recovered (diagnose
  // the error, fix the precondition, re-propose), an applied one means the step is done and the
  // plan should advance. Without this the planner cannot tell whether its last confirmed action
  // succeeded, so it either repeats it or abandons the goal mid-flight.
  const actionResolutions = collectRecentActionResolutions(messages);
  if (visibleMessages.length === 0 && actionResolutions.length === 0) {
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
    recent_messages: recent,
    ...(actionResolutions.length > 0 ? { recent_action_resolutions: actionResolutions } : {})
  };
}

// How many confirmation receipts ride along as recent_action_resolutions — enough to cover a
// multi-step plan's confirmed operations, bounded so the context stays small.
const COPILOT_RECENT_ACTION_RESOLUTIONS = 12;

function collectRecentActionResolutions(messages: ProjectCopilotMessage[]): CopilotActionResolution[] {
  const collected: CopilotActionResolution[] = [];
  for (let index = messages.length - 1; index >= 0 && collected.length < COPILOT_RECENT_ACTION_RESOLUTIONS; index -= 1) {
    const message = messages[index];
    if (message.role !== 'system') continue;
    const resolutions = readActionResolutions(message);
    for (let inner = resolutions.length - 1; inner >= 0 && collected.length < COPILOT_RECENT_ACTION_RESOLUTIONS; inner -= 1) {
      collected.unshift(resolutions[inner]);
    }
  }
  return collected;
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
  // When a host page opens a modal-mask dialog (e.g. new-project), the overlay coordinator flags it;
  // the panel collapses to a dock chip so it never overlaps the dialog. Null when no provider is
  // mounted (the panel behaves exactly as before).
  const overlayPresence = useOverlayPresence();
  const suppressedByOverlay = Boolean(overlayPresence?.hasOpenOverlay);
  // Track whether the viewport is mobile-width. CSS media queries handle layout, but the inline
  // style on the panel div (persisted desktop size/position) must be suppressed on mobile so it
  // doesn't override the CSS. This state re-renders on viewport changes (rotation, resize).
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
  // Mobile soft-keyboard handling via visualViewport. dvh alone is unreliable (varies by browser,
  // and on desktop Chrome / older Safari it doesn't shrink at all), so the panel would be covered
  // by the keyboard. visualViewport.height is the precise visible region; when it's smaller than the
  // layout viewport the keyboard is open. We bind the panel's height/max-height/top directly to the
  // visual viewport so the composer (pinned at the bottom of the flex column) always sits right
  // above the keyboard. pageTopOffset follows visualViewport.pageTop so the panel tracks the
  // viewport's vertical position when the page is scrolled under the keyboard on iOS.
  const [visualViewportHeight, setVisualViewportHeight] = useState<number | null>(null);
  const [visualViewportTop, setVisualViewportTop] = useState(0);
  useEffect(() => {
    // Mobile-only: on desktop the soft keyboard never opens, so tracking visualViewport only wastes
    // work — its resize/scroll events fire on trackpad scroll and pinch-zoom, churning state and
    // re-rendering the whole modal for nothing. Reset the bound values when leaving mobile so the
    // panel isn't pinned to a stale height on a desktop that was briefly mobile-width.
    if (typeof window === 'undefined' || !isMobileViewport) {
      setVisualViewportHeight(null);
      setVisualViewportTop(0);
      return;
    }
    const vv = window.visualViewport;
    if (!vv) return;
    let pending = false;
    // Coalesce rapid resize frames (iOS fires dozens during the keyboard-open animation) into a
    // single state update per frame via rAF, so the panel doesn't re-render and re-scroll on every
    // intermediate frame.
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
  const [messages, setMessages] = useState<ProjectCopilotMessage[]>(() =>
    readCachedProjectCopilotMessages(globalCopilotMessageScope(currentUserId))
  );
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
  const [draft, setDraft] = useState(() => readStoredCopilotDraftLocal(draftScope));
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const [plusMenuOpen, setPlusMenuOpen] = useState(false);
  const plusMenuRef = useRef<HTMLDivElement | null>(null);
  const [liveTrace, setLiveTrace] = useState<CopilotTraceStep[]>([]);
  // Stable timestamp for the streaming bubble's meta header, so the header doesn't pop in when
  // the finished assistant message replaces the live bubble.
  const [streamStartedAt, setStreamStartedAt] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pendingActions, setPendingActions] = useState<CopilotPlanAction[]>([]);
  const [applyingActionKey, setApplyingActionKey] = useState<string | null>(null);
  const [bulkAction, setBulkAction] = useState<'apply' | 'cancel' | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // Whether the message list is pinned to the bottom. Auto-scroll only when the user is already
  // at (or near) the bottom, so reading history mid-answer isn't yanked away.
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
  const [mentionCaret, setMentionCaret] = useState(0);
  const [mentionActiveIndex, setMentionActiveIndex] = useState(0);
  const [mentionDismissedDraft, setMentionDismissedDraft] = useState<string | null>(null);
  // ↑/↓ sent-input history (per-user, persisted) + the navigation cursor (null = not navigating).
  const inputHistoryRef = useRef<string[]>([]);
  const historyNavRef = useRef<InputHistoryNav | null>(null);
  // Inline LLM auto-complete: the current ghost suffix + its fetch orchestration.
  const [completions, setCompletions] = useState<string[]>([]);
  const [completionPickerIndex, setCompletionPickerIndex] = useState<number | null>(null);
  // In-flight turn identity + steering texts (pi alignment: interject, don't cancel).
  const activeTurnKeyRef = useRef<string | null>(null);
  const [steeredTurnTexts, setSteeredTurnTexts] = useState<string[]>([]);
  const [completionEnabled, setCompletionEnabled] = useState(false);
  const completionTimerRef = useRef<number | null>(null);
  const completionAbortRef = useRef<AbortController | null>(null);
  const completionTokenRef = useRef(0);
  const draftRef = useRef(draft);
  const contextPayloadRef = useRef(contextPayload);
  const ghostOverlayInnerRef = useRef<HTMLDivElement | null>(null);

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
      if (!open || sending || applyingActionKey || bulkAction) return;
      textareaRef.current?.focus({ preventScroll: true });
    });
  }, [applyingActionKey, bulkAction, open, sending]);

  useEffect(() => {
    return () => {
      if (focusComposerFrameRef.current) {
        window.cancelAnimationFrame(focusComposerFrameRef.current);
      }
    };
  }, []);

  // Track whether the user intentionally switched sessions (via startNewChat/selectSession). When
  // true, loadMessages must NOT override the active session — the user's choice wins.
  const sessionSwitchRef = useRef(false);
  // Why an in-flight send was aborted: 'user' (Stop button) restores the composer draft, while
  // 'session-switch' / 'close' / 'unmount' must NOT — the async catch otherwise resurrects the
  // old session's draft into the newly selected session's composer and storage.
  const abortReasonRef = useRef<'user' | 'session-switch' | 'close' | 'unmount'>('user');

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
      const loaded = await listProjectCopilotMessages(messageScope);
      setMessages(loaded);
      // If the user just switched sessions (new chat / select session), do NOT override — their
      // choice wins. loadMessages still needs to run to refresh the message list, but it should
      // not change activeSessionId back to an old session.
      if (sessionSwitchRef.current) {
        sessionSwitchRef.current = false;
        restoreSessionActions(loaded, activeSessionId);
        return;
      }
      const storedActiveSessionId = await readStoredCopilotActiveSession(currentUserId);
      // Resolve the session id OUTSIDE the setState updater.
      const sessionIds = Array.from(new Set(loaded.map(readSessionId)));
      const latestLoadedSessionId = loaded.length > 0 ? readSessionId(loaded[loaded.length - 1]) : '';
      const currentSessionId = activeSessionId;
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
  }, [activeSessionId, currentUserId, messageScope, open, restoreSessionActions]);

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

  // Keep the conversation pinned to the newest content: when a message arrives, when the live
  // Thinking bubble appears, and as its trace steps stream in — but only if the user hasn't
  // scrolled up to read earlier messages.
  useEffect(() => {
    if (!open) {
      setError(null);
      return;
    }
    if (!stickToBottomRef.current) return;
    const el = scrollRef.current;
    if (!el) return;
    window.setTimeout(() => el.scrollTo({ top: el.scrollHeight }), 0);
  }, [sessionMessages.length, liveTrace.length, sending, open]);

  // When the mobile soft keyboard opens the panel shrinks (visualViewport binding above); re-pin
  // the newest message into view so the composer doesn't cover it. Only fires while the modal is
  // open and the user hasn't scrolled up, so it never fights manual scrollback.
  useEffect(() => {
    if (!open || visualViewportHeight == null || !stickToBottomRef.current) return;
    const el = scrollRef.current;
    if (!el) return;
    window.setTimeout(() => el.scrollTo({ top: el.scrollHeight }), 0);
  }, [visualViewportHeight, open]);

  const handleMessagesScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }, []);

  useEffect(() => {
    if (!open || sending || applyingActionKey || bulkAction) return;
    // Desktop-only: re-focusing on state transitions is a keyboard convenience. On mobile, a
    // programmatic focus here would re-open the soft keyboard the instant a turn finishes — the
    // second half of the keyboard flicker. Mobile users re-open the keyboard by tapping.
    if (isMobileViewport) return;
    focusComposer();
  }, [applyingActionKey, bulkAction, focusComposer, isMobileViewport, open, sending]);

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

  // Persist settings form (proxy / api_url / model — NOT api_key) to localStorage so the user
  // doesn't have to re-enter values every time they reopen the panel.
  useEffect(() => {
    writeStoredSettingsForm(settingsForm);
  }, [settingsForm]);

  // Keep a ref of the latest draft so the async completion callback can detect mid-flight typing.
  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);

  // Load this user's sent-input history once the user is known; reset any navigation cursor.
  useEffect(() => {
    inputHistoryRef.current = readStoredInputHistory(currentUserId);
    historyNavRef.current = null;
  }, [currentUserId, draftScope]);

  // Discover whether inline completion is enabled on the backend (one cheap GET per open).
  useEffect(() => {
    if (!open) return;
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
  }, [open]);

  // Inline @-mention detection for uploaded attachments. Computed from draft + caret; null when no
  // active mention. Hoisted above the completion effect because that effect gates on this value.
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
    // cmdk-style fuzzy ranking (absorbed command-score): word-boundary jumps beat character
    // jumps, non-matching attachments drop out; a bare @ with no query keeps upload order.
    const options = normalizedQuery
      ? fuzzyRank(uploadedAttachments, normalizedQuery, (attachment) => attachment.name).slice(0, 6)
      : uploadedAttachments.slice(0, 6);
    if (options.length === 0) return null;
    return { start: atIndex, end: caret, query, options };
  }, [draft, mentionCaret, mentionDismissedDraft, uploadedAttachments]);

  // Debounced inline-completion fetch. On every draft change: cancel anything in flight, clear the
  // current ghost, then after a short pause ask the model for a continuation. Best-effort — a stale
  // or aborted result is dropped, and any failure leaves the ghost empty. ``contextPayload`` is read
  // via a ref because parents pass an inline object (new identity each render); depending on it would
  // re-run this effect — and clear the ghost — on every unrelated parent re-render.
  // The ghost suffix is the top-ranked prediction; the picker exposes the rest (top-10).
  const completion = completions[0] || '';
  const acceptCompletion = (suffix: string) => {
    if (!suffix) return;
    const next = `${draft}${suffix}`;
    setCompletions([]);
    setCompletionPickerIndex(null);
    historyNavRef.current = null;
    setDraft(next);
    setMentionCaret(next.length);
    window.requestAnimationFrame(() => {
      textareaRef.current?.focus({ preventScroll: true });
      textareaRef.current?.setSelectionRange(next.length, next.length);
    });
  };

  // Conversation read via ref: an unrelated message-list change (receipt landing, session
  // restore finishing) must NOT clear the ghost and re-fire the fetch — the ghost is a
  // per-keystroke prediction and survives until the draft actually changes (the historical
  // flicker/drop bug: any sessionMessages identity change wiped the prediction mid-typing).
  const completionConversationRef = useRef(sessionMessages);
  completionConversationRef.current = sessionMessages;
  useEffect(() => {
    if (completionTimerRef.current !== null) {
      window.clearTimeout(completionTimerRef.current);
      completionTimerRef.current = null;
    }
    completionAbortRef.current?.abort();
    completionAbortRef.current = null;
    setCompletions([]);
    setCompletionPickerIndex(null);

    if (!completionEnabled || !open || sending || applyingActionKey || bulkAction || attachmentMentionState) {
      return;
    }
    const snapshot = draft;
    if (!snapshot.trim()) return;
    const token = ++completionTokenRef.current;
    completionTimerRef.current = window.setTimeout(() => {
      completionTimerRef.current = null;
      const controller = new AbortController();
      completionAbortRef.current = controller;
      // Merge the recent conversation into the completer's context so it can predict follow-up
      // intent (e.g. after "aspirin SMILES" the user typing "它的靶点" should complete toward
      // targets). The planner already gets this via copilot_conversation; the completer needs the
      // same recency window to anticipate what the user is most likely to say next.
      const conversationContext = buildCopilotConversationContext(completionConversationRef.current);
      const enrichedPayload = { ...contextPayloadRef.current, copilot_conversation: conversationContext };
      void requestCopilotCompletions(
        { contextType, contextPayload: enrichedPayload, userId: currentUserId, username: currentUsername, content: snapshot },
        controller.signal
      ).then((ranked) => {
        if (token !== completionTokenRef.current || controller.signal.aborted) return;
        if (ranked.length > 0 && draftRef.current === snapshot) setCompletions(ranked);
      });
    }, 400);
    return () => {
      if (completionTimerRef.current !== null) {
        window.clearTimeout(completionTimerRef.current);
        completionTimerRef.current = null;
      }
      completionAbortRef.current?.abort();
      completionAbortRef.current = null;
    };
  }, [attachmentMentionState, applyingActionKey, bulkAction, completionEnabled, contextType, currentUserId, currentUsername, draft, open, sending]);

  // Keep the contextPayload ref current (parents pass an inline object, so this is frequent + cheap).
  useEffect(() => {
    contextPayloadRef.current = contextPayload;
  }, [contextPayload]);

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

  // Global ⌘K palette handoff: the palette dispatches a window event; every mounted panel
  // instance calls its onOpen — only the page the user is on has a visible launcher, so
  // this opens the panel right where they are.
  useEffect(() => {
    const onPaletteOpen = () => onOpen();
    window.addEventListener('vbio:open-copilot', onPaletteOpen);
    return () => window.removeEventListener('vbio:open-copilot', onPaletteOpen);
  }, [onOpen]);

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
  }, [currentUserId, open]);

  const startDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    // On mobile (full-screen mode), dragging is disabled — the panel is pinned to the viewport.
    if (isMobileViewport) return;
    const target = event.target as HTMLElement;
    if (target.closest('button, textarea, input, select, a')) return;
    // Only start dragging on primary pointer (left mouse). This prevents drag from blocking
    // touch clicks on header buttons on mobile/touch devices.
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

  const syncMentionCaretFromTextarea = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    setMentionDismissedDraft(null);
    setMentionCaret(textarea.selectionStart ?? textarea.value.length);
  }, []);

  const sendMessage = async (overrideContent?: string) => {
    const isChipAnswer = typeof overrideContent === 'string';
    const content = (isChipAnswer ? overrideContent! : draft).trim();
    // Re-entrancy guard: a turn may already be in flight (the send button becomes a Stop button, but
    // Enter and question-chip answers bypass it). Starting a second stream would orphan the first
    // (its AbortController gets clobbered) and double-insert messages. Treat a send while sending as
    // a cancel of the in-flight turn, not a new one — matching the Stop button's behavior.
    if (!content || applyingActionKey || bulkAction) return;
    // A user-typed turn supersedes any armed auto-continuation — both the in-memory arm and
    // the cross-page storage entry, or the effect would fire the stale continuation after
    // this turn ends.
    if (!isChipAnswer) {
      continuationArmedRef.current = null;
      clearStoredCopilotContinuation(currentUserId);
    }
    if (sending) {
      // STEERING (pi agent-loop alignment): an interjection while a turn runs is queued to
      // the in-flight stream and drained between planner rounds — the turn adapts instead of
      // being killed and retyped. Chip answers still cancel (they answer a pending question
      // of a PREVIOUS turn's UI, not this stream). If the steer POST fails (turn just
      // finished), fall back to sending a fresh turn with the same text.
      if (!isChipAnswer && activeTurnKeyRef.current) {
        const steered = content;
        setDraft('');
        writeStoredCopilotDraftLocal(draftScope, '');
        setSteeredTurnTexts((prev) => [...prev, steered]);
        const queued = await submitCopilotSteering({ turnKey: activeTurnKeyRef.current, text: steered });
        if (queued) return;
        setSteeredTurnTexts((prev) => prev.filter((item) => item !== steered));
        setDraft(steered);
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
    // LOCAL controller for the whole turn: session-switch/close/unmount handlers null out
    // abortRef during the DB-await window below, and re-reading abortRef.current?.signal after
    // the await would start an UNCANCELLABLE orphan stream. The local reference survives.
    const controller = new AbortController();
    abortRef.current = controller;
    abortReasonRef.current = 'user';
    setStreamStartedAt(new Date().toISOString());
    setError(null);
    setLiveTrace([]);
    // Only clear the composer draft when sending what the user typed. A chip answer (guided
    // question) must not wipe a draft the user may still be composing alongside it.
    if (!isChipAnswer) {
      setDraft('');
      writeStoredCopilotDraftLocal(draftScope, '');
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
      // Synthetic-turn dedupe: an auto-continuation whose send was aborted by a page
      // navigation already inserted its user row (the stream died, no assistant reply). The
      // cross-page retry must REUSE that orphan instead of stacking a second identical row —
      // only programmatic (chip) turns dedupe; a user re-typing the same text is their own
      // message and always inserts.
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
        // The turn already streamed (possibly minutes of LLM work): a receipt-DB failure must
        // not discard it. Fall back to a local-only row — shown now, persisted on the next
        // successful insert path (session restore will drop it, but the user has the answer
        // and can act on the confirmation cards immediately).
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
      // Same context filter as the restore path: a cross-page action landing here would render
      // an Apply button whose host branch throws on this page and files a bogus failed receipt.
      // A turn that proposes NOTHING for this page must not wipe actions a mid-stream session
      // load restored (page remount during the turn) — those still await the user's decision.
      setPendingActions((prev) => {
        const next = planActions.filter((action) => actionMatchesContext(action, contextType));
        return next.length > 0 ? next : prev;
      });
      // Desktop-only re-focus after a turn: on mobile this would re-open the soft keyboard the user
      // just dismissed by sending, causing the keyboard to flicker back up.
      if (!isMobileViewport) focusComposer();
    } catch (err) {
      // Canceled send — restore the composer draft ONLY when the user pressed Stop. Session
      // switches, panel close, and unmount abort the stream for navigation reasons; restoring
      // the draft there would leak the old session's text into the new one.
      if (err instanceof DOMException && err.name === 'AbortError') {
        if (abortReasonRef.current === 'user' && !isChipAnswer) setDraft(content);
        if (!isMobileViewport) focusComposer();
        return;
      }
      setError(err instanceof Error ? err.message : 'Failed to send Copilot message.');
      if (!isChipAnswer) {
        setDraft(content);
      }
      if (!isMobileViewport) focusComposer();
    } finally {
      // Identity-guarded cleanup: if a NEW turn already started (its controller replaced
      // abortRef), clearing the shared state here would kill the new turn's Stop button/trace.
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
  // Keep a ref to the latest sendMessage so the memoized answerQuestion callback (passed into the
  // memoized message items) stays stable across renders without forcing every message to re-render
  // on each keystroke/drag. Without this, an inline arrow at the call site would break the memo.
  const sendMessageRef = useRef(sendMessage);
  sendMessageRef.current = sendMessage;
  const answerQuestion = useCallback((answer: string) => {
    void sendMessageRef.current(answer);
  }, []);

  // AUTO-CONTINUATION (pi loop alignment): confirming an action IS the "tool result" of a write
  // operation — the agent loop must resume on its own instead of stalling until the user thinks
  // to type again. Armed with the APPLIED ACTION'S OWN plan id (never a remembered ref — a
  // stale plan id from an earlier turn/session must not fire here); fires once per plan id
  // with a hard cap. Guards: panel open (no phantom turns behind a closed launcher), no
  // bulk cancel in flight, no step still awaiting confirmation, and none of the busy states
  // sendMessage itself guards on (applyingActionKey / sending).
  // The armed entry survives PAGE NAVIGATION via sessionStorage: an apply that navigates
  // (tasks:create_*) unmounts this page before the continuation effect can fire, so the next
  // page's modal consumes the stored entry on mount and resumes the loop there — multi-page
  // plans advance page by page instead of stalling at the first navigation.
  // FAILED receipts arm the loop too, with the recovery prompt: a failed apply is a tool
  // error the planner must diagnose and recover from, never a dead end after which the
  // transcript's last word is an untruthful pre-written success claim.
  const continuationArmedRef = useRef<CopilotContinuation | null>(null);
  const continuedPlansRef = useRef<Set<string>>(new Set());
  // A continuation send still in flight when this page unmounts (or the panel closes) must
  // be handed to the next mount instead of dying with the aborted request.
  const continuationInFlightRef = useRef<CopilotContinuation | null>(null);
  // Plans with at least one failed receipt — arming speaks the failed outcome even when the
  // plan's LAST action applied cleanly.
  const failedPlansRef = useRef<Set<string>>(new Set());
  // While waiting for the arming receipt to become visible (cross-page mount raced the
  // receipt POST), a timer nudges this effect back to life — sessionStorage writes and a
  // completed POST notify React about nothing.
  const continuationPollTimerRef = useRef<number | null>(null);
  const [continuationNudge, setContinuationNudge] = useState(0);
  useEffect(() => {
    if (!open || bulkAction || pendingActions.length > 0 || applyingActionKey || sending) return;
    const armed =
      continuationArmedRef.current ??
      (() => {
        const stored = readStoredCopilotContinuation(currentUserId);
        return stored && stored.sessionId === activeSessionId ? stored : null;
      })();
    if (!armed) return;
    continuationArmedRef.current = null;
    // Session and TTL checks apply to BOTH arms (a ref entry was never TTL-checked before it
    // reached storage): a session switch between the arm and now must not fire a turn into
    // the wrong session, and a stale arm must not resurrect a long-dead plan.
    if (armed.sessionId !== activeSessionId || Date.now() - armed.at > COPILOT_CONTINUATION_TTL_MS) {
      clearStoredCopilotContinuation(currentUserId);
      return;
    }
    // The continuation turn must SEE the receipt it continues from. On a freshly mounted
    // page the storage entry can be consumed before the session's messages (including the
    // receipt) have loaded; firing then would resume the loop blind. Wait for the arming
    // operation's receipt to become visible; the TTL bounds the wait.
    const receiptVisible = sessionMessages.some((message) =>
      readActionResolutions(message).some((row) => row.plan_id === armed.planId && row.operation_id === armed.operationId)
    );
    if (!receiptVisible) {
      // Re-arm in memory (storage keeps its copy) so a later effect run — typically when the
      // message load lands — picks this up; the ref path and the storage path stay identical.
      continuationArmedRef.current = armed;
      // The load may already have completed BEFORE the receipt POST committed (a page remount
      // races it), and nothing will re-run this effect on its own — poll briefly.
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
  }, [open, activeSessionId, bulkAction, pendingActions.length, applyingActionKey, sending, currentUserId, sessionMessages, continuationNudge]);

  const cancelSending = useCallback(() => {
    abortReasonRef.current = 'user';
    abortRef.current?.abort();
  }, []);

  // Hand an in-flight continuation to the NEXT mount: the send is being aborted, but the
  // plan it belongs to was user-confirmed and its loop must not die with the request. The
  // timestamp is refreshed — a long streamed turn must not hand off an entry that is
  // already older than the TTL.
  const rearmInFlightContinuation = () => {
    const inFlight = continuationInFlightRef.current;
    if (!inFlight) return;
    continuationInFlightRef.current = null;
    writeStoredCopilotContinuation(currentUserId, { ...inFlight, at: Date.now() });
  };

  // Abort any in-flight turn when the modal closes or the component unmounts. Without this, closing
  // the panel mid-turn leaves the fetch running up to the server timeout, and its resolution path
  // can insert a message against a stale session after the user has moved on. The inline-completion
  // path already does this (completionAbortRef); the main send path needs the same treatment.
  // Closing the panel is not cancelling the plan: an aborted auto-continuation is re-armed so
  // reopening the panel (on this or the next page) resumes it.
  useEffect(() => {
    if (open) return;
    abortReasonRef.current = 'close';
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    setLiveTrace([]);
    setStreamStartedAt('');
    rearmInFlightContinuation();
  }, [open]);

  // Viewport shrink (window resize, monitor change, tablet rotation): re-clamp the floating
  // panel so a position persisted on a larger screen never strands it off-viewport.
  useEffect(() => {
    if (!open || isMobileViewport) return;
    const onResize = () => {
      setPosition((prev) => {
        if (!prev) return prev;
        const clamped = clampPanelPosition(prev);
        return clamped.x === prev.x && clamped.y === prev.y ? prev : clamped;
      });
    };
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [open, isMobileViewport]);

  // Escape closes the panel — the last-resort exit when the close button is hard to reach.
  useEffect(() => {
    if (!open) return;
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
  }, [open, onClose]);

  // Unmount abort: this panel is mounted per host page (projects / tasks / task detail), so page
  // navigation unmounts it mid-stream — the fetch must be canceled, not left running in the
  // background with its setState/insert callbacks firing against a gone component. A
  // continuation send aborted by this unmount is re-armed into sessionStorage so the next
  // page's modal resumes the loop instead of the continuation dying with the request.
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
    textarea.style.height = `${Math.min(textarea.scrollHeight, 220)}px`;
  }, []);

  // The ghost overlay mirrors the textarea's scroll so its suffix stays glued to the caret when the
  // draft exceeds the composer's max height.
  const syncGhostScroll = useCallback(() => {
    const inner = ghostOverlayInnerRef.current;
    const textarea = textareaRef.current;
    if (inner && textarea) {
      inner.style.transform = `translateY(${-textarea.scrollTop}px)`;
    }
  }, []);

  // Apply a recalled history value: set the draft and park the caret at the end so a further ↑/↓ is
  // a single predictable step.
  const applyHistoryValue = useCallback((value: string) => {
    setDraft(value);
    setMentionCaret(value.length);
    window.requestAnimationFrame(() => {
      const textarea = textareaRef.current;
      if (!textarea) return;
      textarea.focus({ preventScroll: true });
      textarea.setSelectionRange(value.length, value.length);
    });
  }, []);

  useEffect(() => {
    resizeComposer();
  }, [draft, resizeComposer]);

  const startNewChat = () => {
      continuationArmedRef.current = null;
      continuedPlansRef.current = new Set();
      failedPlansRef.current = new Set();
      clearStoredCopilotContinuation(currentUserId);
    // Abort any in-flight turn before swapping sessions: otherwise the stream's onTrace/onResult
    // callbacks keep running and land their assistant message + trace in the NEW session.
    abortReasonRef.current = 'session-switch';
    abortRef.current?.abort();
    abortRef.current = null;
    sessionSwitchRef.current = true;
    const nextSessionId = createSessionId();
    activateSession(nextSessionId);
    setSending(false);
    setDraft('');
    writeStoredCopilotDraftLocal(draftScope, '');
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
    // Same stream-abort as startNewChat: a turn streaming in session A must not resolve into
    // session B. Aborting cancels the fetch; clearing sending/trace stops the live UI.
    abortReasonRef.current = 'session-switch';
    abortRef.current?.abort();
    abortRef.current = null;
    sessionSwitchRef.current = true;
    activateSession(sessionId);
    setSending(false);
    setLiveTrace([]);
    setStreamStartedAt('');
    setDraft('');
    writeStoredCopilotDraftLocal(draftScope, '');
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

  // ----- Copilot runtime settings (proxy / LLM server / API key) -----

  // Wrapper that swallows network/session-expiry errors from ensureManagementSession
  // (renewManagementSession throws on 401 or transport failure) and returns null instead,
  // so the handlers can treat "no usable token" uniformly.
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
        // The action's arguments ride along so a later recovery/summary turn can cite WHAT
        // was applied (pdbId, SMILES, …) — receipts are the only place those values stay
        // verifiable for the grounding audit after the proposing turn scrolls away.
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
    // A failed receipt is a blocker the plan must recover from, and every later action of
    // the SAME plan resolves against that knowledge: record the plan as failed so the
    // continuation arming speaks the failed outcome even when the LAST action applies.
    // Recorded only AFTER the receipt persisted — an insert that throws must not poison the
    // plan (the retry that succeeds would then arm a false 'failed' continuation).
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
      if (!onSendAttachments) throw new Error('This page cannot apply Copilot file attachments.');
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
      await onSendAttachments(selectedAttachments, '', applications);
      return null;
    }
    if (!onApplyPlanAction) throw new Error('This Copilot action cannot be applied on the current page.');
    const result = await onApplyPlanAction(action);
    return typeof result === 'string' ? result : null;
  };

  const applyAction = async (action: CopilotPlanAction): Promise<boolean> => {
    // Entry mutex: never start an apply while another apply, a bulk cancel, or a streaming
    // turn is in flight — sendMessage auto-cancels pendingActions mid-turn, and a concurrent
    // apply here can write an `applied` receipt for the same key the turn is cancelling.
    if (applyingActionKey || bulkAction || sending) return false;
    const actionKey = planActionKey(action);
    setApplyingActionKey(actionKey);
    setError(null);
    const armedPlanId = String(action.plan_id || '').trim() || null;
    const armedOperationId = String(action.operation_id || '').trim();
    const isTerminalEffect = action.effect === 'execute' || action.payload?.destructive === true;
    // Arm the auto-continuation when this was the LAST action of the plan: the loop resumes
    // by itself (see the continuation effect). A cleanly applied TERMINAL effect
    // (execute/destructive: submit, cancel, delete) ends the goal — the receipt speaks, no
    // follow-up turn. ANY failure — terminal or not — arms the loop with the failed
    // outcome instead: the failed receipt is the tool result the planner must diagnose and
    // recover from, and leaving it un-armed is exactly the failure where the transcript's
    // last word is a pre-written success claim over two failed receipts.
    // NOTE 1: computed from the closure's pendingActions, never inside a setPendingActions
    // updater — a NAVIGATING apply (tasks:create_*) unmounts this page during the receipt
    // await, and React silently drops an unmounted component's state updater. The entry
    // mutex above (no concurrent apply / bulk cancel / streaming turn) keeps this closure
    // authoritative; a session switch mid-apply is contained by the sessionId check in the
    // continuation effect.
    // NOTE 2: arming runs BEFORE the receipt insert is awaited, deliberately. The navigating
    // apply unmounts this page within a frame; the arm (ref + sessionStorage) must already
    // be on disk when the next page's modal mounts, or the continuation is silently lost —
    // the receipt insert's network round-trip must not sit in front of it. The consuming
    // effect waits for the receipt to become visible before it actually fires.
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
      // The arming apply may navigate (tasks:create_*) and unmount this page before the
      // continuation effect fires — sessionStorage carries the handoff to the next page.
      writeStoredCopilotContinuation(currentUserId, armed);
    };
    try {
      const detailMessage = await executeAction(action);
      armPlanContinuation(true);
      try {
        await persistActionResolutions([action], 'applied', detailMessage);
      } catch (persistErr) {
        // The HOST change already happened — a receipt-persistence failure must NOT be
        // reported as a failed action (the planner would re-apply an applied operation).
        // Keep the action pending so the user can retry, and surface the persist error.
        setError(persistErr instanceof Error ? persistErr.message : 'Applied, but saving the receipt failed. The operation stays listed — you can retry to record it.');
        return false;
      }
      return true;
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Failed to apply Copilot action.';
      armPlanContinuation(false);
      // The failure surfaces ONCE, in the failed-action receipt at the bottom of the chat (the
      // transcript is the single source of truth — same place the action was proposed). No
      // duplicate banner above: only if the receipt itself cannot be persisted (the action
      // stays pending with no other signal) does the banner carry the reason.
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
    // The user killed the plan: any armed continuation for it must not fire afterwards and
    // instruct the model to keep executing what was just cancelled.
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
      className={`copilot-floating-panel${suppressedByOverlay ? ' copilot-suppressed' : ''}`}
      style={
        suppressedByOverlay
          ? { right: 24, bottom: 24 }
          : isMobileViewport
            ? // On mobile the panel is pinned full-screen by CSS. When the soft keyboard is open,
              // visualViewport reports the visible region above it — bind the panel to that region
              // (height + max-height + top) so the composer sits right above the keyboard instead of
              // being covered. When the keyboard is closed visualViewportHeight == layout height and
              // these reduce to the CSS full-screen rules (top:0, height:100dvh).
              visualViewportHeight != null
                ? {
                    top: visualViewportTop,
                    height: visualViewportHeight,
                    maxHeight: visualViewportHeight,
                  }
                : undefined
            : {
                ...(position ? { left: position.x, top: position.y } : {}),
                ...(panelSize ? { width: panelSize.width, height: panelSize.height } : {})
              }
      }
      role="dialog"
      aria-modal="false"
      aria-hidden={suppressedByOverlay ? 'true' : undefined}
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
            {authSession?.isAdmin ? (
              <button className="task-row-action-btn" type="button" onClick={openSettings} aria-label="Copilot settings" title="Copilot settings">
                <Settings size={15} />
              </button>
            ) : null}
            <button className="task-row-action-btn" type="button" onClick={onClose} aria-label="Close Copilot" title="Close">
              <X size={15} />
            </button>
          </div>
        </div>

        {error ? <div className="alert error copilot-error">{error}</div> : null}

        {historyOpen ? (
          <aside className="copilot-history">
            <div className="copilot-history-head">
              <span className="copilot-history-label">Chats</span>
              <button type="button" className="copilot-history-new" onClick={startNewChat} title="New chat">
                <MessageSquarePlus size={14} />
              </button>
            </div>
            <div className="copilot-history-list">
              {chatSessions.length === 0 ? (
                <div className="copilot-history-empty">No previous chats</div>
              ) : (
                chatSessions.map((session) => (
                  <div className={`copilot-history-item${session.id === activeSessionId ? ' active' : ''}`} key={session.id}>
                    <button type="button" className="copilot-history-btn" onClick={() => selectSession(session.id)}>
                      <span className="copilot-history-title">{session.title}</span>
                      {session.updatedAt ? (
                        <small className="copilot-history-time">{formatDateTime(session.updatedAt)}</small>
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

        {settingsOpen ? (
          <div className="copilot-settings-overlay">
            <div className="copilot-settings-panel" onClick={(e) => e.stopPropagation()}>
              <div className="copilot-settings-head">
                <span className="copilot-settings-title">
                  <Settings size={15} />
                  Copilot Settings
                </span>
                <button type="button" className="copilot-settings-close" onClick={() => setSettingsOpen(false)} aria-label="Close settings" title="Close settings">
                  <X size={16} />
                </button>
              </div>
              <div className="copilot-settings-body">
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">Outbound Proxy</span>
                  <input
                    type="text"
                    className="copilot-settings-input"
                    placeholder="http://172.16.34.31:2080"
                    value={settingsForm.proxy}
                    onChange={(e) => setSettingsForm((prev) => ({ ...prev, proxy: e.target.value }))}
                  />
                </label>
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">LLM Server URL</span>
                  <input
                    type="text"
                    className="copilot-settings-input"
                    placeholder="https://api.openai.com/v1/chat/completions"
                    value={settingsForm.api_url}
                    onChange={(e) => setSettingsForm((prev) => ({ ...prev, api_url: e.target.value }))}
                  />
                </label>
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">
                    API Key{settingsHasKey ? <em className="copilot-settings-current"> (current: {settingsMaskedKey})</em> : null}
                  </span>
                  <input
                    type="password"
                    className="copilot-settings-input"
                    placeholder={settingsHasKey ? 'Leave blank to keep current key' : 'Enter API key'}
                    value={settingsForm.api_key}
                    onChange={(e) => setSettingsForm((prev) => ({ ...prev, api_key: e.target.value }))}
                  />
                </label>
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">Model</span>
                  <input
                    type="text"
                    className="copilot-settings-input"
                    placeholder="e.g. gpt-4o"
                    value={settingsForm.model}
                    onChange={(e) => setSettingsForm((prev) => ({ ...prev, model: e.target.value }))}
                  />
                </label>
                {settingsError ? <div className="copilot-settings-error">{settingsError}</div> : null}
                {settingsSaved ? <div className="copilot-settings-success">Settings saved — applied live.</div> : null}
                {settingsTestResult ? (
                  <div className="copilot-settings-test-results">
                    <div className={`copilot-settings-test-item ${settingsTestState(settingsTestResult.proxy)}`}>
                      <span className="copilot-settings-test-name">Network</span>
                      <span className="copilot-settings-test-detail">{settingsTestResult.proxy.detail}</span>
                    </div>
                    <div className={`copilot-settings-test-item ${settingsTestState(settingsTestResult.llm)}`}>
                      <span className="copilot-settings-test-name">LLM</span>
                      <span className="copilot-settings-test-detail">{settingsTestResult.llm.detail}</span>
                    </div>
                  </div>
                ) : null}
              </div>
              <div className="copilot-settings-actions">
                <button
                  type="button"
                  className="copilot-settings-btn secondary"
                  onClick={() => void handleTestSettings()}
                  disabled={settingsTesting || settingsSaving}
                >
                  {settingsTesting ? <LoaderCircle size={14} className="spin" /> : null}
                  Test Connection
                </button>
                <button
                  type="button"
                  className="copilot-settings-btn primary"
                  onClick={() => void handleSaveSettings()}
                  disabled={settingsSaving || settingsTesting}
                >
                  {settingsSaving ? <LoaderCircle size={14} className="spin" /> : null}
                  Save Settings
                </button>
              </div>
            </div>
          </div>
        ) : null}

        <div className="copilot-messages" ref={scrollRef} onScroll={handleMessagesScroll}>
          {loading ? (
            <div className="copilot-empty">
              <LoaderCircle size={16} className="spin" />
              Loading messages
            </div>
          ) : sessionMessages.length === 0 ? (
            null
          ) : (
            sessionMessages.map((message) => (
              <CopilotMessageItem
                key={message.id}
                message={message}
                disabled={sending || Boolean(applyingActionKey || bulkAction)}
                onAnswerQuestion={answerQuestion}
              />
            ))
          )}
          {sending ? (
            <article className="copilot-message is-assistant">
              <div className="copilot-message-meta">
                <strong>V-Bio Copilot</strong>
                <span>{formatDateTime(streamStartedAt)}</span>
              </div>
              {/* Same shape as a finished assistant message (meta + body + reasoning sibling) so
                  completion only fills the body instead of restructuring the bubble — no jitter. */}
              <div className="copilot-message-body copilot-thinking" />
              {steeredTurnTexts.length > 0 ? (
                <div className="copilot-steered-list" aria-label="已插入的转向消息">
                  {steeredTurnTexts.map((text, index) => (
                    <div className="copilot-steered-item" key={`${index}-${text.slice(0, 24)}`}>
                      <span className="copilot-steered-badge">已插入</span>
                      <span className="copilot-steered-text">{text}</span>
                    </div>
                  ))}
                </div>
              ) : null}
              <CopilotThinkingCard steps={liveTrace} live />
            </article>
          ) : null}
        </div>

        {pendingActions.length > 0 ? (() => {
          // Progressive reveal: show ONE confirmation card at a time. When the user confirms it,
          // persistActionResolutions removes it from pendingActions, so the next step becomes
          // pendingActions[0] on the next render — no extra state or effect needed. This keeps long
          // plans manageable (one decision at a time) and avoids surfacing every step at once.
          const action = pendingActions[0];
          const actionKey = planActionKey(action);
          const isApplying = applyingActionKey === actionKey;
          const summary = formatActionSummary(action);
          const isDestructive = action.payload?.destructive === true;
          // Include `sending`: a streaming turn auto-cancels pendingActions mid-flight, so an
          // Apply clicked during the stream races the cancel and can double-write receipts.
          const blocked = Boolean(applyingActionKey || bulkAction || sending);
          return (
            <div className="copilot-action-stack" aria-label="Pending confirmation step">
              {/* Deterministic honesty guard: whatever the assistant message above says, the
                  pending step has NOT executed. This line is the UI's own statement of fact —
                  it neutralizes a model that narrates proposed operations as completed. */}
              <div className="copilot-plan-pending-hint">
                此操作尚未执行——点击「应用」后才会生效；实际结果以确认后的回执为准。
              </div>
              <div className="copilot-plan-actions">
                <div
                  className={`copilot-plan-action${isDestructive ? ' is-destructive' : ''}${isApplying ? ' is-applying' : ''}`}
                  key={actionKey}
                >
                  <div className="copilot-plan-action-main">
                    <strong>{action.label}</strong>
                    <small className="copilot-plan-action-desc">{action.description}</small>
                    {summary.length > 0 ? (
                      <dl className="copilot-plan-action-summary">
                        {summary.map((entry) => (
                          <div className="copilot-plan-action-summary-row" key={entry.label}>
                            <dt>{entry.label}</dt>
                            <dd>{entry.value}</dd>
                          </div>
                        ))}
                      </dl>
                    ) : null}
                  </div>
                  <div className="copilot-plan-action-buttons">
                    <button
                      className="copilot-plan-action-cancel"
                      type="button"
                      onClick={() => void cancelPendingActions()}
                      disabled={Boolean(applyingActionKey || bulkAction)}
                      title="取消"
                    >
                      {bulkAction === 'cancel' ? <LoaderCircle size={14} className="spin" /> : <X size={14} />}
                      <span>取消</span>
                    </button>
                    <button
                      className="copilot-plan-action-apply"
                      type="button"
                      onClick={() => void applyAction(action)}
                      disabled={blocked}
                      title="应用此步骤"
                    >
                      {isApplying ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
                      <span>{isApplying ? '应用中' : '应用'}</span>
                    </button>
                  </div>
                </div>
              </div>
            </div>
          );
        })() : null}

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
              <div className="copilot-plus-wrap" ref={plusMenuRef}>
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
              <div className="copilot-input-wrap">
                {completionPickerIndex !== null && completions.length > 0 ? (
                  <div className="copilot-mention-menu copilot-completion-menu" role="listbox" id="copilot-completion-listbox" aria-label="候选续写">
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
                    <div className="copilot-ghost-overlay-inner" ref={ghostOverlayInnerRef}>
                      <span className="copilot-ghost-spacer">{draft}</span>
                      <span className="copilot-ghost-suffix">{completion}</span>
                    </div>
                  </div>
                ) : null}
                <textarea
                  ref={textareaRef}
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
                onKeyDown={(event) => {
                  if (attachmentMentionState) {
                    // cmdk-absorbed navigation: ArrowUp/Down loop (kept), Home/End jump to the
                    // first/last option, PageUp/PageDown move in pages of 6 — the exact key
                    // set cmdk's list handles (it has no Tab completion built in; our
                    // Enter+Tab insert stays, it predates and exceeds it).
                    const mentionCount = attachmentMentionState.options.length;
                    if (event.key === 'ArrowDown') {
                      event.preventDefault();
                      setMentionActiveIndex((index) => (index + 1) % mentionCount);
                      return;
                    }
                    if (event.key === 'ArrowUp') {
                      event.preventDefault();
                      setMentionActiveIndex((index) => (index - 1 + mentionCount) % mentionCount);
                      return;
                    }
                    if (event.key === 'Home') {
                      event.preventDefault();
                      setMentionActiveIndex(0);
                      return;
                    }
                    if (event.key === 'End') {
                      event.preventDefault();
                      setMentionActiveIndex(mentionCount - 1);
                      return;
                    }
                    if (event.key === 'PageDown') {
                      event.preventDefault();
                      setMentionActiveIndex((index) => Math.min(mentionCount - 1, index + 6));
                      return;
                    }
                    if (event.key === 'PageUp') {
                      event.preventDefault();
                      setMentionActiveIndex((index) => Math.max(0, index - 6));
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
                  // Inline-completion ghost + top-10 picker (mention menu closed, not composing).
                  if (!event.nativeEvent.isComposing && (completion || completions.length > 0)) {
                    if (completionPickerIndex !== null) {
                      // Picker open: ↑/↓ move (loop), Enter/Tab accept the highlighted
                      // candidate, Esc closes ONLY the picker (the ghost survives).
                      if (event.key === 'ArrowDown') {
                        event.preventDefault();
                        setCompletionPickerIndex((index) => ((index ?? 0) + 1) % completions.length);
                        return;
                      }
                      if (event.key === 'ArrowUp') {
                        event.preventDefault();
                        setCompletionPickerIndex((index) => ((index ?? 0) - 1 + completions.length) % completions.length);
                        return;
                      }
                      if (event.key === 'Enter' || event.key === 'Tab') {
                        event.preventDefault();
                        acceptCompletion(completions[completionPickerIndex] ?? completion);
                        return;
                      }
                      if (event.key === 'Escape') {
                        event.preventDefault();
                        setCompletionPickerIndex(null);
                        return;
                      }
                    } else if (completion) {
                      if (event.key === 'Tab') {
                        event.preventDefault();
                        acceptCompletion(completion);
                        return;
                      }
                      if (event.key === 'ArrowDown') {
                        // Ghost showing: ↓ opens the ranked candidates (fish-shell style);
                        // ↑ stays with input-history recall.
                        event.preventDefault();
                        setCompletionPickerIndex(0);
                        return;
                      }
                      if (event.key === 'Escape') {
                        event.preventDefault();
                        setCompletions([]);
                        return;
                      }
                    }
                  }
                  // ↑/↓ sent-input history (caret on first/last line, not composing).
                  if (
                    !event.nativeEvent.isComposing &&
                    (event.key === 'ArrowUp' || event.key === 'ArrowDown') &&
                    shouldNavigateHistory(
                      event.key === 'ArrowUp' ? 'up' : 'down',
                      draft,
                      event.currentTarget.selectionStart ?? draft.length
                    )
                  ) {
                    const result = nextInputHistoryNav(
                      inputHistoryRef.current,
                      historyNavRef.current,
                      draft,
                      event.key === 'ArrowUp' ? 'up' : 'down'
                    );
                    if (result) {
                      event.preventDefault();
                      historyNavRef.current = result.nav;
                      applyHistoryValue(result.value);
                    }
                    return;
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
                onScroll={syncGhostScroll}
                placeholder="输入消息…"
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
      </div>
    </div>
  );
}
