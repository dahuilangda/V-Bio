drop view if exists public.project_task_counts;
create or replace view public.project_task_counts as
with normalized as (
  select
    project_id,
    upper(coalesce(task_state, '')) as normalized_task_state,
    greatest(
      0,
      coalesce(
        nullif(properties->'lead_opt_state'->'prediction_summary'->>'total', '')::bigint,
        nullif(properties->'lead_opt_list'->'prediction_summary'->>'total', '')::bigint,
        0
      )
    ) as lead_opt_total,
    greatest(
      0,
      coalesce(
        nullif(properties->'lead_opt_state'->'prediction_summary'->>'running', '')::bigint,
        nullif(properties->'lead_opt_list'->'prediction_summary'->>'running', '')::bigint,
        0
      )
    ) as lead_opt_running,
    greatest(
      0,
      coalesce(
        nullif(properties->'lead_opt_state'->'prediction_summary'->>'queued', '')::bigint,
        nullif(properties->'lead_opt_list'->'prediction_summary'->>'queued', '')::bigint,
        0
      )
    ) as lead_opt_queued,
    greatest(
      0,
      coalesce(
        nullif(properties->'lead_opt_state'->'prediction_summary'->>'success', '')::bigint,
        nullif(properties->'lead_opt_list'->'prediction_summary'->>'success', '')::bigint,
        0
      )
    ) as lead_opt_success,
    greatest(
      0,
      coalesce(
        nullif(properties->'lead_opt_state'->'prediction_summary'->>'failure', '')::bigint,
        nullif(properties->'lead_opt_list'->'prediction_summary'->>'failure', '')::bigint,
        0
      )
    ) as lead_opt_failure
  from public.project_tasks
),
lead_opt_counts as (
  select
    project_id,
    normalized_task_state,
    lead_opt_total,
    lead_opt_success,
    lead_opt_failure,
    least(
      lead_opt_running,
      greatest(lead_opt_total - lead_opt_success - lead_opt_failure, 0)
    ) as effective_lead_opt_running,
    least(
      lead_opt_queued,
      greatest(
        lead_opt_total
        - lead_opt_success
        - lead_opt_failure
        - least(
          lead_opt_running,
          greatest(lead_opt_total - lead_opt_success - lead_opt_failure, 0)
        ),
        0
      )
    ) as effective_lead_opt_queued
  from normalized
)
select
  project_id,
  sum(
    case
      when lead_opt_total > 0 then lead_opt_total
      else 1
    end
  )::bigint as total_count,
  sum(
    case
      when lead_opt_total > 0 then effective_lead_opt_running
      else 0
    end
  )::bigint as running_count,
  sum(
    case
      when lead_opt_total > 0 then lead_opt_success
      when normalized_task_state = 'SUCCESS' then 1
      else 0
    end
  )::bigint as success_count,
  sum(
    case
      when lead_opt_total > 0 then lead_opt_failure
      when normalized_task_state = 'FAILURE' then 1
      else 0
    end
  )::bigint as failure_count,
  sum(
    case
      when lead_opt_total > 0 then effective_lead_opt_queued
      when normalized_task_state in ('QUEUED', 'RUNNING') then 1
      else 0
    end
  )::bigint as queued_count,
  sum(
    case
      when lead_opt_total > 0 then greatest(
        lead_opt_total
        - lead_opt_success
        - lead_opt_failure
        - effective_lead_opt_running
        - effective_lead_opt_queued,
        0
      )
      when normalized_task_state not in ('SUCCESS', 'FAILURE', 'QUEUED', 'RUNNING') then 1
      else 0
    end
  )::bigint as other_count
from lead_opt_counts
group by project_id;
