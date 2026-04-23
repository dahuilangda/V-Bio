drop table if exists public.project_task_chat_messages cascade;

create table if not exists public.project_copilot_messages (
  id uuid primary key default gen_random_uuid(),
  context_type text not null default 'task_list',
  project_id uuid references public.projects(id) on delete cascade,
  project_task_id uuid references public.project_tasks(id) on delete cascade,
  user_id uuid references public.app_users(id) on delete set null,
  role text not null default 'user',
  content text not null default '',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_project_copilot_messages_scope_time
on public.project_copilot_messages (context_type, project_id, project_task_id, created_at asc);

drop trigger if exists trg_project_copilot_messages_updated_at on public.project_copilot_messages;
create trigger trg_project_copilot_messages_updated_at
before update on public.project_copilot_messages
for each row
execute procedure public.set_updated_at();

alter table public.project_copilot_messages enable row level security;

drop policy if exists project_copilot_messages_anon_select on public.project_copilot_messages;
drop policy if exists project_copilot_messages_anon_insert on public.project_copilot_messages;
drop policy if exists project_copilot_messages_anon_update on public.project_copilot_messages;
drop policy if exists project_copilot_messages_anon_delete on public.project_copilot_messages;

create policy project_copilot_messages_anon_select
on public.project_copilot_messages
for select
to anon
using (true);

create policy project_copilot_messages_anon_insert
on public.project_copilot_messages
for insert
to anon
with check (true);

create policy project_copilot_messages_anon_update
on public.project_copilot_messages
for update
to anon
using (true)
with check (true);

create policy project_copilot_messages_anon_delete
on public.project_copilot_messages
for delete
to anon
using (true);

grant select, insert, update, delete on public.project_copilot_messages to anon, authenticated, service_role;
