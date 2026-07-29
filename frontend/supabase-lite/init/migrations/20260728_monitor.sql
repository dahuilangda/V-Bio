-- Durable monitor projection, event journal, and SQL-side task rollups.
-- This file is applied by backend.monitoring.migration_runner, not by the
-- postgres image's one-time init scan (the migrations directory is nested).

create table if not exists public.monitor_workers_current (
  worker_id text primary key,
  server text not null,
  host text not null default '',
  worker_type text not null default 'mixed',
  queues jsonb not null default '[]'::jsonb,
  capabilities jsonb not null default '[]'::jsonb,
  slots_total integer not null default 0,
  slots_busy integer not null default 0,
  slots_idle integer not null default 0,
  gpu_slots_total integer not null default 0,
  cpu_slots_total integer not null default 0,
  active_count integer not null default 0,
  reserved_count integer not null default 0,
  scheduled_count integer not null default 0,
  executed_total_since_start bigint not null default 0,
  executed_by_task_name jsonb not null default '{}'::jsonb,
  worker_stats jsonb not null default '{}'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  state text not null default 'online',
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  lease_expires_at timestamptz not null default now() + interval '90 seconds',
  updated_at timestamptz not null default now()
);

create table if not exists public.monitor_tasks_current (
  task_id text primary key,
  project_task_id uuid,
  project_id uuid,
  name text not null default '',
  capability text,
  queue text not null default '',
  worker_id text,
  raw_state text not null default 'PENDING',
  state_bucket text not null default 'queued',
  status_text text not null default '',
  error_text text not null default '',
  details jsonb not null default '{}'::jsonb,
  submitted_at timestamptz,
  started_at timestamptz,
  completed_at timestamptz,
  runtime_seconds double precision,
  last_seen_at timestamptz not null default now(),
  lease_expires_at timestamptz,
  last_event_at timestamptz,
  last_event_key text,
  state_revision bigint not null default 0,
  updated_at timestamptz not null default now()
);

create index if not exists monitor_tasks_current_active_idx
  on public.monitor_tasks_current (state_bucket, lease_expires_at, updated_at desc);
create index if not exists monitor_tasks_current_worker_idx
  on public.monitor_tasks_current (worker_id, state_bucket, updated_at desc);
create index if not exists monitor_tasks_current_project_idx
  on public.monitor_tasks_current (project_id, updated_at desc);

