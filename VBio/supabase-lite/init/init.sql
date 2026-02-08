create extension if not exists pgcrypto;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
    CREATE ROLE anon NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
    CREATE ROLE authenticated NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
    CREATE ROLE service_role NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticator') THEN
    CREATE ROLE authenticator NOINHERIT LOGIN PASSWORD 'authenticator';
  END IF;
END
$$;

GRANT anon TO authenticator;
GRANT authenticated TO authenticator;
GRANT service_role TO authenticator;

create table if not exists public.app_users (
  id uuid primary key default gen_random_uuid(),
  username text not null,
  name text not null default '',
  email text,
  password_hash text not null default '',
  is_admin boolean not null default false,
  last_login_at timestamptz,
  deleted_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.app_users add column if not exists username text;
alter table public.app_users add column if not exists name text;
alter table public.app_users add column if not exists email text;
alter table public.app_users add column if not exists password_hash text;
alter table public.app_users add column if not exists is_admin boolean;
alter table public.app_users add column if not exists last_login_at timestamptz;
alter table public.app_users add column if not exists deleted_at timestamptz;
alter table public.app_users add column if not exists created_at timestamptz;
alter table public.app_users add column if not exists updated_at timestamptz;

update public.app_users
set username = lower(
  regexp_replace(
    coalesce(
      nullif(name, ''),
      nullif(split_part(coalesce(email, ''), '@', 1), ''),
      'user_' || substring(id::text, 1, 8)
    ),
    '[^a-zA-Z0-9_.-]',
    '_',
    'g'
  )
)
where username is null or username = '';

update public.app_users
set name = username
where name is null or name = '';

update public.app_users
set password_hash = ''
where password_hash is null;

update public.app_users
set is_admin = false
where is_admin is null;

update public.app_users
set created_at = now()
where created_at is null;

update public.app_users
set updated_at = now()
where updated_at is null;

alter table public.app_users alter column username set not null;
alter table public.app_users alter column name set not null;
alter table public.app_users alter column password_hash set not null;
alter table public.app_users alter column is_admin set not null;
alter table public.app_users alter column created_at set not null;
alter table public.app_users alter column updated_at set not null;

create unique index if not exists idx_app_users_username on public.app_users (lower(username));
create index if not exists idx_app_users_created_at on public.app_users (created_at asc);

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(),
  user_id uuid,
  name text not null,
  summary text not null default '',
  backend text not null default 'boltz',
  use_msa boolean not null default true,
  protein_sequence text not null default '',
  ligand_smiles text not null default '',
  color_mode text not null default 'alphafold',
  task_type text not null default 'Boltz-2 Prediction',
  task_id text not null default '',
  task_state text not null default 'DRAFT',
  status_text text not null default 'Ready for input',
  error_text text not null default '',
  confidence jsonb not null default '{}'::jsonb,
  affinity jsonb not null default '{}'::jsonb,
  submitted_at timestamptz,
  completed_at timestamptz,
  duration_seconds double precision,
  structure_name text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  deleted_at timestamptz
);

alter table public.projects add column if not exists user_id uuid;
alter table public.projects alter column backend set default 'boltz';

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'projects_user_id_fkey'
      AND conrelid = 'public.projects'::regclass
  ) THEN
    ALTER TABLE public.projects
      ADD CONSTRAINT projects_user_id_fkey
      FOREIGN KEY (user_id)
      REFERENCES public.app_users(id)
      ON DELETE CASCADE;
  END IF;
END
$$;

create index if not exists idx_projects_user_id on public.projects (user_id);
create index if not exists idx_projects_updated_at on public.projects (updated_at desc);
create index if not exists idx_projects_deleted_at on public.projects (deleted_at);

create or replace function public.set_updated_at()
returns trigger as $$
begin
  NEW.updated_at = now();
  return NEW;
end;
$$ language plpgsql;

drop trigger if exists trg_app_users_updated_at on public.app_users;
create trigger trg_app_users_updated_at
before update on public.app_users
for each row
execute procedure public.set_updated_at();

drop trigger if exists trg_projects_updated_at on public.projects;
create trigger trg_projects_updated_at
before update on public.projects
for each row
execute procedure public.set_updated_at();

alter table public.app_users enable row level security;
alter table public.projects enable row level security;

drop policy if exists app_users_anon_select on public.app_users;
drop policy if exists app_users_anon_insert on public.app_users;
drop policy if exists app_users_anon_update on public.app_users;
drop policy if exists app_users_anon_delete on public.app_users;

create policy app_users_anon_select
on public.app_users
for select
to anon
using (true);

create policy app_users_anon_insert
on public.app_users
for insert
to anon
with check (true);

create policy app_users_anon_update
on public.app_users
for update
to anon
using (true)
with check (true);

create policy app_users_anon_delete
on public.app_users
for delete
to anon
using (true);

drop policy if exists projects_anon_select on public.projects;
drop policy if exists projects_anon_insert on public.projects;
drop policy if exists projects_anon_update on public.projects;
drop policy if exists projects_anon_delete on public.projects;

create policy projects_anon_select
on public.projects
for select
to anon
using (true);

create policy projects_anon_insert
on public.projects
for insert
to anon
with check (true);

create policy projects_anon_update
on public.projects
for update
to anon
using (true)
with check (true);

create policy projects_anon_delete
on public.projects
for delete
to anon
using (true);

grant usage on schema public to anon, authenticated, service_role;
grant select, insert, update, delete on public.app_users to anon, authenticated, service_role;
grant select, insert, update, delete on public.projects to anon, authenticated, service_role;
