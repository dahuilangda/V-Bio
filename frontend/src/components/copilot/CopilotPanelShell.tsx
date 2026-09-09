/**
 * The open-state panel shell of the copilot: floating/draggable panel frame,
 * header, error banner, history sidebar, settings panel, messages list and
 * plan-action card. Pure presentation — zero hooks/effects; every value
 * arrives via seven named prop groups (panel/header/history/settings/
 * messages/turn/plan) re-destructured to the historical local names so the
 * JSX stayed byte-identical. The composer element (with its keyboard map)
 * is built by the modal and passed as `children`, keeping the keymap's
 * ~26 composer-scope identifiers out of this interface.
 */
import type { Dispatch, PointerEvent as ReactPointerEvent, ReactNode, RefObject, SetStateAction } from 'react';
import { LoaderCircle } from 'lucide-react';
import type { CopilotPlanAction, CopilotTraceStep, ProjectCopilotMessage, Session } from '../../types/models';
import type { CopilotTestResult } from '../../api/copilotApi';
import { CopilotHeader } from './CopilotHeader';
import { CopilotHistorySidebar, type CopilotSessionSummary } from './CopilotHistorySidebar';
import { CopilotSettingsPanel } from './CopilotSettingsPanel';
import { CopilotMessageItem } from './CopilotMessageCards';
import { CopilotStreamingBubble } from './CopilotStreamingBubble';
import { CopilotPlanActionCard } from './CopilotPlanActionCard';
import { planActionKey } from './copilotPlanHelpers';
import { TOP_CHROME_PX, type SettingsFormValues } from './ProjectCopilotModal';
import './CopilotPanelShell.css';

export interface CopilotPanelShellPanelProps {
  panelRef: RefObject<HTMLDivElement | null>;
  suppressedByOverlay: boolean;
  isMobileViewport: boolean;
  visualViewportHeight: number | null;
  visualViewportTop: number;
  position: { x: number; y: number } | null;
  panelSize: { width: number; height: number } | null;
  startDrag: (event: ReactPointerEvent<HTMLDivElement>) => void;
  moveDrag: (event: ReactPointerEvent<HTMLDivElement>) => void;
  endDrag: (event: ReactPointerEvent<HTMLDivElement>) => void;
}

export interface CopilotPanelShellHeaderProps {
  title: string;
  subtitle: string;
  authSession: Session | null;
  onClose: () => void;
  openSettings: () => void;
}

export interface CopilotPanelShellHistoryProps {
  historyOpen: boolean;
  setHistoryOpen: Dispatch<SetStateAction<boolean>>;
  chatSessions: CopilotSessionSummary[];
  activeSessionId: string;
  selectSession: (sessionId: string) => void;
  deleteSession: (sessionId: string) => Promise<void>;
  startNewChat: () => void;
}

export interface CopilotPanelShellSettingsProps {
  settingsOpen: boolean;
  setSettingsOpen: Dispatch<SetStateAction<boolean>>;
  settingsForm: SettingsFormValues;
  setSettingsForm: Dispatch<SetStateAction<SettingsFormValues>>;
  settingsError: string;
  settingsHasKey: boolean;
  settingsMaskedKey: string;
  settingsSaved: boolean;
  settingsSaving: boolean;
  settingsTestResult: CopilotTestResult | null;
  settingsTesting: boolean;
  handleSaveSettings: () => Promise<void>;
  handleTestSettings: () => Promise<void>;
}

export interface CopilotPanelShellMessagesProps {
  scrollRef: RefObject<HTMLDivElement | null>;
  handleMessagesScroll: () => void;
  loading: boolean;
  sessionMessages: ProjectCopilotMessage[];
  visibleMessageCount: number;
  setVisibleMessageCount: Dispatch<SetStateAction<number>>;
  MESSAGE_WINDOW: number;
  visibleSessionMessages: ProjectCopilotMessage[];
  answerQuestion: (answer: string) => void;
  loadMessageTrace: (messageId: string) => void;
  streamStartedAt: string;
  steeredTurnTexts: string[];
  liveTrace: CopilotTraceStep[];
}

export interface CopilotPanelShellTurnProps {
  sending: boolean;
  applyingActionKey: string | null;
  bulkAction: 'apply' | 'cancel' | null;
}

export interface CopilotPanelShellPlanProps {
  pendingActions: CopilotPlanAction[];
  applyAction: (action: CopilotPlanAction) => Promise<boolean>;
  cancelPendingActions: () => Promise<void>;
}

interface CopilotPanelShellProps {
  panel: CopilotPanelShellPanelProps;
  header: CopilotPanelShellHeaderProps;
  history: CopilotPanelShellHistoryProps;
  settings: CopilotPanelShellSettingsProps;
  messages: CopilotPanelShellMessagesProps;
  turn: CopilotPanelShellTurnProps;
  plan: CopilotPanelShellPlanProps;
  error: string | null;
  children: ReactNode;
}