create table if not exists public.monitor_events (
  sequence bigint generated always as identity primary key,
  event_key text not null unique,
  entity_type text not null,
  entity_id text not null,
  event_type text not null,
  state_bucket text,
  occurred_at timestamptz not null default now(),
  source text not null default 'collector',
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists monitor_events_cursor_idx
  on public.monitor_events (sequence);
create index if not exists monitor_events_created_idx
  on public.monitor_events (created_at desc);
create index if not exists monitor_events_entity_idx
  on public.monitor_events (entity_type, entity_id, sequence desc);

create table if not exists public.monitor_task_rollups_hourly (
  bucket_start timestamptz not null,
  backend text not null,
  state_bucket text not null,
  total_count bigint not null default 0,
  terminal_count bigint not null default 0,
  success_count bigint not null default 0,
  duration_sum double precision not null default 0,
  duration_count bigint not null default 0,
  primary key (bucket_start, backend, state_bucket)
);

create index if not exists monitor_rollups_window_idx
  on public.monitor_task_rollups_hourly (bucket_start desc, backend, state_bucket);
create index if not exists project_tasks_monitor_window_idx
  on public.project_tasks ((coalesce(submitted_at, created_at)) desc)
  where task_id <> '';

create or replace function public.monitor_normalize_task_state(raw_state text)
returns text
language sql
immutable
as $$
  select case
    when upper(coalesce(raw_state, '')) in ('QUEUED', 'PENDING', 'RECEIVED', 'WAITING', 'SENT') then 'queued'
    when upper(coalesce(raw_state, '')) in ('RUNNING', 'STARTED', 'PREPARING', 'ACQUIRING_GPU', 'GPU_ACQUIRED', 'PROCESSING_OUTPUT', 'UPLOADING', 'PACKAGING') then 'running'
    when upper(coalesce(raw_state, '')) in ('SUCCESS', 'SUCCEEDED', 'COMPLETED', 'COMPLETE') then 'success'
    when upper(coalesce(raw_state, '')) in ('FAILURE', 'FAILED', 'ERROR', 'TIMEOUT', 'TIMED_OUT') then 'failure'
    when upper(coalesce(raw_state, '')) in ('REVOKED', 'REJECTED', 'CANCELLED', 'CANCELED') then 'cancelled'
    else 'other'
  end
$$;

create or replace function public.monitor_rollup_adjust(
  p_bucket_start timestamptz,
  p_backend text,
  p_state_bucket text,
  p_count integer,
  p_duration double precision
)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_terminal boolean := p_state_bucket in ('success', 'failure', 'cancelled');
  v_success boolean := p_state_bucket = 'success';
  v_duration double precision := case
    when p_duration is not null
      and p_duration > '-Infinity'::double precision
      and p_duration < 'Infinity'::double precision
    then p_duration else 0 end;
  v_duration_count integer := case
    when p_duration is not null
      and p_duration > '-Infinity'::double precision
      and p_duration < 'Infinity'::double precision
    then 1 else 0 end;
begin
  if p_count = 0 or p_bucket_start is null then
    return;
  end if;
  insert into public.monitor_task_rollups_hourly (
    bucket_start, backend, state_bucket, total_count, terminal_count,
    success_count, duration_sum, duration_count
  ) values (
    p_bucket_start, coalesce(p_backend, 'unknown'), coalesce(p_state_bucket, 'other'),
    p_count,
    case when v_terminal then p_count else 0 end,
    case when v_success then p_count else 0 end,
    v_duration * p_count,
    v_duration_count * p_count
  )
  on conflict (bucket_start, backend, state_bucket) do update set
    total_count = monitor_task_rollups_hourly.total_count + excluded.total_count,
    terminal_count = monitor_task_rollups_hourly.terminal_count + excluded.terminal_count,
    success_count = monitor_task_rollups_hourly.success_count + excluded.success_count,
    duration_sum = monitor_task_rollups_hourly.duration_sum + excluded.duration_sum,
    duration_count = monitor_task_rollups_hourly.duration_count + excluded.duration_count;

  delete from public.monitor_task_rollups_hourly
  where bucket_start = p_bucket_start
    and backend = coalesce(p_backend, 'unknown')
    and state_bucket = coalesce(p_state_bucket, 'other')
    and total_count <= 0;
end
$$;

create or replace function public.monitor_project_tasks_rollup_trigger()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_old_time timestamptz;
  v_new_time timestamptz;
  v_old_state text;
  v_new_state text;
begin
  if tg_op = 'UPDATE'
    and old.task_id is not distinct from new.task_id
    and old.task_state is not distinct from new.task_state
    and old.backend is not distinct from new.backend
    and old.submitted_at is not distinct from new.submitted_at
    and old.created_at is not distinct from new.created_at
    and old.duration_seconds is not distinct from new.duration_seconds then
    return new;
  end if;
  if tg_op in ('UPDATE', 'DELETE') and old.task_id <> '' then
    v_old_time := date_trunc('hour', coalesce(old.submitted_at, old.created_at));
    v_old_state := public.monitor_normalize_task_state(old.task_state);
    perform public.monitor_rollup_adjust(v_old_time, old.backend, v_old_state, -1, old.duration_seconds);
  end if;
  if tg_op in ('INSERT', 'UPDATE') and new.task_id <> '' then
    v_new_time := date_trunc('hour', coalesce(new.submitted_at, new.created_at));
    v_new_state := public.monitor_normalize_task_state(new.task_state);
    perform public.monitor_rollup_adjust(v_new_time, new.backend, v_new_state, 1, new.duration_seconds);
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

drop trigger if exists monitor_project_tasks_rollup on public.project_tasks;
create trigger monitor_project_tasks_rollup
after insert or update or delete on public.project_tasks
for each row execute procedure public.monitor_project_tasks_rollup_trigger();

create or replace function public.monitor_record_event(
  p_event_key text,
  p_entity_type text,
  p_entity_id text,
  p_event_type text,
  p_state_bucket text,
  p_occurred_at timestamptz,
  p_source text,
  p_payload jsonb
)
returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_sequence bigint;
begin
  insert into public.monitor_events (
    event_key, entity_type, entity_id, event_type, state_bucket,
    occurred_at, source, payload
  ) values (
    p_event_key, p_entity_type, p_entity_id, p_event_type, p_state_bucket,
    coalesce(p_occurred_at, now()), coalesce(p_source, 'collector'), coalesce(p_payload, '{}'::jsonb)
  )
  on conflict (event_key) do nothing
  returning sequence into v_sequence;
  if v_sequence is not null then
    perform pg_notify('vbio_monitor', v_sequence::text);
  end if;
  return v_sequence;
end
$$;

create or replace function public.monitor_project_tasks_projection_trigger()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_bucket text;
  v_changed boolean := tg_op = 'INSERT';
  v_event_key text;
  v_payload jsonb;
begin
  if tg_op = 'DELETE' then
    if old.task_id <> '' then
      delete from public.monitor_tasks_current where task_id = old.task_id;
      perform public.monitor_record_event(
        'project-task-delete:' || old.id::text || ':' || coalesce(old.updated_at::text, clock_timestamp()::text),
        'task', old.task_id, 'task_deleted', 'other', old.updated_at, 'project_tasks',
        jsonb_build_object('task_id', old.task_id, 'project_task_id', old.id, 'deleted', true)
      );
    end if;
    return old;
  end if;

  if new.task_id = '' then
    return new;
  end if;
  if tg_op = 'UPDATE'
    and old.task_id is not distinct from new.task_id
    and old.project_id is not distinct from new.project_id
    and old.name is not distinct from new.name
    and old.backend is not distinct from new.backend
    and old.task_state is not distinct from new.task_state
    and old.status_text is not distinct from new.status_text
    and old.error_text is not distinct from new.error_text
    and old.submitted_at is not distinct from new.submitted_at
    and old.completed_at is not distinct from new.completed_at
    and old.duration_seconds is not distinct from new.duration_seconds then
    return new;
  end if;
  if tg_op = 'UPDATE' then
    v_changed := old.task_state is distinct from new.task_state
      or old.status_text is distinct from new.status_text
      or old.error_text is distinct from new.error_text
      or old.completed_at is distinct from new.completed_at
      or old.duration_seconds is distinct from new.duration_seconds;
  end if;
  v_bucket := public.monitor_normalize_task_state(new.task_state);
  insert into public.monitor_tasks_current (
    task_id, project_task_id, project_id, name, capability, raw_state, state_bucket,
    status_text, error_text, submitted_at, completed_at, runtime_seconds,
    last_seen_at, lease_expires_at, updated_at
  ) values (
    new.task_id, new.id, new.project_id, new.name, new.backend, new.task_state, v_bucket,
    new.status_text, new.error_text, new.submitted_at, new.completed_at, new.duration_seconds,
    now(), case when v_bucket = 'running' then now() + interval '3 minutes' else null end, now()
  )
  on conflict (task_id) do update set
    project_task_id = excluded.project_task_id,
    project_id = excluded.project_id,
    name = excluded.name,
    capability = excluded.capability,
    raw_state = excluded.raw_state,
    state_bucket = excluded.state_bucket,
    status_text = excluded.status_text,
    error_text = excluded.error_text,
    submitted_at = excluded.submitted_at,
    completed_at = excluded.completed_at,
    runtime_seconds = excluded.runtime_seconds,
    last_seen_at = excluded.last_seen_at,
    lease_expires_at = excluded.lease_expires_at,
    updated_at = excluded.updated_at,
    state_revision = monitor_tasks_current.state_revision + case when (
      monitor_tasks_current.raw_state is distinct from excluded.raw_state
      or monitor_tasks_current.state_bucket is distinct from excluded.state_bucket
    ) then 1 else 0 end;

  if v_changed then
    v_event_key := 'project-task:' || new.id::text || ':' || coalesce(new.updated_at::text, clock_timestamp()::text);
    v_payload := jsonb_build_object(
      'task_id', new.task_id, 'project_task_id', new.id, 'project_id', new.project_id,
      'state', new.task_state, 'state_bucket', v_bucket
    );
    perform public.monitor_record_event(
      v_event_key, 'task', new.task_id, 'task_projection', v_bucket,
      coalesce(new.updated_at, now()), 'project_tasks', v_payload
    );
  end if;
  return new;
end
$$;

drop trigger if exists monitor_project_tasks_projection on public.project_tasks;
create trigger monitor_project_tasks_projection
after insert or update or delete on public.project_tasks
for each row execute procedure public.monitor_project_tasks_projection_trigger();

truncate table public.monitor_task_rollups_hourly;
insert into public.monitor_task_rollups_hourly (
  bucket_start, backend, state_bucket, total_count, terminal_count,
  success_count, duration_sum, duration_count
)
select
  date_trunc('hour', coalesce(submitted_at, created_at)),
  coalesce(nullif(backend, ''), 'unknown'),
  public.monitor_normalize_task_state(task_state),
  count(*)::bigint,
  count(*) filter (where public.monitor_normalize_task_state(task_state) in ('success', 'failure', 'cancelled'))::bigint,
  count(*) filter (where public.monitor_normalize_task_state(task_state) = 'success')::bigint,
  coalesce(sum(duration_seconds) filter (
    where duration_seconds is not null
      and duration_seconds > '-Infinity'::double precision
      and duration_seconds < 'Infinity'::double precision
  ), 0),
  count(duration_seconds) filter (
    where duration_seconds is not null
      and duration_seconds > '-Infinity'::double precision
      and duration_seconds < 'Infinity'::double precision
  )::bigint
from public.project_tasks
where task_id <> ''
group by 1, 2, 3;

create or replace function public.monitor_task_statistics(
  p_window_hours integer default 24,
  p_recent_limit integer default 20
)
returns jsonb
language plpgsql
stable
as $$
declare
  v_hours integer := greatest(1, least(24 * 31, coalesce(p_window_hours, 24)));
  v_recent integer := greatest(1, least(200, coalesce(p_recent_limit, 20)));
  v_now timestamptz := now();
  v_step interval := case when v_hours <= 48 then interval '1 hour' else interval '1 day' end;
  v_bucket_count integer := case when v_hours <= 48 then v_hours else ceil(v_hours::numeric / 24)::integer end;
  v_start timestamptz;
  v_total bigint;
  v_terminal bigint;
  v_success bigint;
  v_duration_sum double precision;
  v_duration_count bigint;
  v_states jsonb;
  v_backends jsonb;
  v_timeline jsonb;
  v_recent_tasks jsonb;
begin
  v_start := date_trunc(case when v_hours <= 48 then 'hour' else 'day' end, v_now)
    - (v_bucket_count - 1) * v_step;

  select coalesce(sum(total_count), 0)::bigint,
         coalesce(sum(terminal_count), 0)::bigint,
         coalesce(sum(success_count), 0)::bigint,
         coalesce(sum(duration_sum), 0),
         coalesce(sum(duration_count), 0)::bigint
    into v_total, v_terminal, v_success, v_duration_sum, v_duration_count
  from public.monitor_task_rollups_hourly
  where bucket_start >= v_start and bucket_start <= v_now;

  with buckets as (
    select generate_series(
      v_start,
      date_trunc(case when v_hours <= 48 then 'hour' else 'day' end, v_now),
      v_step
    ) as bucket_start
  ), counts as (
    select date_trunc(case when v_hours <= 48 then 'hour' else 'day' end, bucket_start) as bucket_start,
      sum(total_count)::bigint as total,
      coalesce(sum(total_count) filter (where state_bucket = 'success'), 0)::bigint as success,
      coalesce(sum(total_count) filter (where state_bucket = 'failure'), 0)::bigint as failure
    from public.monitor_task_rollups_hourly
    where bucket_start >= v_start and bucket_start <= v_now
    group by 1
  )
  select coalesce(jsonb_agg(jsonb_build_object(
    'start', b.bucket_start,
    'total', coalesce(c.total, 0),
    'success', coalesce(c.success, 0),
    'failure', coalesce(c.failure, 0)
  ) order by b.bucket_start), '[]'::jsonb)
    into v_timeline
  from buckets b left join counts c using (bucket_start);

  with counts as (
    select state_bucket as bucket, sum(total_count)::bigint as total
    from public.monitor_task_rollups_hourly
    where bucket_start >= v_start and bucket_start <= v_now
    group by state_bucket
  )
  select jsonb_build_object(
    'queued', coalesce((select total from counts where bucket = 'queued'), 0),
    'running', coalesce((select total from counts where bucket = 'running'), 0),
    'success', coalesce((select total from counts where bucket = 'success'), 0),
    'failure', coalesce((select total from counts where bucket = 'failure'), 0),
    'cancelled', coalesce((select total from counts where bucket = 'cancelled'), 0),
    'other', coalesce((select total from counts where bucket = 'other'), 0)
  ) into v_states;

  with grouped as (
    select backend,
      sum(total_count)::bigint as total,
      coalesce(sum(total_count) filter (where state_bucket = 'queued'), 0)::bigint as queued,
      coalesce(sum(total_count) filter (where state_bucket = 'running'), 0)::bigint as running,
      coalesce(sum(total_count) filter (where state_bucket = 'success'), 0)::bigint as success,
      coalesce(sum(total_count) filter (where state_bucket = 'failure'), 0)::bigint as failure,
      coalesce(sum(total_count) filter (where state_bucket = 'cancelled'), 0)::bigint as cancelled,
      coalesce(sum(total_count) filter (where state_bucket = 'other'), 0)::bigint as other
    from public.monitor_task_rollups_hourly
    where bucket_start >= v_start and bucket_start <= v_now
    group by backend
  )
  select coalesce(jsonb_agg(to_jsonb(grouped) order by backend), '[]'::jsonb) into v_backends from grouped;

  select coalesce(jsonb_agg(to_jsonb(row_data) order by submitted_at desc), '[]'::jsonb)
    into v_recent_tasks
  from (
    select id, project_id, task_id, name, backend, task_state as state,
      public.monitor_normalize_task_state(task_state) as bucket,
      coalesce(submitted_at, created_at) as submitted_at, completed_at,
      duration_seconds, status_text, error_text
    from public.project_tasks
    where task_id <> '' and coalesce(submitted_at, created_at) >= v_start
      and coalesce(submitted_at, created_at) <= v_now + interval '5 minutes'
    order by coalesce(submitted_at, created_at) desc
    limit v_recent
  ) row_data;

  return jsonb_build_object(
    'generated_at', v_now,
    'window_start', v_start,
    'window_hours', v_hours,
    'total', v_total,
    'states', v_states,
    'terminal_total', v_terminal,
    'success_rate', case when v_terminal > 0 then v_success::numeric / v_terminal else null end,
    'average_duration_seconds', case when v_duration_count > 0 then v_duration_sum / v_duration_count else null end,
    'by_backend', v_backends,
    'timeline', v_timeline,
    'recent_tasks', v_recent_tasks,
    'truncated', false
  );
end
$$;

create or replace function public.monitor_expire_leases()
returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  v_count bigint := 0;
  v_row record;
begin
  for v_row in
    update public.monitor_workers_current
       set state = 'stale', updated_at = now()
     where state = 'online' and lease_expires_at < now()
     returning worker_id
  loop
    v_count := v_count + 1;
    perform public.monitor_record_event(
      'worker-stale:' || v_row.worker_id || ':' || date_trunc('second', now())::text,
      'worker', v_row.worker_id, 'worker_stale', 'other', now(), 'lease',
      jsonb_build_object('worker_id', v_row.worker_id, 'state', 'stale')
    );
  end loop;

  for v_row in
    update public.monitor_tasks_current
       set state_bucket = 'other', raw_state = 'STALE', updated_at = now(), lease_expires_at = null
     where lease_expires_at < now() and state_bucket = 'running'
     returning task_id
  loop
    v_count := v_count + 1;
    perform public.monitor_record_event(
      'task-stale:' || v_row.task_id || ':' || date_trunc('second', now())::text,
      'task', v_row.task_id, 'task_stale', 'other', now(), 'lease',
      jsonb_build_object('task_id', v_row.task_id, 'state', 'STALE')
    );
  end loop;
  return v_count;
end
$$;

create or replace function public.monitor_prune_events(p_keep interval default interval '30 days')
returns bigint
language sql
security definer
set search_path = public, pg_temp
as $$
  with removed as (
    delete from public.monitor_events where created_at < now() - p_keep returning 1
  ) select count(*)::bigint from removed
$$;

alter table public.monitor_workers_current enable row level security;
alter table public.monitor_tasks_current enable row level security;
alter table public.monitor_events enable row level security;
alter table public.monitor_task_rollups_hourly enable row level security;

revoke all on public.monitor_workers_current from public, anon, authenticated;
revoke all on public.monitor_tasks_current from public, anon, authenticated;
revoke all on public.monitor_events from public, anon, authenticated;
revoke all on public.monitor_task_rollups_hourly from public, anon, authenticated;
revoke execute on function public.monitor_rollup_adjust(timestamptz, text, text, integer, double precision) from public, anon, authenticated;

drop policy if exists monitor_workers_service_role on public.monitor_workers_current;
create policy monitor_workers_service_role on public.monitor_workers_current for all to service_role using (true) with check (true);
drop policy if exists monitor_tasks_service_role on public.monitor_tasks_current;
create policy monitor_tasks_service_role on public.monitor_tasks_current for all to service_role using (true) with check (true);
drop policy if exists monitor_events_service_role on public.monitor_events;
create policy monitor_events_service_role on public.monitor_events for all to service_role using (true) with check (true);
drop policy if exists monitor_rollups_service_role on public.monitor_task_rollups_hourly;
create policy monitor_rollups_service_role on public.monitor_task_rollups_hourly for all to service_role using (true) with check (true);

grant select, insert, update, delete on public.monitor_workers_current to service_role;
grant select, insert, update, delete on public.monitor_tasks_current to service_role;
grant select, insert, update, delete on public.monitor_events to service_role;
grant select, insert, update, delete on public.monitor_task_rollups_hourly to service_role;
grant usage, select on all sequences in schema public to service_role;
revoke execute on function public.monitor_record_event(text, text, text, text, text, timestamptz, text, jsonb) from public, anon, authenticated;
revoke execute on function public.monitor_task_statistics(integer, integer) from public, anon, authenticated;
revoke execute on function public.monitor_expire_leases() from public, anon, authenticated;
revoke execute on function public.monitor_prune_events(interval) from public, anon, authenticated;
grant execute on function public.monitor_normalize_task_state(text) to service_role;
grant execute on function public.monitor_record_event(text, text, text, text, text, timestamptz, text, jsonb) to service_role;
grant execute on function public.monitor_task_statistics(integer, integer) to service_role;
grant execute on function public.monitor_expire_leases() to service_role;
grant execute on function public.monitor_prune_events(interval) to service_role;
