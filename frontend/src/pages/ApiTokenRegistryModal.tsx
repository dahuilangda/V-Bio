/**
 * The token registry modal of the API access page: create-token form
 * (name/project/expiry/permission chips + plain-text reveal) and the paged
 * token table with select/revoke/delete actions. Pure presentation — all
 * state and handlers stay in ApiAccessPage.
 */
import type { Dispatch, FormEvent, SetStateAction } from 'react';
import { Check, ChevronLeft, ChevronRight, Copy, LoaderCircle, Plus, Search, ShieldCheck, ShieldOff, Trash2, X } from 'lucide-react';
import { Field } from '../components/common/Field';
import type { ModalDialogProps } from '../components/ui/useModalDialog';
import type { ApiToken, Project } from '../types/models';
import './ApiTokenRegistryModal.css';

interface ApiTokenRegistryModalProps {
  registryScopeProject: Project | null;
  tokenRegistryDialogProps: ModalDialogProps;
  onClose: () => void;
  createTokenAction: (e: FormEvent<HTMLFormElement>) => Promise<void>;
  isTokenCreating: boolean;
  newTokenName: string;
  setNewTokenName: Dispatch<SetStateAction<string>>;
  newTokenExpiresDays: string;
  setNewTokenExpiresDays: Dispatch<SetStateAction<string>>;
  newTokenPlainText: string;
  setNewTokenPlainText: Dispatch<SetStateAction<string>>;
  isSubmitAllowed: boolean;
  setAllowSubmit: Dispatch<SetStateAction<boolean>>;
  isDeleteAllowed: boolean;
  setAllowDelete: Dispatch<SetStateAction<boolean>>;
  isCancelAllowed: boolean;
  setAllowCancel: Dispatch<SetStateAction<boolean>>;
  projects: Project[];
  isProjectLoading: boolean;
  selectedProjectId: string;
  setSelectedProjectId: Dispatch<SetStateAction<string>>;
  isRegistryProjectColumnVisible: boolean;
  isTokenLoading: boolean;
  tokenQuery: string;
  setTokenQuery: Dispatch<SetStateAction<string>>;
  tokenPage: number;
  setTokenPage: Dispatch<SetStateAction<number>>;
  tokenPageCount: number;
  pagedTokens: ApiToken[];
  selectedTokenId: string;
  setSelectedTokenId: Dispatch<SetStateAction<string>>;
  tokenRevokingId: string | null;
  tokenDeletingId: string | null;
  revokeTokenAction: (tokenId: string) => Promise<void>;
  removeTokenAction: (tokenId: string) => Promise<void>;
  copiedActionId: string | null;
  copyTextAction: (text: string, okMessage: string, historyLabel?: string, copyId?: string) => Promise<void>;
}

