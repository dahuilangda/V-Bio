/**
 * Message-bubble cards of the copilot transcript: trace steps, thinking
 * disclosure, observation/question cards and the memoized message item.
 * Purely prop-driven; no modal-state dependencies.
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

// Reasoning steps, one short phrase each (formatTraceStep); the latest streaming step brightens.
// Rows are memoized per step object (append-only identity) so streaming re-renders only the new step.
const TraceStepRow = memo(function TraceStepRow({ step, isLast }: { step: CopilotTraceStep; isLast: boolean }) {
  return (
    <li className={`copilot-trace-item${isLast ? ' is-current' : ''}`}>
      {formatTraceStep(step)}
    </li>
  );
});

function TraceStepList({ steps, isLastHighlighted }: { steps: CopilotTraceStep[]; isLastHighlighted?: boolean }) {
  const lastIndex = steps.length - 1;
  return (
    <ol className="copilot-trace-list">
      {steps.map((step, index) => (
        <TraceStepRow
          key={`${step.round}-${step.event}-${index}`}
          step={step}
          isLast={Boolean(isLastHighlighted) && index === lastIndex}
        />
      ))}
    </ol>
  );
}

// Collapsible "thinking" disclosure, memoized. History cards start collapsed so a
// long transcript doesn't mount every trace step of every message at once.
export const CopilotThinkingCard = memo(function CopilotThinkingCard({ steps, isLive, isPending, onExpand }: { steps: CopilotTraceStep[]; isLive?: boolean; isPending?: boolean; onExpand?: () => void }) {
  const [open, setOpen] = useState(Boolean(isLive));
  // Live with no steps yet: a bare "Thinking…" indicator, no card chrome.
  if (isLive && steps.length === 0) {
    return (
      <span className="copilot-thinking-inline">
        <Sparkles className="copilot-thinking-spark" size={13} aria-hidden="true" />
        <span className="copilot-thinking-title">Thinking…</span>
      </span>
    );
  }
  const label = isLive ? 'Thinking' : 'Reasoning';
  return (
    <div className={`copilot-thinking-card${isLive ? ' is-live' : ''}${open ? ' is-open' : ''}`}>
      <button
        type="button"
        className="copilot-thinking-head"
        onClick={() => {
          const next = !open;
          setOpen(next);
          // Lazy trace: the list projection omits planner_trace; first expand fetches this message's steps.
          if (next && isPending && onExpand) onExpand();
        }}
        aria-expanded={open}
      >
        <Sparkles className="copilot-thinking-spark" size={13} aria-hidden="true" />
        <span className="copilot-thinking-title">{label}</span>
        <span className="copilot-thinking-meta">
          {isPending ? '' : `${steps.length} ${steps.length === 1 ? 'step' : 'steps'}`}
        </span>
        <ChevronRight className="copilot-thinking-chev" size={12} aria-hidden="true" />
      </button>
      <div className="copilot-thinking-body">
        <div className="copilot-thinking-body-inner">
          {/* Collapsed = not mounted; grid 0fr would still lay out the children. */}
          {open ? <TraceStepList steps={steps} isLastHighlighted={isLive} /> : null}
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

// Flatten planner_observations into displayable records (search results carry many, resolve
// one). Long values (SMILES, sequences) are kept in full so the user can copy them.
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

// Retrieved records in a collapsible section so the user sees the authoritative data
// (sequence, SMILES, ...) even when the message only summarizes it.
function CopilotObservationCard({ records }: { records: ObservationRecord[] }) {
  // Auto-expand on long fields — the message often truncates the value; the user
  // shouldn't have to know to click "Retrieved data".
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

// Planner questions as clickable chips. Choice questions add an "Other ___" free-text answer
// (unless allowOther=false); confirm is yes/no; freeform points at the composer.
function CopilotQuestionCard({
  questions,
  isDisabled,
  onAnswer
}: {
  questions: CopilotPlannerQuestion[];
  isDisabled: boolean;
  onAnswer: (answer: string) => void;
}) {
  // Single question answers immediately; multiple questions accumulate locally before submitting.
  const isSingle = questions.length === 1;
  const [answers, setAnswers] = useState<Record<number, string>>({});
  // "Other ___" free-text state per choice question.
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
                      disabled={isDisabled}
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
                    disabled={isDisabled}
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
                  disabled={isDisabled}
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
                  disabled={isDisabled || !String(otherText[questionIndex] || '').trim()}
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
                      disabled={isDisabled}
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
          disabled={isDisabled || !allAnswered}
          onClick={submit}
        >
          {allAnswered ? 'Submit answers' : `Answer ${questions.length - Object.keys(answers).length} more question(s)`}
        </button>
      ) : null}
    </div>
  );
}

// ReactMarkdown is expensive; memoize so each distinct body is parsed exactly once.
const CopilotMarkdown = memo(function CopilotMarkdown({ content }: { content: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
      {content}
    </ReactMarkdown>
  );
});

// Memoize so a message only re-renders when its own content changes, not on every
// unrelated copilot state update.
const EMPTY_TRACE: CopilotTraceStep[] = [];
export const CopilotMessageItem = memo(function CopilotMessageItem({
  message,
  isDisabled,
  onAnswerQuestion,
  onLoadTrace
}: {
  message: ProjectCopilotMessage;
  isDisabled: boolean;
  onAnswerQuestion: (answer: string) => void;
  onLoadTrace: (messageId: string) => void;
}) {
  const metadata = message.metadata;
  const trace = useMemo(
    () => (message.role === 'assistant' ? readPlannerTrace(metadata?.planner_trace) : []),
    [message.role, metadata]
  );
  // List projection omits planner_trace; a missing key means steps are owed (fetched on expand).
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
  // Retrieved records from read skills, shown even when the message only summarizes them.
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
        <CopilotQuestionCard questions={questions} isDisabled={isDisabled} onAnswer={onAnswerQuestion} />
      ) : null}
      {trace.length > 0 ? (
        <CopilotThinkingCard steps={trace} />
      ) : tracePending ? (
        <CopilotThinkingCard steps={EMPTY_TRACE} isPending onExpand={handleExpandTrace} />
      ) : null}
    </article>
  );
});
