/**
 * Message-bubble cards of the copilot transcript: trace steps, thinking
 * disclosure, observation/question cards and the memoized message item.
 * Zero modal-state dependencies — every card is driven purely by its props
 * (or the message it renders), so they were lifted out of the modal 1:1.
 */
import { memo, useCallback, useMemo, useState } from 'react';
import { ChevronRight, Sparkles } from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { parseCopilotQuestions } from '../../api/copilotApi';
import type { CopilotPlannerQuestion, CopilotTraceStep, ProjectCopilotMessage } from '../../types/models';
import { formatDateTime } from '../../utils/date';
import { formatTraceStep, readPlannerTrace, readActionResolutions } from './copilotTraceUi';
import './CopilotMessageCards.css';

function author(message: ProjectCopilotMessage): string {
  if (message.role === 'assistant') return 'V-Bio Copilot';
  return message.user_name || message.username || 'User';
}

// Planner trace + memory helpers live in ./copilotTraceUi (pure + unit-tested).

// Reasoning steps — plain muted text, one short phrase per step (wording in formatTraceStep).
// The latest streaming step brightens; everything else stays quiet so the panel reads as part of
// the message instead of a debug log.
// Rows are memoized per step object: trace steps are append-only (identity never changes), so a
// streaming turn that adds step N re-renders ONLY step N instead of re-formatting and reconciling
// every earlier step on each SSE frame.
const TraceStepRow = memo(function TraceStepRow({ step, isLast }: { step: CopilotTraceStep; isLast: boolean }) {
  return (
    <li className={`copilot-trace-item${isLast ? ' is-current' : ''}`}>
      {formatTraceStep(step)}
    </li>
  );
});

function TraceStepList({ steps, highlightLast }: { steps: CopilotTraceStep[]; highlightLast?: boolean }) {
  const lastIndex = steps.length - 1;
  return (
    <ol className="copilot-trace-list">
      {steps.map((step, index) => (
        <TraceStepRow
          key={`${step.round}-${step.event}-${index}`}
          step={step}
          isLast={Boolean(highlightLast) && index === lastIndex}
        />
      ))}
    </ol>
  );
}

// Collapsible "thinking / thinking…" disclosure — a quiet inline section of the message: a small
// animated sparkle toggle while live, muted step text below, smooth expand/collapse.
// Memoized: finished messages hold a stable steps array from metadata, so parent re-renders
// (typing, dragging, disabled flips, task-page polling) skip the whole card.
// History cards start COLLAPSED: a long transcript otherwise mounts every trace step of every
// message at once (thousands of <li>), which turns each layout pass — poll re-renders, the
// per-step auto-scroll during streaming — into a full-document layout and freezes the panel.
export const CopilotThinkingCard = memo(function CopilotThinkingCard({ steps, live, pending, onExpand }: { steps: CopilotTraceStep[]; live?: boolean; pending?: boolean; onExpand?: () => void }) {
  const [open, setOpen] = useState(Boolean(live));
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
        onClick={() => {
          const next = !open;
          setOpen(next);
          // Lazy trace: the transcript list projection omits planner_trace (the heaviest
          // metadata field); the first expand of a finished message fetches just that
          // message's steps instead of shipping every turn's trace with the list.
          if (next && pending && onExpand) onExpand();
        }}
        aria-expanded={open}
      >
        <Sparkles className="copilot-thinking-spark" size={13} aria-hidden="true" />
        <span className="copilot-thinking-title">{label}</span>
        <span className="copilot-thinking-meta">
          {pending ? '' : `${steps.length} ${steps.length === 1 ? 'step' : 'steps'}`}
        </span>
        <ChevronRight className="copilot-thinking-chev" size={12} aria-hidden="true" />
      </button>
      <div className="copilot-thinking-body">
        <div className="copilot-thinking-body-inner">
          {/* Collapsed = the step tree is not mounted at all (grid 0fr still lays out the
              children, so a long transcript's thousands of <li> keep costing every layout). */}
          {open ? <TraceStepList steps={steps} highlightLast={live} /> : null}
        </div>
      </div>
    </div>
  );
});

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
                    Other…
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
                  placeholder="Type your answer…"
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
                  Submit
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
            {isAnswered ? <small className="copilot-question-answered">✓ Selected</small> : null}
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
          {allAnswered ? 'Submit answers' : `Answer ${questions.length - Object.keys(answers).length} more question(s)`}
        </button>
      ) : null}
    </div>
  );
}

// ReactMarkdown parses the full message content on every render — with a long transcript that is
// seconds of synchronous work per pass. Content strings are immutable once a message lands, so
// parse each distinct body exactly once and reuse the element for every other re-render.
const CopilotMarkdown = memo(function CopilotMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
      {content}
    </ReactMarkdown>
  );
});

// Message rendering runs ReactMarkdown (expensive). Memoize so a message only re-renders when its
// own content changes — not on every unrelated Copilot state update (typing, dragging, resize,
// caret moves), which otherwise re-parsed markdown for every message and froze the panel.
const EMPTY_TRACE: CopilotTraceStep[] = [];
export const CopilotMessageItem = memo(function CopilotMessageItem({
  message,
  disabled,
  onAnswerQuestion,
  onLoadTrace
}: {
  message: ProjectCopilotMessage;
  disabled: boolean;
  onAnswerQuestion: (answer: string) => void;
  onLoadTrace: (messageId: string) => void;
}) {
  const metadata = message.metadata;
  const trace = useMemo(
    () => (message.role === 'assistant' ? readPlannerTrace(metadata?.planner_trace) : []),
    [message.role, metadata]
  );
  // The list projection ships every transcript message WITHOUT planner_trace; an assistant
  // row whose metadata simply lacks the key still owes its steps (fetched on first expand).
  const tracePending = message.role === 'assistant' && Boolean(metadata) && !('planner_trace' in (metadata || {}));
  const handleExpandTrace = useCallback(() => {
    onLoadTrace(message.id);
  }, [message.id, onLoadTrace]);
  const questions = useMemo(
    () => (message.role === 'assistant' ? readPlannerQuestions(metadata?.planner_questions) : []),
    [message.role, metadata]
  );
  const plannerState = String(metadata?.planner_state || '').trim();
  const showQuestions = plannerState === 'needs_input' && questions.length > 0;
  // Retrieved records from read skills — shown in a collapsible section so the user always sees
  // the authoritative data even when the model's message only summarizes it (e.g. "Here is the sequence:"
  // without pasting 395 chars, which models routinely truncate in structured output).
  const observations = useMemo(
    () => (message.role === 'assistant' ? readObservationRecords(metadata?.planner_observations) : []),
    [message.role, metadata]
  );
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
        <CopilotMarkdown content={message.content} />
      </div>
      {showObservations ? <CopilotObservationCard records={observations} /> : null}
      {showQuestions ? (
        <CopilotQuestionCard questions={questions} disabled={disabled} onAnswer={onAnswerQuestion} />
      ) : null}
      {trace.length > 0 ? (
        <CopilotThinkingCard steps={trace} />
      ) : tracePending ? (
        <CopilotThinkingCard steps={EMPTY_TRACE} pending onExpand={handleExpandTrace} />
      ) : null}
    </article>
  );
});
