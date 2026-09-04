import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { listProjects } from '../../api/supabaseLite';
import { useAuth } from '../../hooks/useAuth';
import {
  filterCommands,
  groupCommands,
  isPaletteToggleEvent,
  nextPaletteIndex,
  type PaletteCommand,
} from '../../utils/commandRegistry';

// Global ⌘K command palette. Built on the absorbed-cmdk model (fuzzy command-score filter,
// value-based selection, the full ARIA combobox pattern, loop/Home/End/Page navigation)
// WITHOUT the cmdk dependency: this palette is an app-level overlay, not an embeddable
// combobox primitive, and every dependency must earn its place.
//
// Commands are REAL host actions (navigation over live project data, host dialogs) —
// nothing placeholder. Recent projects load lazily on first open and cache for the
// session's tab lifetime.

const RECENT_CACHE_KEY = 'vbio:palette:recent-projects:v1';
const RECENT_CACHE_TTL_MS = 5 * 60 * 1000;

interface CachedRecent {
  at: number;
  projects: Array<{ id: string; name: string; task_type: string }>;
}

function readCachedRecent(): CachedRecent | null {
  try {
    const raw = window.sessionStorage.getItem(RECENT_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedRecent;
    if (!parsed || typeof parsed.at !== 'number' || !Array.isArray(parsed.projects)) return null;
    if (Date.now() - parsed.at > RECENT_CACHE_TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeCachedRecent(projects: Array<{ id: string; name: string; task_type: string }>): void {
  try {
    window.sessionStorage.setItem(RECENT_CACHE_KEY, JSON.stringify({ at: Date.now(), projects } satisfies CachedRecent));
  } catch {
    // quota errors lose only the cache; the palette still lists static commands
  }
}

export function CommandPalette() {
  const navigate = useNavigate();
  const { session } = useAuth();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState('');
  const [recent, setRecent] = useState<Array<{ id: string; name: string; task_type: string }>>(
    () => (typeof window === 'undefined' ? [] : readCachedRecent()?.projects ?? [])
  );
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Global toggle: ⌘K / Ctrl+K anywhere (the palette convention — also from inputs). The
  // Escape key closes only while open; IME composition never triggers the toggle.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if (isPaletteToggleEvent(event)) {
        event.preventDefault();
        setOpen((prev) => !prev);
        return;
      }
      if (event.key === 'Escape' && open) {
        event.preventDefault();
        setOpen(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open]);

  // Reset per-open state: fresh query, first command active.
  useEffect(() => {
    if (!open) return;
    setQuery('');
    window.requestAnimationFrame(() => inputRef.current?.focus({ preventScroll: true }));
  }, [open]);

  // Lazy recent-projects hydration on first open (session-tab cache, 5-minute TTL — the
  // list is a navigation aid, not authoritative data).
  useEffect(() => {
    if (!open || recent.length > 0 || !session?.userId) return;
    let cancelled = false;
    void listProjects({ userId: session.userId })
      .then((projects) => {
        if (cancelled) return;
        const top = [...projects]
          .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')))
          .slice(0, 5)
          .map((project) => ({ id: project.id, name: project.name, task_type: project.task_type }));
        setRecent(top);
        writeCachedRecent(top);
      })
      .catch(() => {
        // A failed listing only means the palette shows static commands this open.
      });
    return () => {
      cancelled = true;
    };
  }, [open, recent.length, session?.userId]);

  const runCommand = useCallback(
    (command: PaletteCommand) => {
      setOpen(false);
      command.run();
    },
    []
  );

  const commands = useMemo<PaletteCommand[]>(() => {
    const statics: PaletteCommand[] = [
      {
        id: 'nav:projects',
        label: 'Projects',
        hint: 'projects home',
        group: 'Navigation',
        order: 10,
        run: () => navigate('/projects'),
      },
      {
        id: 'nav:shares',
        label: 'Sharing & tokens',
        hint: 'shares',
        group: 'Navigation',
        order: 20,
        run: () => navigate('/shares'),
      },
      {
        id: 'action:new-project',
        label: 'New project',
        hint: 'create prediction docking screening project',
        group: 'Actions',
        order: 30,
        run: () => {
          try {
            window.sessionStorage.setItem('vbio:palette:open-create', '1');
          } catch {
            // The dialog simply won't pre-open; navigation still lands on the list.
          }
          navigate('/projects');
        },
      },
      {
        id: 'action:open-copilot',
        label: 'Open Copilot assistant',
        hint: 'copilot assistant panel',
        group: 'Actions',
        order: 40,
        run: () => {
          window.dispatchEvent(new CustomEvent('vbio:open-copilot'));
        },
      },
    ];
    const recents: PaletteCommand[] = recent.map((project, index) => ({
      id: `recent:${project.id}`,
      label: project.name,
      hint: `Recent project ${project.task_type}`,
      group: 'Recent projects',
      order: 100 + index,
      run: () => navigate(`/projects/${project.id}`),
    }));
    return [...statics, ...recents];
  }, [navigate, recent]);

  const filtered = useMemo(() => filterCommands(commands, query), [commands, query]);
  const grouped = useMemo(() => groupCommands(filtered), [filtered]);
  const activeIndex = Math.max(
    0,
    filtered.findIndex((command) => command.id === activeId)
  );
  const activeCommand = filtered[activeIndex];

  // Keep the active item visible while navigating with keys (cmdk scrolls the selected
  // option into view).
  useEffect(() => {
    if (!open || !activeCommand) return;
    listRef.current
      ?.querySelector(`[data-command-id="${CSS.escape(activeCommand.id)}"]`)
      ?.scrollIntoView({ block: 'nearest' });
  }, [open, activeCommand]);

  if (!open) {
    return null;
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    const count = filtered.length;
    switch (event.key) {
      case 'ArrowDown':
      case 'ArrowUp':
      case 'Home':
      case 'End':
      case 'PageDown':
      case 'PageUp': {
        if (count === 0) return;
        event.preventDefault();
        const next = nextPaletteIndex(count, activeIndex, event.key as Parameters<typeof nextPaletteIndex>[2]);
        setActiveId(filtered[next].id);
        return;
      }
      case 'Enter': {
        if (activeCommand) {
          event.preventDefault();
          runCommand(activeCommand);
        }
        return;
      }
      default:
        return;
    }
  };

  let flatIndex = -1;

  return (
    <div
      className="palette-overlay"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) setOpen(false);
      }}
    >
      <div className="palette-dialog" role="dialog" aria-modal="true" aria-label="Command palette">
        <input
          ref={inputRef}
          className="palette-input"
          type="text"
          value={query}
          placeholder="Search commands, projects…"
          role="combobox"
          aria-expanded
          aria-controls="palette-listbox"
          aria-autocomplete="list"
          aria-activedescendant={activeCommand ? `palette-option-${CSS.escape(activeCommand.id)}` : undefined}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="palette-list" id="palette-listbox" role="listbox" aria-label="Commands" ref={listRef}>
          {grouped.length === 0 ? (
            <div className="palette-empty">No matching commands</div>
          ) : (
            grouped.map((bucket) => (
              <div key={bucket.group} className="palette-group" role="group" aria-labelledby={undefined}>
                <div className="palette-group-heading" aria-hidden>
                  {bucket.group}
                </div>
                {bucket.commands.map((command) => {
                  flatIndex += 1;
                  const active = command.id === activeCommand?.id;
                  return (
                    <button
                      key={command.id}
                      id={`palette-option-${CSS.escape(command.id)}`}
                      type="button"
                      role="option"
                      aria-selected={active}
                      data-command-id={CSS.escape(command.id)}
                      className={`palette-option${active ? ' active' : ''}`}
                      onMouseEnter={() => setActiveId(command.id)}
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => runCommand(command)}
                    >
                      <span className="palette-option-label">{command.label}</span>
                      {command.hint ? <span className="palette-option-hint">{command.hint}</span> : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
        <div className="palette-footer" aria-hidden>
          <span>↑↓ Navigate</span>
          <span>↵ Run</span>
          <span>esc Close</span>
        </div>
      </div>
    </div>
  );
}
