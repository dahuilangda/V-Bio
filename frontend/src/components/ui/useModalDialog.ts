import { useCallback, useEffect, useRef } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, RefCallback } from 'react';

/**
 * WAI-ARIA Authoring Practices Guide "Dialog (Modal)" behaviour for the app's
 * modal-mask dialogs (no visual change — behaviour and ARIA only):
 *
 * - Escape closes the dialog (IME composition never triggers it);
 * - opening moves focus into the dialog (first focusable element, else the
 *   container itself);
 * - Tab / Shift+Tab cycle within the dialog (focus containment);
 * - closing restores focus to the element that opened it.
 *
 * Usage: spread the returned props onto the dialog card element (the inner
 * `.modal` div, NOT the mask) and pass an aria-label separately:
 *
 *   const dialogProps = useModalDialog(open, onClose);
 *   <div className="modal-mask" onClick={onClose}>
 *     <div className="modal" {...dialogProps} aria-label="Title">...</div>
 *   </div>
 */
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])'
].join(',');

function collectFocusable(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.offsetParent !== null || element === document.activeElement
  );
}

export interface ModalDialogProps {
  ref: RefCallback<HTMLDivElement>;
  role: 'dialog';
  'aria-modal': true;
  tabIndex: number;
  onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
}

export function useModalDialog(open: boolean, onClose: () => void): ModalDialogProps {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // Callback ref (stable identity): React 18's LegacyRef accepts it for spread.
  const setDialogNode = useCallback((node: HTMLDivElement | null) => {
    dialogRef.current = node;
  }, []);

  useEffect(() => {
    if (!open) return;
    const previouslyFocused = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const dialog = dialogRef.current;
    if (dialog) {
      const firstFocusable = collectFocusable(dialog)[0];
      (firstFocusable || dialog).focus();
    }
    return () => {
      if (previouslyFocused && document.body.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    };
  }, [open]);

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== 'Tab') return;
    const dialog = dialogRef.current;
    if (!dialog) return;
    const focusable = collectFocusable(dialog);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || !dialog.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !dialog.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  };

  return {
    ref: setDialogNode,
    role: 'dialog',
    'aria-modal': true,
    tabIndex: -1,
    onKeyDown
  };
}
