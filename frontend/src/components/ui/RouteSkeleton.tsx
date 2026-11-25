import { Skeleton, SkeletonRows } from './Pending';
import './RouteSkeleton.css';

export function RouteSkeleton() {
  return (
    <div className="route-skeleton" role="status" aria-label="Loading page">
      <section className="page-header">
        <div>
          <Skeleton className="skel-title" />
          <Skeleton className="skel-subtitle" />
        </div>
        <Skeleton className="skel-button" />
      </section>
      <section className="panel route-skeleton-panel">
        <div className="route-skeleton-toolbar">
          <Skeleton className="skel-input" />
          <Skeleton className="skel-select" />
          <Skeleton className="skel-select" />
        </div>
        <SkeletonRows rows={6} />
      </section>
    </div>
  );
}
