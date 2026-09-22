/**
 * The open-state panel shell of the copilot: floating/draggable panel frame,
 * header, error banner, history sidebar, settings panel, messages list and
 * plan-action card. Pure presentation; every value arrives via named prop
 * groups. The composer element is built by the modal and passed as `children`.
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
  isSuppressedByOverlay: boolean;
  isMobileViewport: boolean;
  visualViewportHeight: number | null;
  visualViewportTop: number;
  position: { x: number; y: number } | null;
  panelSize: { width: number; height: number } | null;
  onDragStart: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onDragMove: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onDragEnd: (event: ReactPointerEvent<HTMLDivElement>) => void;
}

export interface CopilotPanelShellHeaderProps {
  title: string;
  subtitle: string;
  authSession: Session | null;
  onClose: () => void;
  onOpenSettings: () => void;
}

export interface CopilotPanelShellHistoryProps {
  isHistoryOpen: boolean;
  setHistoryOpen: Dispatch<SetStateAction<boolean>>;
  chatSessions: CopilotSessionSummary[];
  activeSessionId: string;
  onSelectSession: (sessionId: string) => void;
  deleteSessionAction: (sessionId: string) => Promise<void>;
  onStartNewChat: () => void;
}

export interface CopilotPanelShellSettingsProps {
  isSettingsOpen: boolean;
  setSettingsOpen: Dispatch<SetStateAction<boolean>>;
  settingsForm: SettingsFormValues;
  setSettingsForm: Dispatch<SetStateAction<SettingsFormValues>>;
  settingsError: string;
  settingsHasKey: boolean;
  settingsMaskedKey: string;
  isSettingsSaved: boolean;
  isSettingsSaving: boolean;
  settingsTestResult: CopilotTestResult | null;
  isSettingsTesting: boolean;
  saveSettingsAction: () => Promise<void>;
  testSettingsAction: () => Promise<void>;
}

export interface CopilotPanelShellMessagesProps {
  scrollRef: RefObject<HTMLDivElement | null>;
  onMessagesScroll: () => void;
  isLoading: boolean;
  sessionMessages: ProjectCopilotMessage[];
  visibleMessageCount: number;
  setVisibleMessageCount: Dispatch<SetStateAction<number>>;
  MESSAGE_WINDOW: number;
  visibleSessionMessages: ProjectCopilotMessage[];
  onAnswerQuestion: (answer: string) => void;
  onLoadTrace: (messageId: string) => void;
  streamStartedAt: string;
  steeredTurnTexts: string[];
  liveTrace: CopilotTraceStep[];
}

export interface CopilotPanelShellTurnProps {
  isSending: boolean;
  applyingActionKey: string | null;
  bulkAction: 'apply' | 'cancel' | null;
}

export interface CopilotPanelShellPlanProps {
  pendingActions: CopilotPlanAction[];
  applyAction: (action: CopilotPlanAction) => Promise<boolean>;
  cancelAction: () => Promise<void>;
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
    isSuppressedByOverlay,
    isMobileViewport,
    visualViewportHeight,
    visualViewportTop,
    position,
    panelSize,
    onDragStart,
    onDragMove,
    onDragEnd
  } = panel;
  const {
    title,
    subtitle,
    authSession,
    onClose,
    onOpenSettings
  } = header;
  const {
    isHistoryOpen,
    setHistoryOpen,
    chatSessions,
    activeSessionId,
    onSelectSession,
    deleteSessionAction,
    onStartNewChat
  } = history;
  const {
    isSettingsOpen,
    setSettingsOpen,
    settingsForm,
    setSettingsForm,
    settingsError,
    settingsHasKey,
    settingsMaskedKey,
    isSettingsSaved,
    isSettingsSaving,
    settingsTestResult,
    isSettingsTesting,
    saveSettingsAction,
    testSettingsAction
  } = settings;
  const {
    scrollRef,
    onMessagesScroll,
    isLoading,
    sessionMessages,
    visibleMessageCount,
    setVisibleMessageCount,
    MESSAGE_WINDOW,
    visibleSessionMessages,
    onAnswerQuestion,
    onLoadTrace,
    streamStartedAt,
    steeredTurnTexts,
    liveTrace
  } = messages;
  const {
    isSending,
    applyingActionKey,
    bulkAction
  } = turn;
  const {
    pendingActions,
    applyAction,
    cancelAction
  } = plan;

  return (
    <div
      ref={panelRef as RefObject<HTMLDivElement>}
      className={`copilot-floating-panel${isSuppressedByOverlay ? ' copilot-suppressed' : ''}`}
      style={
        isSuppressedByOverlay
          ? { right: 24, bottom: 24 }
          : isMobileViewport
            ? // Bind to visualViewport while the soft keyboard is open so the composer
              // sits above it; when closed these reduce to the CSS full-screen rules.
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
      aria-hidden={isSuppressedByOverlay ? 'true' : undefined}
      aria-label={title}
    >
      <div className={`copilot-modal copilot-chat-window${isHistoryOpen ? ' history-open' : ''}`}>
        <CopilotHeader
          title={title}
          subtitle={subtitle}
          isAdmin={Boolean(authSession?.isAdmin)}
          isHistoryOpen={isHistoryOpen}
          onToggleHistory={() => setHistoryOpen((prev) => !prev)}
          onNewChat={onStartNewChat}
          onOpenSettings={onOpenSettings}
          onClose={onClose}
          onDragStart={onDragStart}
          onDragMove={onDragMove}
          onDragEnd={onDragEnd}
        />

        {error ? <div className="alert error copilot-error">{error}</div> : null}

        {isHistoryOpen ? (
          <CopilotHistorySidebar
            sessions={chatSessions}
            activeSessionId={activeSessionId}
            onSelect={onSelectSession}
            deleteAction={deleteSessionAction}
            onNewChat={onStartNewChat}
          />
        ) : null}

        {isSettingsOpen && (
          <CopilotSettingsPanel
            settingsForm={settingsForm}
            settingsError={settingsError}
            settingsHasKey={settingsHasKey}
            settingsMaskedKey={settingsMaskedKey}
            isSettingsSaved={isSettingsSaved}
            isSettingsSaving={isSettingsSaving}
            settingsTestResult={settingsTestResult}
            isSettingsTesting={isSettingsTesting}
            onFormChange={setSettingsForm}
            saveAction={saveSettingsAction}
            testAction={testSettingsAction}
            onClose={() => setSettingsOpen(false)}
          />
        )}

        <div className="copilot-messages" ref={scrollRef as RefObject<HTMLDivElement>} onScroll={onMessagesScroll}>
          {isLoading ? (
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
                  isDisabled={isSending || Boolean(applyingActionKey || bulkAction)}
                  onAnswerQuestion={onAnswerQuestion}
                  onLoadTrace={onLoadTrace}
                />
              ))}
            </>
          )}
          <CopilotStreamingBubble
            isSending={isSending}
            streamStartedAt={streamStartedAt}
            steeredTurnTexts={steeredTurnTexts}
            liveTrace={liveTrace}
          />
        </div>

        {pendingActions.length > 0 ? (
          <CopilotPlanActionCard
            action={pendingActions[0]}
            isApplying={applyingActionKey === planActionKey(pendingActions[0])}
            isBlocked={isSending || bulkAction !== null}
            bulkAction={bulkAction as 'apply' | 'cancel' | null}
            applyAction={applyAction}
            cancelAction={cancelAction}
          />
        ) : null}

        {children}
      </div>
    </div>
  );
}
