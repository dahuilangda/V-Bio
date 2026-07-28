import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock3,
  Cpu,
  Gauge,
  RefreshCcw,
  Server,
  Timer,
  Workflow,
  type LucideIcon
} from 'lucide-react';
import {
  fetchAdminClusterOverview,
  type AdminClusterOverview,
  type AdminTaskBucket,
  type AdminTaskStateCounts,
  type AdminTaskTimelinePoint,
  type AdminWorkerStatus
} from '../api/adminMonitorApi';
import { useAuth } from '../hooks/useAuth';
import { formatDateTime, formatDuration } from '../utils/date';

const WINDOW_OPTIONS = [
  { value: 24, label: '24h' },
  { value: 48, label: '48h' },
  { value: 24 * 7, label: '7d' },
  { value: 24 * 30, label: '30d' }
] as const;

const ADMIN_MONITOR_REFRESH_INTERVAL_MS = 60_000;

const EMPTY_STATES: AdminTaskStateCounts = {
  queued: 0,
  running: 0,
  success: 0,
  failure: 0,
  cancelled: 0,
  other: 0
};

const STATE_META: Array<{ key: keyof AdminTaskStateCounts; label: string; tone: AdminTaskBucket }> = [
  { key: 'success', label: 'Success', tone: 'success' },
  { key: 'running', label: 'Running', tone: 'running' },
  { key: 'queued', label: 'Queued', tone: 'queued' },
  { key: 'failure', label: 'Failed', tone: 'failure' },
  { key: 'cancelled', label: 'Cancelled', tone: 'cancelled' },
  { key: 'other', label: 'Other', tone: 'other' }
];

const CAPABILITY_LABELS: Record<string, string> = {
  boltz2: 'Boltz2',
  alphafold3: 'AlphaFold 3',
  protenix: 'Protenix',
  nesso: 'Nesso-1',
  pocketxmol: 'PocketXMol',
  boltz2score: 'Boltz2 Score',
  lead_opt: 'Lead Optimization',
  peptide_design: 'Peptide Design'
};

function capabilityLabel(value: string | null | undefined): string {
  const key = String(value || '').trim().toLowerCase();
  return CAPABILITY_LABELS[key] || key || 'Unknown';
}

function formatInteger(value: number | null | undefined): string {
  return new Intl.NumberFormat().format(Math.max(0, Number(value) || 0));
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '-';
  return `${Math.round(Number(value) * 100)}%`;
}

