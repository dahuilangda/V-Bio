/**
 * Composer keyboard state machine — extracted from ProjectCopilotModal's
 * 122-line onKeyDown. Three independent navigation layers in priority order:
 *   1. @-mention menu (ArrowUp/Down/Home/End/PageUp/Down, Enter/Tab insert, Esc dismiss)
 *   2. inline-completion ghost + top-10 picker (Tab accept, ↓ open picker, Esc close)
 *   3. ↑/↓ input-history recall (caret on first/last line)
 * Enter (no shift, not composing) falls through to send.
 */
import { type KeyboardEvent as ReactKeyboardEvent } from 'react';

interface MentionStateLike {
  options: Array<{ id: string; name: string }>;
}

interface UseCopilotKeymapParams {
  // mention layer
  mentionState: MentionStateLike | null;
  mentionActiveIndex: number;
  setMentionActiveIndex: React.Dispatch<React.SetStateAction<number>>;
  insertMention: (option: { id: string; name: string }) => void;
  dismissMention: () => void;
  // completion layer
  completion: string;
  completions: string[];
  completionPickerIndex: number | null;
  setCompletionPickerIndex: React.Dispatch<React.SetStateAction<number | null>>;
  setCompletions: React.Dispatch<React.SetStateAction<string[]>>;
  acceptCompletion: (value: string) => void;
  // history layer
  shouldNavigateHistory: (dir: 'up' | 'down', draft: string, caret: number) => boolean;
  nextHistory: (dir: 'up' | 'down') => { value: string } | null;
  applyHistoryValue: (value: string) => void;
  // send
  draft: string;
  sendMessage: () => void | Promise<void>;
}

export function useCopilotKeymap(p: UseCopilotKeymapParams) {
  return (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    // ── Layer 1: @-mention menu ──
    if (p.mentionState) {
      const count = p.mentionState.options.length;
      switch (event.key) {
        case 'ArrowDown':
          event.preventDefault();
          p.setMentionActiveIndex((i) => (i + 1) % count);
          return;
        case 'ArrowUp':
          event.preventDefault();
          p.setMentionActiveIndex((i) => (i - 1 + count) % count);
          return;
        case 'Home':
          event.preventDefault();
          p.setMentionActiveIndex(0);
          return;
        case 'End':
          event.preventDefault();
          p.setMentionActiveIndex(count - 1);
          return;
        case 'PageDown':
          event.preventDefault();
          p.setMentionActiveIndex((i) => Math.min(count - 1, i + 6));
          return;
        case 'PageUp':
          event.preventDefault();
          p.setMentionActiveIndex((i) => Math.max(0, i - 6));
          return;
        case 'Enter':
        case 'Tab':
          event.preventDefault();
          p.insertMention(
            p.mentionState.options[p.mentionActiveIndex] ?? p.mentionState.options[0],
          );
          return;
        case 'Escape':
          event.preventDefault();
          p.dismissMention();
          return;
      }
    }

    // ── Layer 2: inline completion (ghost + picker) ──
    if (!event.nativeEvent.isComposing && (p.completion || p.completions.length > 0)) {
      if (p.completionPickerIndex !== null) {
        // Picker open: ↑/↓ move (loop), Enter/Tab accept, Esc closes ONLY the picker.
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          p.setCompletionPickerIndex((i) => ((i ?? 0) + 1) % p.completions.length);
          return;
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault();
          p.setCompletionPickerIndex((i) => ((i ?? 0) - 1 + p.completions.length) % p.completions.length);
          return;
        }
        if (event.key === 'Enter' || event.key === 'Tab') {
          event.preventDefault();
          p.acceptCompletion(p.completions[p.completionPickerIndex] ?? p.completion);
          return;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          p.setCompletionPickerIndex(null);
          return;
        }
      } else if (p.completion) {
        if (event.key === 'Tab') {
          event.preventDefault();
          p.acceptCompletion(p.completion);
          return;
        }
        if (event.key === 'ArrowDown') {
          // Ghost showing: ↓ opens the ranked candidates (fish-shell style);
          // ↑ stays with input-history recall.
          event.preventDefault();
          p.setCompletionPickerIndex(0);
          return;
        }
        if (event.key === 'Escape') {
          event.preventDefault();
          p.setCompletions([]);
          return;
        }
      }
    }

    // ── Layer 3: ↑/↓ input history (caret on first/last line) ──
    if (
      !event.nativeEvent.isComposing &&
      (event.key === 'ArrowUp' || event.key === 'ArrowDown') &&
      p.shouldNavigateHistory(
        event.key === 'ArrowUp' ? 'up' : 'down',
        p.draft,
        event.currentTarget.selectionStart ?? p.draft.length,
      )
    ) {
      const result = p.nextHistory(event.key === 'ArrowUp' ? 'up' : 'down');
      if (result) {
        event.preventDefault();
        p.applyHistoryValue(result.value);
      }
      return;
    }

    // ── Send ──
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      void p.sendMessage();
    }
  };
}
