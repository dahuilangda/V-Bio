/**
 * Clipboard + command-history hook — extracted from ApiAccessPage.
 * Owns: copiedActionId highlight state, commandHistory (localStorage-backed),
 * rememberCommandHistory, copyText (navigator.clipboard with fallback).
 * Page-level error/success toasts stay injected via callbacks.
 */
import { useCallback, useRef, useState } from 'react';
import { COMMAND_HISTORY_LIMIT, COMMAND_HISTORY_STORAGE_KEY, fallbackCopyText, readCommandHistoryFromStorage, type CommandHistoryEntry } from './apiAccessHelpers';



interface UseCommandClipboardOptions {
  onError?: (message: string) => void;
  onSuccess?: (message: string) => void;
  /** Enriches history entries with page context (workflow/backend/project/token). */
  buildEntryContext?: () => Partial<CommandHistoryEntry>;
}

export function useCommandClipboard(options: UseCommandClipboardOptions = {}) {
  const [copiedActionId, setCopiedActionId] = useState('');
  const [commandHistory, setCommandHistory] = useState<CommandHistoryEntry[]>(readCommandHistoryFromStorage);
  const copiedResetTimerRef = useRef<number | null>(null);

  const rememberCommandHistory = useCallback((label: string, command: string) => {
    setCommandHistory((prev) => {
      const ctx = options.buildEntryContext?.() ?? ({} as Partial<CommandHistoryEntry>);
      const entry: CommandHistoryEntry = {
        id: typeof globalThis.crypto?.randomUUID === 'function' ? globalThis.crypto.randomUUID() : `hist_${Date.now()}`,
        createdAt: new Date().toISOString(),
        label,
        command,
        workflow: 'prediction',
        backend: '',
        projectId: '',
        projectName: '',
        tokenId: '',
        tokenName: '',
        ...ctx,
      };
      const next = [entry, ...prev.filter((item) => !(item.label === label && item.command === command))].slice(0, COMMAND_HISTORY_LIMIT);
      try {
        window.localStorage.setItem(COMMAND_HISTORY_STORAGE_KEY, JSON.stringify(next));
      } catch { /* quota errors are non-fatal */ }
      return next;
    });
  }, []);

  const copyText = useCallback(async (text: string, okMessage: string, historyLabel?: string, copyId?: string) => {
    let copied = false;
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch {
      copied = false;
    }
    if (!copied) copied = fallbackCopyText(text);
    if (!copied) {
      options.onError?.('Copy failed. Clipboard permission may be blocked in this context.');
      return;
    }
    if (historyLabel) rememberCommandHistory(historyLabel, text);
    if (copyId) {
      setCopiedActionId(copyId);
      if (copiedResetTimerRef.current !== null) window.clearTimeout(copiedResetTimerRef.current);
      copiedResetTimerRef.current = window.setTimeout(() => {
        setCopiedActionId((prev) => (prev === copyId ? '' : prev));
      }, 1200);
    }
    options.onSuccess?.(okMessage);
  }, [options.onError, options.onSuccess, rememberCommandHistory]);

  const clearHistory = useCallback(() => {
    setCommandHistory([]);
    try { window.localStorage.removeItem(COMMAND_HISTORY_STORAGE_KEY); } catch { /* ignore */ }
  }, []);

  return { copiedActionId, commandHistory, copyText, rememberCommandHistory, clearHistory, setCommandHistory };
}
