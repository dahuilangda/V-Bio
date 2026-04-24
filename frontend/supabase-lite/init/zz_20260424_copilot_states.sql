create table if not exists public.project_copilot_states (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.app_users(id) on delete cascade,
  state_key text not null,
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, state_key)
);

create index if not exists idx_project_copilot_states_user_key
on public.project_copilot_states (user_id, state_key);

drop trigger if exists trg_project_copilot_states_updated_at on public.project_copilot_states;
create trigger trg_project_copilot_states_updated_at
before update on public.project_copilot_states
for each row
execute procedure public.set_updated_at();

alter table public.project_copilot_states enable row level security;

drop policy if exists project_copilot_states_anon_select on public.project_copilot_states;
drop policy if exists project_copilot_states_anon_insert on public.project_copilot_states;
drop policy if exists project_copilot_states_anon_update on public.project_copilot_states;
drop policy if exists project_copilot_states_anon_delete on public.project_copilot_states;

create policy project_copilot_states_anon_select
on public.project_copilot_states
for select
to anon
using (true);

create policy project_copilot_states_anon_insert
on public.project_copilot_states
for insert
to anon
with check (true);

create policy project_copilot_states_anon_update
on public.project_copilot_states
for update
to anon
using (true)
with check (true);

create policy project_copilot_states_anon_delete
on public.project_copilot_states
for delete
to anon
using (true);

grant select, insert, update, delete on public.project_copilot_states to anon, authenticated, service_role;