function formatUptime(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (!total) return '-';
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function taskStateLabel(bucket: AdminTaskBucket, rawState = ''): string {
  const item = STATE_META.find((candidate) => candidate.tone === bucket);
  return item?.label || rawState || 'Other';
}

function timelineLabel(value: string, windowHours: number): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  if (windowHours <= 48) {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function MonitorMetric({
  icon: Icon,
  label,
  value,
  detail,
  tone = 'neutral'
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  detail: string;
  tone?: 'neutral' | 'success' | 'warning';
}) {
  return (
    <article className={`admin-monitor-metric metric-tone-${tone}`}>
      <span className="admin-monitor-metric-icon" aria-hidden="true"><Icon size={17} /></span>
      <div className="admin-monitor-metric-copy">
        <span>{label}</span>
        <strong>{value}</strong>
        <small>{detail}</small>
      </div>
    </article>
  );
}

function TaskTimeline({
  points,
  windowHours
}: {
  points: AdminTaskTimelinePoint[];
  windowHours: number;
}) {
  const maximum = Math.max(0, ...points.map((point) => Number(point.total) || 0));
  const labelEvery = Math.max(1, Math.floor(points.length / 6));

  return (
    <div className="admin-monitor-chart-shell">
      <div className="admin-monitor-chart-legend" aria-label="Timeline legend">
        <span><i className="tone-success" />Success</span>
        <span><i className="tone-failure" />Failed</span>
        <span><i className="tone-other" />Other states</span>
      </div>
      <div
        className="admin-monitor-timeline"
        role="img"
        aria-label="Task volume over time"
        style={{ gridTemplateColumns: `repeat(${Math.max(points.length, 1)}, minmax(0, 1fr))` }}
      >
        {points.map((point, index) => {
          const success = Math.max(0, Number(point.success) || 0);
          const failure = Math.max(0, Number(point.failure) || 0);
          const other = Math.max(0, (Number(point.total) || 0) - success - failure);
          const successHeight = maximum ? (success / maximum) * 100 : 0;
          const failureHeight = maximum ? (failure / maximum) * 100 : 0;
          const otherHeight = maximum ? (other / maximum) * 100 : 0;
          const showLabel = index === 0 || index === points.length - 1 || index % labelEvery === 0;
          return (
            <div
              className={`admin-monitor-timeline-column${index === 0 ? ' is-first' : ''}${index === points.length - 1 ? ' is-last' : ''}`}
              key={point.start}
              title={`${formatDateTime(point.start)} · ${point.total} total · ${success} success · ${failure} failed`}
            >
              <div className="admin-monitor-timeline-bar">
                <span className="tone-success" style={{ height: `${successHeight}%`, bottom: 0 }} />
                <span className="tone-failure" style={{ height: `${failureHeight}%`, bottom: `${successHeight}%` }} />
                <span
                  className="tone-other"
                  style={{ height: `${otherHeight}%`, bottom: `${successHeight + failureHeight}%` }}
                />
              </div>
              <small>{showLabel ? timelineLabel(point.start, windowHours) : ''}</small>
            </div>
          );
        })}
        {!maximum ? <div className="admin-monitor-chart-empty">No submitted tasks in this window.</div> : null}
      </div>
    </div>
  );
}

function WorkerCurrentTask({ worker }: { worker: AdminWorkerStatus }) {
  const task = worker.tasks?.active?.[0];
  if (!task) return <span className="muted small">Idle</span>;
  return (
    <div className="admin-monitor-current-task">
      <strong>{capabilityLabel(task.capability)}</strong>
      <span title={task.name || task.id}>{task.name?.split('.').pop() || 'Active task'}</span>
      <code title={task.id}>{task.id ? task.id.slice(0, 10) : '-'}</code>
      {task.runtime_seconds !== null ? <small>{formatDuration(task.runtime_seconds)}</small> : null}
    </div>
  );
}

export function AdminMonitorPage() {
  const { session, loading: authLoading, ensureManagementSession } = useAuth();
  const [windowHours, setWindowHours] = useState(24);
  const [overview, setOverview] = useState<AdminClusterOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSequence = useRef(0);

  const loadOverview = useCallback(async (initial = false) => {
    const sequence = ++requestSequence.current;
    if (initial) setLoading(true);
    else setRefreshing(true);
    setError(null);
    try {
      const managementToken = await ensureManagementSession();
      if (!managementToken) {
        throw new Error('Administrator management session is unavailable. Sign in again to continue.');
      }
      const next = await fetchAdminClusterOverview(managementToken, windowHours);
      if (requestSequence.current !== sequence) return;
      setOverview(next);
    } catch (err) {
      if (requestSequence.current !== sequence) return;
      setError(err instanceof Error ? err.message : 'Unable to load cluster overview.');
    } finally {
      if (requestSequence.current === sequence) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [ensureManagementSession, windowHours]);

  useEffect(() => {
    if (authLoading) return;

    const refreshIfVisible = (initial = false) => {
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
      void loadOverview(initial);
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        void loadOverview(false);
      }
    };

    refreshIfVisible(true);
    const timer = window.setInterval(
      () => refreshIfVisible(false),
      ADMIN_MONITOR_REFRESH_INTERVAL_MS
    );
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
      requestSequence.current += 1;
    };
  }, [authLoading, loadOverview, session?.userId]);

  const cluster = overview?.cluster || null;
  const tasks = overview?.tasks || null;
  const states = tasks?.states || EMPTY_STATES;
  const workers = useMemo(
    () => Object.values(cluster?.workers || {}).sort((left, right) => left.server.localeCompare(right.server)),
    [cluster]
  );
  const capabilities = useMemo(
    () => Object.entries(cluster?.capabilities || {}).sort(([leftName, left], [rightName, right]) => {
      if (left.online !== right.online) return left.online ? -1 : 1;
      return capabilityLabel(leftName).localeCompare(capabilityLabel(rightName));
    }),
    [cluster]
  );

  const summary = cluster?.summary;
  const slotUtilization = summary?.slots_total
    ? (Number(summary.slots_busy) || 0) / Number(summary.slots_total)
    : 0;
  const runtimeQueued = workers.reduce(
    (total, worker) => total + (Number(worker.task_counts?.reserved) || 0) + (Number(worker.task_counts?.scheduled) || 0),
    0
  );
  const stateTotal = STATE_META.reduce((total, item) => total + (Number(states[item.key]) || 0), 0);

  return (
    <div className="page-grid admin-monitor-page">
      <section className="page-header admin-monitor-header">
        <div className="admin-monitor-heading">
          <span className="admin-monitor-eyebrow"><Activity size={14} />Administration</span>
          <h1>Cluster Monitor</h1>
          <p className="muted">Live worker state and platform-wide task execution.</p>
        </div>
        <div className="admin-monitor-header-actions">
          <div className="admin-monitor-window-switch" role="group" aria-label="Task statistics window">
            {WINDOW_OPTIONS.map((option) => (
              <button
                type="button"
                key={option.value}
                className={windowHours === option.value ? 'active' : ''}
                aria-pressed={windowHours === option.value}
                onClick={() => setWindowHours(option.value)}
              >
                {option.label}
              </button>
            ))}
          </div>
          <button
            type="button"
            className="icon-btn admin-monitor-refresh"
            title="Refresh cluster overview"
            aria-label="Refresh cluster overview"
            disabled={loading || refreshing}
            onClick={() => void loadOverview(false)}
          >
            <RefreshCcw size={15} className={loading || refreshing ? 'spinning' : ''} />
          </button>
          {overview?.generated_at ? (
            <div className="admin-monitor-updated" aria-live="polite">
              <span className={error ? 'is-error' : 'is-live'} />
              Updated {formatDateTime(overview.generated_at)}
            </div>
          ) : null}
        </div>
      </section>

      {error ? <div className="alert error"><AlertTriangle size={15} />{error}</div> : null}
      {overview?.cluster_error ? (
        <div className="admin-monitor-warning"><AlertTriangle size={15} /><span>Worker snapshot unavailable: {overview.cluster_error}</span></div>
      ) : null}
      {overview?.tasks_error ? (
        <div className="admin-monitor-warning"><AlertTriangle size={15} /><span>Task statistics unavailable: {overview.tasks_error}</span></div>
      ) : null}

      {loading && !overview ? (
        <section className="panel admin-monitor-loading">
          <RefreshCcw size={18} className="spinning" />
          <span>Collecting worker and task snapshots...</span>
        </section>
      ) : (
        <>
          <section className="admin-monitor-metrics" aria-label="Cluster summary">
            <MonitorMetric
              icon={Server}
              label="Online nodes"
              value={formatInteger(summary?.workers_total)}
              detail={`${formatInteger(summary?.capabilities_online)} of ${formatInteger(summary?.capabilities_total)} capabilities online`}
              tone={summary?.workers_total ? 'success' : 'warning'}
            />
            <MonitorMetric
              icon={Gauge}
              label="Compute slots"
              value={`${formatInteger(summary?.slots_busy)} / ${formatInteger(summary?.slots_total)}`}
              detail={`${formatPercent(slotUtilization)} utilized · ${formatInteger(summary?.slots_idle)} idle`}
            />
            <MonitorMetric
              icon={Activity}
              label="Runtime tasks"
              value={formatInteger(summary?.slots_busy)}
              detail={`${formatInteger(runtimeQueued)} reserved or scheduled`}
              tone={summary?.slots_busy ? 'success' : 'neutral'}
            />
            <MonitorMetric
              icon={Workflow}
              label="Tasks in window"
              value={formatInteger(tasks?.total)}
              detail={tasks ? `Since ${formatDateTime(tasks.window_start)}` : 'Statistics unavailable'}
            />
            <MonitorMetric
              icon={CheckCircle2}
              label="Success rate"
              value={formatPercent(tasks?.success_rate)}
              detail={`${formatInteger(states.success)} succeeded · ${formatInteger(states.failure)} failed`}
              tone={tasks?.success_rate !== null && Number(tasks?.success_rate) >= 0.9 ? 'success' : 'neutral'}
            />
            <MonitorMetric
              icon={Timer}
              label="Average duration"
              value={formatDuration(tasks?.average_duration_seconds)}
              detail={`${formatInteger(tasks?.terminal_total)} terminal tasks sampled`}
            />
          </section>

          <section className="admin-monitor-overview-grid">
            <article className="panel admin-monitor-chart-panel">
              <div className="admin-monitor-section-head">
                <div>
                  <h2>Task throughput</h2>
                  <p className="muted small">{windowHours <= 48 ? 'Hourly' : 'Daily'} submitted task volume</p>
                </div>
                {tasks?.truncated ? <span className="admin-monitor-truncated">First 10,000 rows</span> : null}
              </div>
              <TaskTimeline points={tasks?.timeline || []} windowHours={windowHours} />
            </article>

            <article className="panel admin-monitor-state-panel">
              <div className="admin-monitor-section-head">
                <div>
                  <h2>Task states</h2>
                  <p className="muted small">{formatInteger(stateTotal)} submitted tasks</p>
                </div>
              </div>
              <div className="admin-monitor-state-bar" aria-label="Task state distribution">
                {STATE_META.map((item) => {
                  const count = Number(states[item.key]) || 0;
                  if (!count || !stateTotal) return null;
                  return (
                    <span
                      key={item.key}
                      className={`tone-${item.tone}`}
                      style={{ width: `${(count / stateTotal) * 100}%` }}
                      title={`${item.label}: ${count}`}
                    />
                  );
                })}
                {!stateTotal ? <span className="tone-empty" style={{ width: '100%' }} /> : null}
              </div>
              <div className="admin-monitor-state-list">
                {STATE_META.map((item) => (
                  <div key={item.key}>
                    <span><i className={`tone-${item.tone}`} />{item.label}</span>
                    <strong>{formatInteger(states[item.key])}</strong>
                    <small>{stateTotal ? `${Math.round(((Number(states[item.key]) || 0) / stateTotal) * 100)}%` : '0%'}</small>
                  </div>
                ))}
              </div>
            </article>
          </section>

          <section className="panel admin-monitor-capabilities-panel">
            <div className="admin-monitor-section-head">
              <div>
                <h2>Capabilities</h2>
                <p className="muted small">Worker coverage and queue pressure by runtime capability</p>
              </div>
              <span className="admin-monitor-count"><Cpu size={14} />GPU {formatInteger(summary?.gpu_slots_total)} · CPU {formatInteger(summary?.cpu_slots_total)}</span>
            </div>
            <div className="admin-monitor-capability-grid">
              {capabilities.map(([name, capability]) => (
                <article className={`admin-monitor-capability ${capability.online ? 'online' : 'offline'}`} key={name}>
                  <div className="admin-monitor-capability-head">
                    <strong>{capabilityLabel(name)}</strong>
                    <span className="admin-monitor-status"><i />{capability.online ? 'Online' : 'Offline'}</span>
                  </div>
                  <div className="admin-monitor-capability-stats">
                    <span><small>Workers</small><b>{formatInteger(capability.worker_count)}</b></span>
                    <span><small>Capacity</small><b>{formatInteger(capability.max_running_tasks_upper_bound)}</b></span>
                    <span><small>Active</small><b>{formatInteger(capability.active_tasks_count)}</b></span>
                    <span><small>Queued</small><b>{formatInteger((capability.reserved_tasks_count || 0) + (capability.scheduled_tasks_count || 0))}</b></span>
                  </div>
                </article>
              ))}
              {!capabilities.length ? <div className="admin-monitor-empty">No capability snapshot is available.</div> : null}
            </div>
          </section>

          <section className="panel admin-monitor-workers-panel">
            <div className="admin-monitor-section-head">
              <div>
                <h2>Worker nodes</h2>
                <p className="muted small">Detected Celery workers, slot utilization, and current work</p>
              </div>
              <span className="admin-monitor-count"><Server size={14} />{formatInteger(workers.length)} online</span>
            </div>
            <div className="table-wrap admin-monitor-table-wrap">
              <table className="table admin-monitor-table admin-monitor-worker-table">
                <thead>
                  <tr>
                    <th>Node</th>
                    <th>Type</th>
                    <th>Capabilities</th>
                    <th>Slots</th>
                    <th>Queue state</th>
                    <th>Current work</th>
                    <th>Uptime</th>
                  </tr>
                </thead>
                <tbody>
                  {workers.map((worker) => {
                    const utilization = Math.max(0, Math.min(1, Number(worker.utilization?.slot_utilization) || 0));
                    return (
                      <tr key={worker.server}>
                        <td>
                          <div className="admin-monitor-node">
                            <span><i />{worker.host || worker.server}</span>
                            <code title={worker.server}>{worker.server}</code>
                          </div>
                        </td>
                        <td><span className={`admin-monitor-type type-${worker.worker_type}`}>{worker.worker_type.toUpperCase()}</span></td>
                        <td>
                          <div className="admin-monitor-tags">
                            {worker.capabilities.map((capability) => <span key={capability}>{capabilityLabel(capability)}</span>)}
                          </div>
                        </td>
                        <td>
                          <div className="admin-monitor-slot-cell">
                            <span><strong>{formatInteger(worker.resources?.slots_busy)}</strong> / {formatInteger(worker.resources?.slots_total)}</span>
                            <div><i style={{ width: `${utilization * 100}%` }} /></div>
                          </div>
                        </td>
                        <td>
                          <div className="admin-monitor-queue-counts">
                            <span><b>{formatInteger(worker.task_counts?.active)}</b> active</span>
                            <span><b>{formatInteger(worker.task_counts?.reserved)}</b> reserved</span>
                            <span><b>{formatInteger(worker.task_counts?.scheduled)}</b> scheduled</span>
                          </div>
                        </td>
                        <td><WorkerCurrentTask worker={worker} /></td>
                        <td>
                          <div className="admin-monitor-uptime"><Clock3 size={13} />{formatUptime(worker.worker_stats?.uptime_seconds)}</div>
                        </td>
                      </tr>
                    );
                  })}
                  {!workers.length ? (
                    <tr><td colSpan={7}><div className="admin-monitor-empty">No online worker nodes detected.</div></td></tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </section>

          <section className="admin-monitor-lower-grid">
            <article className="panel">
              <div className="admin-monitor-section-head">
                <div>
                  <h2>Backends</h2>
                  <p className="muted small">Task outcomes grouped by submitted backend</p>
                </div>
              </div>
              <div className="table-wrap admin-monitor-table-wrap">
                <table className="table admin-monitor-table">
                  <thead>
                    <tr>
                      <th>Backend</th>
                      <th>Total</th>
                      <th>Queued</th>
                      <th>Running</th>
                      <th>Success</th>
                      <th>Failed</th>
                      <th>Rate</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(tasks?.by_backend || []).map((backend) => {
                      const terminal = backend.success + backend.failure;
                      return (
                        <tr key={backend.backend}>
                          <td><strong>{capabilityLabel(backend.backend)}</strong></td>
                          <td>{formatInteger(backend.total)}</td>
                          <td>{formatInteger(backend.queued)}</td>
                          <td>{formatInteger(backend.running)}</td>
                          <td className="admin-monitor-success-text">{formatInteger(backend.success)}</td>
                          <td className="admin-monitor-failure-text">{formatInteger(backend.failure)}</td>
                          <td>{terminal ? `${Math.round((backend.success / terminal) * 100)}%` : '-'}</td>
                        </tr>
                      );
                    })}
                    {!tasks?.by_backend?.length ? (
                      <tr><td colSpan={7}><div className="admin-monitor-empty">No backend statistics in this window.</div></td></tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </article>

            <article className="panel">
              <div className="admin-monitor-section-head">
                <div>
                  <h2>Recent tasks</h2>
                  <p className="muted small">Newest submitted tasks across projects</p>
                </div>
              </div>
              <div className="table-wrap admin-monitor-table-wrap">
                <table className="table admin-monitor-table admin-monitor-recent-table">
                  <thead>
                    <tr>
                      <th>Task</th>
                      <th>Backend</th>
                      <th>Status</th>
                      <th>Submitted</th>
                      <th>Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(tasks?.recent_tasks || []).map((task) => (
                      <tr key={task.id || task.task_id}>
                        <td>
                          <div className="admin-monitor-task-name">
                            <strong title={task.name}>{task.name || 'Untitled task'}</strong>
                            <code title={task.task_id}>{task.task_id.slice(0, 12)}</code>
                            {task.error_text ? <small className="admin-monitor-failure-text" title={task.error_text}>{task.error_text}</small> : null}
                          </div>
                        </td>
                        <td>{capabilityLabel(task.backend)}</td>
                        <td><span className={`admin-monitor-task-state tone-${task.bucket}`}>{taskStateLabel(task.bucket, task.state)}</span></td>
                        <td>{formatDateTime(task.submitted_at)}</td>
                        <td>{formatDuration(task.duration_seconds)}</td>
                      </tr>
                    ))}
                    {!tasks?.recent_tasks?.length ? (
                      <tr><td colSpan={5}><div className="admin-monitor-empty">No recent submitted tasks.</div></td></tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </article>
          </section>
        </>
      )}
    </div>
  );
}