export function ApiTokenRegistryModal({
  registryScopeProject,
  tokenRegistryDialogProps,
  onClose,
  createTokenAction,
  isTokenCreating,
  newTokenName,
  setNewTokenName,
  newTokenExpiresDays,
  setNewTokenExpiresDays,
  newTokenPlainText,
  setNewTokenPlainText,
  isSubmitAllowed,
  setAllowSubmit,
  isDeleteAllowed,
  setAllowDelete,
  isCancelAllowed,
  setAllowCancel,
  projects,
  isProjectLoading,
  selectedProjectId,
  setSelectedProjectId,
  isRegistryProjectColumnVisible,
  isTokenLoading,
  tokenQuery,
  setTokenQuery,
  tokenPage,
  setTokenPage,
  tokenPageCount,
  pagedTokens,
  selectedTokenId,
  setSelectedTokenId,
  tokenRevokingId,
  tokenDeletingId,
  revokeTokenAction,
  removeTokenAction,
  copiedActionId,
  copyTextAction
}: ApiTokenRegistryModalProps) {
  return (
    <div className="modal-mask" onClick={onClose}>
      <div
        className="modal modal-wide api-token-modal"
        onClick={(e) => e.stopPropagation()}
        {...tokenRegistryDialogProps}
        aria-label="Token registry"
      >
        <div className="api-token-modal-head">
          <h2><ShieldCheck size={17} /> Token Registry{registryScopeProject ? ` · ${registryScopeProject.name}` : ''}</h2>
          <button
            className="icon-btn"
            type="button"
            aria-label="Close token registry"
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </div>

        <div className="api-token-modal-body">
          <section className="api-token-modal-create">
            <form className="api-token-create" onSubmit={createTokenAction}>
              <label className="field api-token-name-field">
                <span>Name</span>
                <input value={newTokenName} onChange={(e) => setNewTokenName(e.target.value)} placeholder="token-xxxxxxxx" required />
              </label>

              {!registryScopeProject && (
                <label className="field api-token-project-field">
                  <span>Project</span>
                  <select
                    value={selectedProjectId}
                    onChange={(e) => setSelectedProjectId(e.target.value)}
                    disabled={isProjectLoading || projects.length === 0}
                  >
                    {projects.length === 0 ? (
                      <option value="">No project</option>
                    ) : (
                      projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)
                    )}
                  </select>
                </label>
              )}

              <label className="field api-token-expiry-field">
                <span>Expire (d)</span>
                <input
                  type="number"
                  min={1}
                  value={newTokenExpiresDays}
                  onChange={(e) => setNewTokenExpiresDays(e.target.value)}
                  placeholder="Never"
                />
              </label>

              <div className="field api-token-source-wrap api-token-permissions-field">
                <span>Permissions</span>
                <div className="api-permission-grid">
                  <button
                    type="button"
                    className={`api-permission-chip ${isSubmitAllowed ? 'active' : ''}`}
                    onClick={() => setAllowSubmit((prev) => !prev)}
                    aria-pressed={isSubmitAllowed}
                  >
                    Submit
                  </button>
                  <button
                    type="button"
                    className={`api-permission-chip ${isDeleteAllowed ? 'active' : ''}`}
                    onClick={() => setAllowDelete((prev) => !prev)}
                    aria-pressed={isDeleteAllowed}
                  >
                    Delete
                  </button>
                  <button
                    type="button"
                    className={`api-permission-chip ${isCancelAllowed ? 'active' : ''}`}
                    onClick={() => setAllowCancel((prev) => !prev)}
                    aria-pressed={isCancelAllowed}
                  >
                    Cancel
                  </button>
                </div>
              </div>

              <div className="row end api-token-create-action">
                <button className="btn btn-primary" type="submit" disabled={isTokenCreating || !selectedProjectId}>
                  <Plus size={14} /> {isTokenCreating ? 'Creating...' : 'Create Token'}
                </button>
              </div>
            </form>

            {newTokenPlainText && (
              <div className="token-plain-block">
                <Field label="New Token">
                  <textarea rows={2} readOnly value={newTokenPlainText} />
                </Field>
                <div className="row">
                  <button
                    className={`btn btn-secondary ${copiedActionId === 'copy-new-token' ? 'is-copied' : ''}`}
                    type="button"
                    onClick={() => { void copyTextAction(newTokenPlainText, 'Token copied.', undefined, 'copy-new-token'); }}
                  >
                    <Copy size={14} /> Copy
                  </button>
                  <button className="btn btn-ghost" type="button" onClick={() => setNewTokenPlainText('')}>
                    Hide
                  </button>
                </div>
              </div>
            )}
          </section>

          <section className="api-token-modal-list">
            <div className="api-token-list-toolbar">
              {registryScopeProject && (
                <div className="api-token-scope-indicator">
                  <span className="badge">Project scope</span>
                  <strong>{registryScopeProject.name}</strong>
                </div>
              )}
              <label className="field api-token-search-field">
                <span><Search size={12} /> Find</span>
                <input
                  value={tokenQuery}
                  onChange={(e) => setTokenQuery(e.target.value)}
                  placeholder="name / prefix"
                />
              </label>
            </div>

            <div className="table-wrap api-token-table-wrap api-token-table-scroll">
              <table className="table api-token-table">
                <thead>
                  <tr>
                    <th>Name</th>
                    {isRegistryProjectColumnVisible && <th>Project</th>}
                    <th>Permissions</th>
                    <th>Status</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {isTokenLoading ? (
                    <tr>
                      <td colSpan={isRegistryProjectColumnVisible ? 5 : 4} className="muted">Loading...</td>
                    </tr>
                  ) : pagedTokens.length === 0 ? (
                    <tr>
                      <td colSpan={isRegistryProjectColumnVisible ? 5 : 4} className="muted">No tokens.</td>
                    </tr>
                  ) : (
                    pagedTokens.map((token) => {
                      const projectName = projects.find((item) => item.id === token.project_id)?.name || '-';
                      return (
                        <tr key={token.id} className={selectedTokenId === token.id ? 'row-selected' : ''}>
                          <td>{token.name}<br /><code>{token.token_prefix}...{token.token_last4}</code></td>
                          {isRegistryProjectColumnVisible && <td>{projectName}</td>}
                          <td>
                            <div className="api-token-perm-badges">
                              <span className={`api-token-perm-badge ${token.allow_submit ? 'on' : 'off'}`}>S</span>
                              <span className={`api-token-perm-badge ${token.allow_delete ? 'on' : 'off'}`}>D</span>
                              <span className={`api-token-perm-badge ${token.allow_cancel ? 'on' : 'off'}`}>C</span>
                            </div>
                          </td>
                          <td>
                            <span className={`api-token-status-chip ${token.is_active ? 'active' : 'revoked'}`}>
                              {token.is_active ? 'Active' : 'Revoked'}
                            </span>
                          </td>
                          <td>
                            <div className="api-token-actions">
                              <button
                                className="icon-btn"
                                type="button"
                                title="Select"
                                aria-label="Select token"
                                onClick={() => setSelectedTokenId(token.id)}
                              >
                                <Check size={14} />
                              </button>
                              <button
                                className="icon-btn"
                                type="button"
                                title="Revoke"
                                aria-label="Revoke token"
                                disabled={!token.is_active || tokenRevokingId === token.id}
                                aria-busy={tokenRevokingId === token.id}
                                onClick={() => {
                                  void revokeTokenAction(token.id);
                                }}
                              >
                                {tokenRevokingId === token.id ? <LoaderCircle size={14} className="spin" /> : <ShieldOff size={14} />}
                              </button>
                              <button
                                className="icon-btn danger"
                                type="button"
                                title="Delete"
                                aria-label="Delete token"
                                disabled={tokenDeletingId === token.id}
                                aria-busy={tokenDeletingId === token.id}
                                onClick={() => {
                                  void removeTokenAction(token.id);
                                }}
                              >
                                {tokenDeletingId === token.id ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>

            <div className="api-pager">
              <button
                type="button"
                className="icon-btn"
                onClick={() => setTokenPage((prev) => Math.max(1, prev - 1))}
                disabled={tokenPage <= 1}
                title="Previous page"
                aria-label="Previous page"
              >
                <ChevronLeft size={14} />
              </button>
              <span className="muted small">{tokenPage} / {tokenPageCount}</span>
              <button
                type="button"
                className="icon-btn"
                onClick={() => setTokenPage((prev) => Math.min(tokenPageCount, prev + 1))}
                disabled={tokenPage >= tokenPageCount}
                title="Next page"
                aria-label="Next page"
              >
                <ChevronRight size={14} />
              </button>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
