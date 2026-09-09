/**
 * The per-project token panel of the API access page: lists a project's
 * tokens with use/revoke/delete actions and a jump into the token registry.
 * Pure presentation — all state and handlers stay in ApiAccessPage; the
 * wrapper there guarantees this renders only while a project is selected.
 */
import type { Dispatch, SetStateAction } from 'react';
import { KeyRound, LoaderCircle, ShieldOff, Trash2, X } from 'lucide-react';
import type { ModalDialogProps } from '../components/ui/useModalDialog';
import type { ApiToken, Project } from '../types/models';
import './ApiProjectTokenModal.css';

interface ApiProjectTokenModalProps {
  projectTokenPanelProjectId: string;
  projectTokenPanelProject: Project | null;
  projectTokenPanelTokens: ApiToken[];
  dialogProps: ModalDialogProps;
  setProjectTokenPanelProjectId: Dispatch<SetStateAction<string | null>>;
  setSelectedProjectId: Dispatch<SetStateAction<string>>;
  setSelectedTokenId: Dispatch<SetStateAction<string>>;
  tokenRevokingId: string | null;
  tokenDeletingId: string | null;
  revokeToken: (tokenId: string) => Promise<void>;
  removeToken: (tokenId: string) => Promise<void>;
  openTokenRegistryForProject: (projectId: string) => void;
}

export function ApiProjectTokenModal({
  projectTokenPanelProjectId,
  projectTokenPanelProject,
  projectTokenPanelTokens,
  dialogProps: projectTokenDialogProps,
  setProjectTokenPanelProjectId,
  setSelectedProjectId,
  setSelectedTokenId,
  tokenRevokingId,
  tokenDeletingId,
  revokeToken,
  removeToken,
  openTokenRegistryForProject
}: ApiProjectTokenModalProps) {
  return (
    <div className="modal-mask" onClick={() => setProjectTokenPanelProjectId(null)}>
      <div
        className="modal api-project-token-modal"
        onClick={(e) => e.stopPropagation()}
        {...projectTokenDialogProps}
        aria-label="Project tokens"
      >
        <div className="api-token-modal-head">
          <h2><KeyRound size={17} /> {projectTokenPanelProject?.name || 'Project'} Tokens</h2>
          <button
            className="icon-btn"
            type="button"
            aria-label="Close project token panel"
            onClick={() => setProjectTokenPanelProjectId(null)}
          >
            <X size={16} />
          </button>
        </div>
        <div className="api-project-token-modal-body">
          {projectTokenPanelTokens.length === 0 ? (
            <div className="api-project-token-modal-empty muted">
              <ShieldOff size={16} />
              <span>No tokens in this project yet.</span>
            </div>
          ) : (
            <div className="api-project-token-modal-list">
              {projectTokenPanelTokens.map((token) => (
                <article key={token.id} className="api-project-token-modal-item">
                  <div className="api-project-token-modal-main">
                    <strong>{token.name}</strong>
                    <code>{token.token_prefix}...{token.token_last4}</code>
                  </div>
                  <div className="api-project-token-modal-meta">
                    <span className={`badge ${token.is_active ? '' : 'badge-muted'}`}>
                      {token.is_active ? 'active' : 'revoked'}
                    </span>
                    <button
                      type="button"
                      className="btn btn-ghost btn-compact"
                      onClick={() => {
                        setSelectedProjectId(String(token.project_id || ''));
                        setSelectedTokenId(token.id);
                        setProjectTokenPanelProjectId(null);
                      }}
                    >
                      Use
                    </button>
                    {token.is_active && (
                      <button
                        type="button"
                        className="icon-btn"
                        title="Revoke token"
                        aria-label="Revoke token"
                        disabled={tokenRevokingId === token.id}
                        aria-busy={tokenRevokingId === token.id}
                        onClick={() => { void revokeToken(token.id); }}
                      >
                        {tokenRevokingId === token.id ? <LoaderCircle size={13} className="spin" /> : <ShieldOff size={13} />}
                      </button>
                    )}
                    <button
                      type="button"
                      className="icon-btn danger"
                      title="Delete token"
                      aria-label="Delete token"
                      disabled={tokenDeletingId === token.id}
                      aria-busy={tokenDeletingId === token.id}
                      onClick={() => { void removeToken(token.id); }}
                    >
                      {tokenDeletingId === token.id ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={13} />}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
          <div className="api-project-token-modal-actions">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                if (!projectTokenPanelProjectId) return;
                setProjectTokenPanelProjectId(null);
                openTokenRegistryForProject(projectTokenPanelProjectId);
              }}
            >
              <KeyRound size={13} />
              Open Token Registry
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
