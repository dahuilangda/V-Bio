/** Single curl command card. Pure presentational. */
import type { ReactNode } from 'react';
import { Copy, Download } from 'lucide-react';
import { InfoTip } from '../components/common/InfoTip';

interface CommandItemProps {
  index: number | string;
  title: ReactNode;
  command: string;
  isCopied: boolean;
  onCopy: () => void;
  copyLabel?: string;
  /** Optional secondary action (e.g. download YAML). */
  extraAction?: { label: string; onClick: () => void };
  /** Optional tooltip below the header. */
  hint?: string;
  /** Optional tooltip next to the title. */
  titleTip?: string;
  /** Disable the copy button. */
  isDisabled?: boolean;
  /** Optional content between header and command (e.g. hint paragraphs). */
  children?: ReactNode;
}

export function CommandItem({ index, title, command, isCopied, onCopy, copyLabel, extraAction, hint, titleTip, isDisabled, children }: CommandItemProps) {
  return (
    <article className="api-command-item">
      <header>
        <span>
          {typeof index === 'number' || index ? `${index}. ${title}` : title}
          {titleTip ? <InfoTip text={titleTip} /> : null}
        </span>
        <div className="api-yaml-preview-actions" style={extraAction ? undefined : { display: 'contents' }}>
          {extraAction ? (
            <button className="icon-btn" type="button" aria-label={extraAction.label} onClick={extraAction.onClick}>
              <Download size={14} />
            </button>
          ) : null}
          <button
            className={`icon-btn ${isCopied ? 'is-copied' : ''}`}
            type="button"
            aria-label={copyLabel ?? 'Copy command'}
            onClick={onCopy}
            disabled={isDisabled}
          >
            <Copy size={14} />
          </button>
        </div>
      </header>
      {hint ? <InfoTip text={hint} /> : null}
      {children}
      <pre><code>{command}</code></pre>
    </article>
  );
}