export function CopilotPanelShell({
  panel,
  header,
  history,
  settings,
  messages,
  turn,
  plan,
  error,
  children
}: CopilotPanelShellProps) {
  const {
    panelRef,
    suppressedByOverlay,
    isMobileViewport,
    visualViewportHeight,
    visualViewportTop,
    position,
    panelSize,
    startDrag,
    moveDrag,
    endDrag
  } = panel;
  const {
    title,
    subtitle,
    authSession,
    onClose,
    openSettings
  } = header;
  const {
    historyOpen,
    setHistoryOpen,
    chatSessions,
    activeSessionId,
    selectSession,
    deleteSession,
    startNewChat
  } = history;
  const {
    settingsOpen,
    setSettingsOpen,
    settingsForm,
    setSettingsForm,
    settingsError,
    settingsHasKey,
    settingsMaskedKey,
    settingsSaved,
    settingsSaving,
    settingsTestResult,
    settingsTesting,
    handleSaveSettings,
    handleTestSettings
  } = settings;
  const {
    scrollRef,
    handleMessagesScroll,
    loading,
    sessionMessages,
    visibleMessageCount,
    setVisibleMessageCount,
    MESSAGE_WINDOW,
    visibleSessionMessages,
    answerQuestion,
    loadMessageTrace,
    streamStartedAt,
    steeredTurnTexts,
    liveTrace
  } = messages;
  const {
    sending,
    applyingActionKey,
    bulkAction
  } = turn;
  const {
    pendingActions,
    applyAction,
    cancelPendingActions
  } = plan;

  return (
    <div
      ref={panelRef as RefObject<HTMLDivElement>}
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
                    top: TOP_CHROME_PX + Math.max(0, visualViewportTop),
                    height: Math.max(240, visualViewportHeight - TOP_CHROME_PX),
                    maxHeight: Math.max(240, visualViewportHeight - TOP_CHROME_PX)
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
        <CopilotHeader
          title={title}
          subtitle={subtitle}
          isAdmin={Boolean(authSession?.isAdmin)}
          historyOpen={historyOpen}
          onToggleHistory={() => setHistoryOpen((prev) => !prev)}
          onNewChat={startNewChat}
          onOpenSettings={openSettings}
          onClose={onClose}
          onDragStart={startDrag}
          onDragMove={moveDrag}
          onDragEnd={endDrag}
        />

        {error ? <div className="alert error copilot-error">{error}</div> : null}

        {historyOpen ? (
          <CopilotHistorySidebar
            sessions={chatSessions}
            activeSessionId={activeSessionId}
            onSelect={selectSession}
            onDelete={deleteSession}
            onNewChat={startNewChat}
          />
        ) : null}

        {settingsOpen && (
          <CopilotSettingsPanel
            settingsForm={settingsForm}
            settingsError={settingsError}
            settingsHasKey={settingsHasKey}
            settingsMaskedKey={settingsMaskedKey}
            settingsSaved={settingsSaved}
            settingsSaving={settingsSaving}
            settingsTestResult={settingsTestResult}
            settingsTesting={settingsTesting}
            onFormChange={setSettingsForm}
            onSave={handleSaveSettings}
            onTest={handleTestSettings}
            onClose={() => setSettingsOpen(false)}
          />
        )}

        <div className="copilot-messages" ref={scrollRef as RefObject<HTMLDivElement>} onScroll={handleMessagesScroll}>
          {loading ? (
            <div className="copilot-empty">
              <LoaderCircle size={16} className="spin" />
              Loading messages
            </div>
          ) : sessionMessages.length === 0 ? (
            null
          ) : (
            <>
              {sessionMessages.length > visibleMessageCount ? (
                <button
                  type="button"
                  className="copilot-load-earlier"
                  onClick={() => setVisibleMessageCount((prev) => prev + MESSAGE_WINDOW)}
                >
                  Load earlier messages ({sessionMessages.length - visibleMessageCount} more)
                </button>
              ) : null}
              {visibleSessionMessages.map((message) => (
                <CopilotMessageItem
                  key={message.id}
                  message={message}
                  disabled={sending || Boolean(applyingActionKey || bulkAction)}
                  onAnswerQuestion={answerQuestion}
                  onLoadTrace={loadMessageTrace}
                />
              ))}
            </>
          )}
          <CopilotStreamingBubble
            sending={sending}
            streamStartedAt={streamStartedAt}
            steeredTurnTexts={steeredTurnTexts}
            liveTrace={liveTrace}
          />
        </div>

        {pendingActions.length > 0 ? (
          <CopilotPlanActionCard
            action={pendingActions[0]}
            isApplying={applyingActionKey === planActionKey(pendingActions[0])}
            blocked={sending || bulkAction !== null}
            bulkAction={bulkAction as 'apply' | 'cancel' | null}
            onApply={applyAction}
            onCancel={cancelPendingActions}
          />
        ) : null}

        {children}
      </div>
    </div>
  );
}
