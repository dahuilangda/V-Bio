import { Check, Eye, LoaderCircle, PencilLine, Search, Share2, Trash2, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  deleteProjectShare,
  deleteProjectTaskShare,
  findUserByUsername,
  listProjectShares,
  listProjectTaskShares,
  searchUsersForSharing,
  updateProjectShareAccessLevel,
  updateProjectTaskShareAccessLevel,
  upsertProjectShare,
  upsertProjectTaskShare
} from '../../api/supabaseLite';
import type { AppUser, ProjectShareRecord, ProjectTaskShareRecord, ShareAccessLevel } from '../../types/models';

interface SharingModalProps {
  open: boolean;
  mode: 'project' | 'task';
  projectId: string;
  projectName: string;
  projectTaskId?: string;
  taskLabel?: string;
  currentUserId: string;
  onClose: () => void;
}

export function SharingModal({
  open,
  mode,
  projectId,
  projectName,
  projectTaskId = '',
  taskLabel = '',
  currentUserId,
  onClose
}: SharingModalProps) {
  const [username, setUsername] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [projectShares, setProjectShares] = useState<ProjectShareRecord[]>([]);
  const [taskShares, setTaskShares] = useState<ProjectTaskShareRecord[]>([]);
  const [suggestions, setSuggestions] = useState<AppUser[]>([]);
  const [suggestionsLoading, setSuggestionsLoading] = useState(false);
  const [selectedUser, setSelectedUser] = useState<AppUser | null>(null);
  const [shareAccessLevel, setShareAccessLevel] = useState<ShareAccessLevel>('viewer');
  const [shareListQuery, setShareListQuery] = useState('');

  const title = mode === 'task' ? 'Share task' : 'Share project';
  const subtitle = useMemo(() => {
    if (mode === 'task') {
      return `Access for ${taskLabel || projectTaskId.slice(0, 8)}`;
    }
    return `Access for ${projectName}`;
  }, [mode, projectName, projectTaskId, taskLabel]);

  const loadShares = useCallback(async () => {
    if (!open) return;
    setLoading(true);
    setError(null);
    try {
      if (mode === 'task') {
        setTaskShares(await listProjectTaskShares(projectTaskId));
        setProjectShares([]);
      } else {
        setProjectShares(await listProjectShares(projectId));
        setTaskShares([]);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load share records.');
    } finally {
      setLoading(false);
    }
  }, [mode, open, projectId, projectTaskId]);

  useEffect(() => {
    if (!open) return;
    void loadShares();
  }, [loadShares, open]);

  useEffect(() => {
    if (open) return;
    setUsername('');
    setError(null);
    setLoading(false);
    setSubmitting(false);
    setProjectShares([]);
    setTaskShares([]);
    setSuggestions([]);
    setSuggestionsLoading(false);
    setSelectedUser(null);
    setShareAccessLevel('viewer');
    setShareListQuery('');
  }, [open]);

  const sharedUserIds = useMemo(
    () =>
      new Set(
        (mode === 'task' ? taskShares : projectShares)
          .map((item) => String(item.user_id || '').trim())
          .filter(Boolean)
      ),
    [mode, projectShares, taskShares]
  );

  useEffect(() => {
    if (!open) return;
    const normalized = username.trim().toLowerCase();
    if (selectedUser && normalized === String(selectedUser.username || '').trim().toLowerCase()) {
      return;
    }
    setSelectedUser(null);
  }, [open, selectedUser, username]);

  useEffect(() => {
    if (!open) return;
    const normalized = username.trim().toLowerCase();
    if (normalized.length < 2) {
      setSuggestions([]);
      setSuggestionsLoading(false);
      return;
    }

    let cancelled = false;
    const timer = window.setTimeout(() => {
      setSuggestionsLoading(true);
      void searchUsersForSharing(normalized, {
        excludeUserId: currentUserId,
        limit: 6
      })
        .then((rows) => {
          if (cancelled) return;
          setSuggestions(
            rows.filter((row) => !sharedUserIds.has(String(row.id || '').trim()))
          );
        })
        .catch(() => {
          if (cancelled) return;
          setSuggestions([]);
        })
        .finally(() => {
          if (cancelled) return;
          setSuggestionsLoading(false);
        });
    }, 180);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [currentUserId, open, sharedUserIds, username]);

  const handleGrant = async () => {
    const normalizedUsername = username.trim().toLowerCase();
    if (!normalizedUsername && !selectedUser) {
      setError('Enter the target username.');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const user =
        selectedUser && String(selectedUser.username || '').trim().toLowerCase() === normalizedUsername
          ? selectedUser
          : await findUserByUsername(normalizedUsername);
      if (!user || user.deleted_at) {
        throw new Error(`User "${normalizedUsername}" was not found.`);
      }
      if (user.id === currentUserId) {
        throw new Error('You already have access.');
      }
      if (sharedUserIds.has(String(user.id || '').trim())) {
        throw new Error(`User "${String(user.username || normalizedUsername).trim()}" already has access.`);
      }
      if (mode === 'task') {
        await upsertProjectTaskShare({
          projectId,
          projectTaskId,
          userId: user.id,
          grantedByUserId: currentUserId,
          accessLevel: shareAccessLevel
        });
      } else {
        await upsertProjectShare({
          projectId,
          userId: user.id,
          grantedByUserId: currentUserId,
          accessLevel: shareAccessLevel
        });
      }
      setUsername('');
      setSelectedUser(null);
      setSuggestions([]);
      setShareAccessLevel('viewer');
      await loadShares();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to grant share access.');
    } finally {
      setSubmitting(false);
    }
  };

  const handleAccessLevelChange = async (shareId: string, accessLevel: ShareAccessLevel) => {
    const currentItem = (mode === 'task' ? taskShares : projectShares).find((item) => item.id === shareId);
    if (currentItem && currentItem.access_level === accessLevel) return;
    setSubmitting(true);
    setError(null);
    try {
      if (mode === 'task') {
        await updateProjectTaskShareAccessLevel(shareId, accessLevel);
      } else {
        await updateProjectShareAccessLevel(shareId, accessLevel);
      }
      await loadShares();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update share access.');
    } finally {
      setSubmitting(false);
    }
  };

  const handleRevoke = async (shareId: string) => {
    setSubmitting(true);
    setError(null);
    try {
      if (mode === 'task') {
        await deleteProjectTaskShare(shareId);
      } else {
        await deleteProjectShare(shareId);
      }
      await loadShares();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke share access.');
    } finally {
      setSubmitting(false);
    }
  };

  if (!open) return null;

  const items = mode === 'task' ? taskShares : projectShares;
  const filteredItems = useMemo(() => {
    const query = shareListQuery.trim().toLowerCase();
    if (!query) return items;
    return items.filter((item) => {
      const targetName = String(item.target_name || '').trim().toLowerCase();
      const targetUsername = String(item.target_username || '').trim().toLowerCase();
      return targetName.includes(query) || targetUsername.includes(query);
    });
  }, [items, shareListQuery]);
  const selectedUsername = String(selectedUser?.username || '').trim().toLowerCase();
  const accessOptions: Array<{ value: ShareAccessLevel; label: string; icon: typeof Eye }> = [
    { value: 'viewer', label: 'Viewer', icon: Eye },
    { value: 'editor', label: 'Editor', icon: PencilLine }
  ];

  return (
    <div className="modal-mask share-modal-mask" onClick={onClose}>
      <div className="modal share-modal" onClick={(event) => event.stopPropagation()}>
        <div className="share-modal-head">
          <div className="share-modal-heading">
            <h2>{title}</h2>
            <p className="muted small share-modal-target">{subtitle}</p>
          </div>
          <div className="share-modal-head-actions">
            <button
              type="button"
              className="btn btn-primary btn-compact share-submit-btn"
              onClick={() => void handleGrant()}
              disabled={submitting || (!username.trim() && !selectedUser)}
              aria-label="Share with selected user"
              title="Share"
            >
              {submitting ? <LoaderCircle size={15} className="spin" /> : <Share2 size={15} />}
              <span>Share</span>
            </button>
            <button className="icon-btn" type="button" onClick={onClose} aria-label="Close share dialog">
              <X size={16} />
            </button>
          </div>
        </div>

        <div className="share-modal-create-card">
          <div className="share-modal-create">
            <div className="field share-modal-search-field">
              <label>Username</label>
              <div className="input-wrap search-input share-modal-search">
                <Search size={15} />
                <input
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  placeholder="Search username"
                  disabled={submitting}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault();
                      void handleGrant();
                    }
                  }}
                />
                {suggestionsLoading ? <LoaderCircle size={14} className="spin share-modal-search-spinner" /> : null}
              </div>
              {selectedUser ? (
                <div className="share-modal-selected muted small">
                  <Check size={12} />
                  Selected @{selectedUser.username}
                </div>
              ) : null}
            </div>
            <div className="field share-modal-role-field">
              <label>Access</label>
              <div className="share-role-toggle" role="radiogroup" aria-label="Share access level">
                {accessOptions.map((option) => {
                  const Icon = option.icon;
                  const active = shareAccessLevel === option.value;
                  return (
                    <button
                      key={option.value}
                      type="button"
                      className={`share-role-option ${active ? 'is-active' : ''}`}
                      aria-pressed={active}
                      onClick={() => setShareAccessLevel(option.value)}
                      disabled={submitting}
                      title={option.label}
                    >
                      <span className="share-role-option-circle">
                        <Icon size={15} />
                      </span>
                      <span className="share-role-option-label">{option.label}</span>
                    </button>
                  );
                })}
              </div>
            </div>
          </div>

          {username.trim().length >= 2 ? (
            <div className="share-modal-suggestions">
              {suggestions.length === 0 && !suggestionsLoading ? (
                <div className="share-modal-suggestion-empty muted small">No matching usernames.</div>
              ) : (
                suggestions.map((user) => {
                  const isSelected = selectedUsername === String(user.username || '').trim().toLowerCase();
                  return (
                    <button
                      key={user.id}
                      type="button"
                      className={`share-modal-suggestion ${isSelected ? 'is-selected' : ''}`}
                      onClick={() => {
                        setSelectedUser(user);
                        setUsername(String(user.username || '').trim());
                        setError(null);
                      }}
                      disabled={submitting}
                    >
                      <span className="share-modal-suggestion-main">
                        <strong>{String(user.name || '').trim() || String(user.username || '').trim()}</strong>
                        <code>@{String(user.username || '').trim()}</code>
                      </span>
                      {isSelected ? <Check size={14} /> : null}
                    </button>
                  );
                })
              )}
            </div>
          ) : null}
        </div>

        {error ? <div className="alert error">{error}</div> : null}

        <div className="share-modal-list">
          <div className="share-modal-list-scroll">
            <div className="share-modal-list-head">
              <div className="share-modal-list-head-main">
                <span>Shared users</span>
                <span className="share-count-pill share-modal-list-count">{filteredItems.length}</span>
              </div>
              <div className="input-wrap search-input share-modal-list-filter">
                <Search size={14} />
                <input
                  value={shareListQuery}
                  onChange={(event) => setShareListQuery(event.target.value)}
                  placeholder="Filter users"
                  disabled={submitting || items.length === 0}
                />
              </div>
            </div>
            {loading ? (
              <div className="muted share-modal-empty">Loading shares...</div>
            ) : items.length === 0 ? (
              <div className="muted share-modal-empty">No shared users yet.</div>
            ) : filteredItems.length === 0 ? (
              <div className="muted share-modal-empty">No users match this filter.</div>
            ) : (
              filteredItems.map((item) => {
                const targetName = String(item.target_name || '').trim();
                const targetUsername = String(item.target_username || '').trim();
                const grantedBy = String(item.granted_by_username || item.granted_by_name || '').trim();
                return (
                  <article key={item.id} className="share-modal-item">
                    <div className="share-modal-main">
                      <strong>{targetName || targetUsername || item.user_id}</strong>
                      {targetUsername ? <code>@{targetUsername}</code> : null}
                      {grantedBy ? <span className="muted small">Granted by {grantedBy}</span> : null}
                    </div>
                    <div className="share-modal-item-actions">
                      <div className="share-role-toggle share-role-toggle-compact" role="radiogroup" aria-label="Update share access level">
                        {accessOptions.map((option) => {
                          const Icon = option.icon;
                          const active = item.access_level === option.value;
                          return (
                            <button
                              key={`${item.id}:${option.value}`}
                              type="button"
                              className={`share-role-option share-role-option-compact ${active ? 'is-active' : ''}`}
                              aria-pressed={active}
                              onClick={() => void handleAccessLevelChange(item.id, option.value)}
                              disabled={submitting}
                              title={option.label}
                            >
                              <span className="share-role-option-circle">
                                <Icon size={14} />
                              </span>
                            </button>
                          );
                        })}
                      </div>
                      <button
                        type="button"
                        className="share-item-revoke-btn"
                        onClick={() => void handleRevoke(item.id)}
                        disabled={submitting}
                        aria-label="Revoke share"
                        title="Revoke share"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  </article>
                );
              })
            )}
          </div>
        </div>

        <div className="share-modal-foot muted small">
          <Share2 size={14} />
          {mode === 'task'
            ? 'Task viewers can open the shared task. Task editors can update that task entry without managing the whole project.'
            : 'Project viewers can browse the project. Project editors can collaborate on project content and task runs.'}
        </div>
      </div>
    </div>
  );
}
