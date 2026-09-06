import { useCallback, useRef } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, RefCallback } from 'react';

/**
 * WAI-ARIA Authoring Practices Guide "Tabs" keyboard behaviour for the app's
 * role="tablist" switches (behaviour only — no visual change):
 *
 * - ArrowRight/ArrowDown selects the next tab, ArrowLeft/ArrowUp the previous
 *   (wrapping), Home/End the first/last tab (automatic activation);
 * - focus follows selection; disabled tabs are skipped exactly like clicks;
 * - roving tabindex: only the selected tab is tab stop.
 *
 * Usage:
 *   const tabs = useTabsKeyboard(value, setValue, ['a', 'b'] as const);
 *   <div role="tablist" {...tabs.props} aria-label="...">
 *     <button role="tab" aria-selected={...} tabIndex={tabs.tabTabIndex(selected)} ...>
 */
export interface TabsKeyboard {
  props: {
    ref: RefCallback<HTMLDivElement>;
    onKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
  };
  tabTabIndex: (selected: boolean) => number;
}

export function useTabsKeyboard<T extends string>(
  value: T,
  setValue: (next: T) => void,
  order: readonly T[]
): TabsKeyboard {
  const listRef = useRef<HTMLDivElement | null>(null);
  // Callback ref (stable identity): React 18's LegacyRef accepts it for spread.
  const setListNode = useCallback((node: HTMLDivElement | null) => {
    listRef.current = node;
  }, []);

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.nativeEvent.isComposing) return;
    const count = order.length;
    if (count === 0) return;
    const current = Math.max(0, order.indexOf(value));
    let next: number | null = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % count;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + count) % count;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = count - 1;
    if (next === null) return;
    event.preventDefault();
    const tab = listRef.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next];
    // A disabled tab cannot be clicked either — keyboard selection must match.
    if (!tab || tab.disabled) return;
    setValue(order[next]);
    tab.focus();
  };

  return {
    props: { ref: setListNode, onKeyDown },
    tabTabIndex: (selected: boolean) => (selected ? 0 : -1)
  };
}
