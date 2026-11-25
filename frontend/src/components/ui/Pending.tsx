import './Pending.css';

export function Skeleton({ className = '', width, height, radius, style }: {
  className?: string;
  width?: number | string;
  height?: number | string;
  radius?: number | string;
  style?: React.CSSProperties;
}) {
  return <span className={`skeleton ${className}`} style={{ width, height, borderRadius: radius, ...style }} />;
}

export function InlineSpinner({ size = 14, className = '' }: { size?: number; className?: string }) {
  return (
    <span className={`inline-spinner ${className}`} style={{ width: size, height: size }} role="status" aria-label="Loading">
      <svg viewBox="0 0 16 16" fill="none">
        <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeOpacity="0.2" strokeWidth="1.5" />
        <path d="M14.25 8A6.25 6.25 0 0 0 8 1.75" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
    </span>
  );
}

export function LoadingLine({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`loading-line ${className}`} role="status">
      <InlineSpinner size={14} />
      <span>{children}</span>
    </div>
  );
}

export function FadeIn({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return <div className={`fade-in ${className}`}>{children}</div>;
}

export function SkeletonRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="skeleton-rows" role="status" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="skeleton-row" style={{ width: `${96 - (index % 3) * 9}%` }} />
      ))}
    </div>
  );
}
