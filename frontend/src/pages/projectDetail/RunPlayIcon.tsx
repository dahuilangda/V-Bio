interface RunPlayIconProps {
  size?: number;
  className?: string;
}

export function RunPlayIcon({ size = 15, className }: RunPlayIconProps) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
      className={className}
    >
      <path d="M8 5.25L19 12L8 18.75V5.25Z" fill="currentColor" stroke="none" />
    </svg>
  );
}
