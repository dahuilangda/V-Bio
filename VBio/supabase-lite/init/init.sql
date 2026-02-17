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

create table if not exists public.project_tasks (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null,
  name text not null default '',
  summary text not null default '',
  task_id text not null default '',
  task_state text not null default 'DRAFT',
  status_text text not null default 'Ready for input',
  error_text text not null default '',
  backend text not null default 'boltz',
  seed integer,
  protein_sequence text not null default '',
  ligand_smiles text not null default '',
  components jsonb not null default '[]'::jsonb,
  constraints jsonb not null default '[]'::jsonb,
  properties jsonb not null default '{}'::jsonb,
  confidence jsonb not null default '{}'::jsonb,
  affinity jsonb not null default '{}'::jsonb,
  structure_name text not null default '',
  submitted_at timestamptz,
  completed_at timestamptz,
  duration_seconds double precision,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.projects add column if not exists user_id uuid;
alter table public.projects alter column backend set default 'boltz';
alter table public.project_tasks add column if not exists project_id uuid;
alter table public.project_tasks add column if not exists name text;
alter table public.project_tasks add column if not exists summary text;
alter table public.project_tasks add column if not exists task_id text;
alter table public.project_tasks add column if not exists task_state text;
alter table public.project_tasks add column if not exists status_text text;
alter table public.project_tasks add column if not exists error_text text;
alter table public.project_tasks add column if not exists backend text;
alter table public.project_tasks add column if not exists seed integer;
alter table public.project_tasks add column if not exists protein_sequence text;
alter table public.project_tasks add column if not exists ligand_smiles text;
alter table public.project_tasks add column if not exists components jsonb;
alter table public.project_tasks add column if not exists constraints jsonb;
alter table public.project_tasks add column if not exists properties jsonb;
alter table public.project_tasks add column if not exists confidence jsonb;
alter table public.project_tasks add column if not exists affinity jsonb;
alter table public.project_tasks add column if not exists structure_name text;
alter table public.project_tasks add column if not exists submitted_at timestamptz;
alter table public.project_tasks add column if not exists completed_at timestamptz;
alter table public.project_tasks add column if not exists duration_seconds double precision;
alter table public.project_tasks add column if not exists created_at timestamptz;
alter table public.project_tasks add column if not exists updated_at timestamptz;
alter table public.project_tasks alter column project_id set not null;
alter table public.project_tasks alter column name set default '';
alter table public.project_tasks alter column summary set default '';
alter table public.project_tasks alter column task_id set default '';
alter table public.project_tasks alter column task_state set default 'DRAFT';
alter table public.project_tasks alter column status_text set default 'Ready for input';
alter table public.project_tasks alter column error_text set default '';
alter table public.project_tasks alter column backend set default 'boltz';
alter table public.project_tasks alter column protein_sequence set default '';
alter table public.project_tasks alter column ligand_smiles set default '';
alter table public.project_tasks alter column components set default '[]'::jsonb;
alter table public.project_tasks alter column constraints set default '[]'::jsonb;
alter table public.project_tasks alter column properties set default '{}'::jsonb;
alter table public.project_tasks alter column confidence set default '{}'::jsonb;
alter table public.project_tasks alter column affinity set default '{}'::jsonb;
alter table public.project_tasks alter column structure_name set default '';
alter table public.project_tasks alter column created_at set default now();
alter table public.project_tasks alter column updated_at set default now();

update public.project_tasks set task_id = '' where task_id is null;
update public.project_tasks set name = '' where name is null;
update public.project_tasks set summary = '' where summary is null;
update public.project_tasks set task_state = 'DRAFT' where task_state is null or task_state = '';
update public.project_tasks set status_text = 'Ready for input' where status_text is null;
update public.project_tasks set error_text = '' where error_text is null;
update public.project_tasks set backend = 'boltz' where backend is null or backend = '';
update public.project_tasks set protein_sequence = '' where protein_sequence is null;
update public.project_tasks set ligand_smiles = '' where ligand_smiles is null;
update public.project_tasks set components = '[]'::jsonb where components is null;
update public.project_tasks set constraints = '[]'::jsonb where constraints is null;
update public.project_tasks set properties = '{}'::jsonb where properties is null;
update public.project_tasks set confidence = '{}'::jsonb where confidence is null;
update public.project_tasks set affinity = '{}'::jsonb where affinity is null;
update public.project_tasks set structure_name = '' where structure_name is null;
update public.project_tasks set created_at = now() where created_at is null;
update public.project_tasks set updated_at = now() where updated_at is null;
-- Compact oversized confidence payloads. Full PAE matrices are not needed by current UI views.
update public.project_tasks
set confidence = confidence - 'pae'
where jsonb_typeof(confidence->'pae') = 'array';

alter table public.project_tasks alter column task_id set not null;
alter table public.project_tasks alter column name set not null;
alter table public.project_tasks alter column summary set not null;
alter table public.project_tasks alter column task_state set not null;
alter table public.project_tasks alter column status_text set not null;
alter table public.project_tasks alter column error_text set not null;
alter table public.project_tasks alter column backend set not null;
alter table public.project_tasks alter column protein_sequence set not null;
alter table public.project_tasks alter column ligand_smiles set not null;
alter table public.project_tasks alter column components set not null;
alter table public.project_tasks alter column constraints set not null;
alter table public.project_tasks alter column properties set not null;
alter table public.project_tasks alter column confidence set not null;
alter table public.project_tasks alter column affinity set not null;
alter table public.project_tasks alter column structure_name set not null;
alter table public.project_tasks alter column created_at set not null;
alter table public.project_tasks alter column updated_at set not null;

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

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'project_tasks_project_id_fkey'
      AND conrelid = 'public.project_tasks'::regclass
  ) THEN
    ALTER TABLE public.project_tasks
      ADD CONSTRAINT project_tasks_project_id_fkey
      FOREIGN KEY (project_id)
      REFERENCES public.projects(id)
      ON DELETE CASCADE;
  END IF;
END
$$;

create table if not exists public.api_tokens (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  name text not null default '',
  token_hash text not null,
  token_plain text not null default '',
  token_prefix text not null default '',
  token_last4 text not null default '',
  project_id uuid,
  allow_submit boolean not null default true,
  allow_delete boolean not null default false,
  allow_cancel boolean not null default true,
  scopes jsonb not null default '[]'::jsonb,
  is_active boolean not null default true,
  last_used_at timestamptz,
  expires_at timestamptz,
  revoked_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.api_tokens add column if not exists user_id uuid;
alter table public.api_tokens add column if not exists name text;
alter table public.api_tokens add column if not exists token_hash text;
alter table public.api_tokens add column if not exists token_plain text;
alter table public.api_tokens add column if not exists token_prefix text;
alter table public.api_tokens add column if not exists token_last4 text;
alter table public.api_tokens add column if not exists project_id uuid;
alter table public.api_tokens add column if not exists allow_submit boolean;
alter table public.api_tokens add column if not exists allow_delete boolean;
alter table public.api_tokens add column if not exists allow_cancel boolean;
alter table public.api_tokens add column if not exists scopes jsonb;
alter table public.api_tokens add column if not exists is_active boolean;
alter table public.api_tokens add column if not exists last_used_at timestamptz;
alter table public.api_tokens add column if not exists expires_at timestamptz;
alter table public.api_tokens add column if not exists revoked_at timestamptz;
alter table public.api_tokens add column if not exists created_at timestamptz;
alter table public.api_tokens add column if not exists updated_at timestamptz;

update public.api_tokens set name = 'Token' where name is null or name = '';
update public.api_tokens set token_hash = encode(gen_random_bytes(32), 'hex') where token_hash is null or token_hash = '';
update public.api_tokens set token_plain = '' where token_plain is null;
update public.api_tokens set token_prefix = substring(token_hash from 1 for 12) where token_prefix is null or token_prefix = '';
update public.api_tokens set token_last4 = right(token_hash, 4) where token_last4 is null or token_last4 = '';
update public.api_tokens set allow_submit = true where allow_submit is null;
update public.api_tokens set allow_delete = false where allow_delete is null;
update public.api_tokens set allow_cancel = true where allow_cancel is null;
update public.api_tokens set scopes = '[]'::jsonb where scopes is null;
update public.api_tokens set is_active = true where is_active is null;
update public.api_tokens set created_at = now() where created_at is null;
update public.api_tokens set updated_at = now() where updated_at is null;
delete from public.api_tokens where user_id is null;

alter table public.api_tokens alter column user_id set not null;
alter table public.api_tokens alter column name set not null;
alter table public.api_tokens alter column token_hash set not null;
alter table public.api_tokens alter column token_plain set not null;
alter table public.api_tokens alter column token_prefix set not null;
alter table public.api_tokens alter column token_last4 set not null;
alter table public.api_tokens alter column allow_submit set not null;
alter table public.api_tokens alter column allow_delete set not null;
alter table public.api_tokens alter column allow_cancel set not null;
alter table public.api_tokens alter column scopes set not null;
alter table public.api_tokens alter column is_active set not null;
alter table public.api_tokens alter column created_at set not null;
alter table public.api_tokens alter column updated_at set not null;
alter table public.api_tokens alter column name set default '';
alter table public.api_tokens alter column token_plain set default '';
alter table public.api_tokens alter column token_prefix set default '';
alter table public.api_tokens alter column token_last4 set default '';
alter table public.api_tokens alter column allow_submit set default true;
alter table public.api_tokens alter column allow_delete set default false;
alter table public.api_tokens alter column allow_cancel set default true;
alter table public.api_tokens alter column scopes set default '[]'::jsonb;
alter table public.api_tokens alter column is_active set default true;
alter table public.api_tokens alter column created_at set default now();
alter table public.api_tokens alter column updated_at set default now();

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'api_tokens_project_id_fkey'
      AND conrelid = 'public.api_tokens'::regclass
  ) THEN
    ALTER TABLE public.api_tokens
      ADD CONSTRAINT api_tokens_project_id_fkey
      FOREIGN KEY (project_id)
      REFERENCES public.projects(id)
      ON DELETE SET NULL;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'api_tokens_user_id_fkey'
      AND conrelid = 'public.api_tokens'::regclass
  ) THEN
    ALTER TABLE public.api_tokens
      ADD CONSTRAINT api_tokens_user_id_fkey
      FOREIGN KEY (user_id)
      REFERENCES public.app_users(id)
      ON DELETE CASCADE;
  END IF;
END
$$;

create unique index if not exists idx_api_tokens_token_hash on public.api_tokens (token_hash);
create index if not exists idx_api_tokens_user_id on public.api_tokens (user_id, created_at desc);
create index if not exists idx_api_tokens_active on public.api_tokens (is_active, created_at desc);
create index if not exists idx_api_tokens_project_id on public.api_tokens (project_id, created_at desc);

create table if not exists public.api_token_usage (
  id uuid primary key default gen_random_uuid(),
  token_id uuid,
  user_id uuid,
  method text not null default '',
  path text not null default '',
  action text not null default '',
  status_code integer not null default 0,
  succeeded boolean not null default false,
  duration_ms integer,
  client text not null default '',
  meta jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

alter table public.api_token_usage add column if not exists token_id uuid;
alter table public.api_token_usage add column if not exists user_id uuid;
alter table public.api_token_usage add column if not exists method text;
alter table public.api_token_usage add column if not exists path text;
alter table public.api_token_usage add column if not exists action text;
alter table public.api_token_usage add column if not exists status_code integer;
alter table public.api_token_usage add column if not exists succeeded boolean;
alter table public.api_token_usage add column if not exists duration_ms integer;
alter table public.api_token_usage add column if not exists client text;
alter table public.api_token_usage add column if not exists meta jsonb;
alter table public.api_token_usage add column if not exists created_at timestamptz;

update public.api_token_usage set method = '' where method is null;
update public.api_token_usage set path = '' where path is null;
update public.api_token_usage set action = '' where action is null;
update public.api_token_usage set status_code = 0 where status_code is null;
update public.api_token_usage set succeeded = false where succeeded is null;
update public.api_token_usage set client = '' where client is null;
update public.api_token_usage set meta = '{}'::jsonb where meta is null;
update public.api_token_usage set created_at = now() where created_at is null;

alter table public.api_token_usage alter column method set not null;
alter table public.api_token_usage alter column path set not null;
alter table public.api_token_usage alter column action set not null;
alter table public.api_token_usage alter column status_code set not null;
alter table public.api_token_usage alter column succeeded set not null;
alter table public.api_token_usage alter column client set not null;
alter table public.api_token_usage alter column meta set not null;
alter table public.api_token_usage alter column created_at set not null;
alter table public.api_token_usage alter column method set default '';
alter table public.api_token_usage alter column path set default '';
alter table public.api_token_usage alter column action set default '';
alter table public.api_token_usage alter column status_code set default 0;
alter table public.api_token_usage alter column succeeded set default false;
alter table public.api_token_usage alter column client set default '';
alter table public.api_token_usage alter column meta set default '{}'::jsonb;
alter table public.api_token_usage alter column created_at set default now();

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'api_token_usage_token_id_fkey'
      AND conrelid = 'public.api_token_usage'::regclass
  ) THEN
    ALTER TABLE public.api_token_usage
      ADD CONSTRAINT api_token_usage_token_id_fkey
      FOREIGN KEY (token_id)
      REFERENCES public.api_tokens(id)
      ON DELETE SET NULL;
  END IF;
END
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'api_token_usage_user_id_fkey'
      AND conrelid = 'public.api_token_usage'::regclass
  ) THEN
    ALTER TABLE public.api_token_usage
      ADD CONSTRAINT api_token_usage_user_id_fkey
      FOREIGN KEY (user_id)
      REFERENCES public.app_users(id)
      ON DELETE SET NULL;
  END IF;
END
$$;

create index if not exists idx_api_token_usage_token_id_created_at on public.api_token_usage (token_id, created_at desc);
create index if not exists idx_api_token_usage_created_at on public.api_token_usage (created_at desc);

create index if not exists idx_projects_user_id on public.projects (user_id);
create index if not exists idx_projects_updated_at on public.projects (updated_at desc);
create index if not exists idx_projects_deleted_at on public.projects (deleted_at);
create index if not exists idx_project_tasks_project_id on public.project_tasks (project_id, created_at desc);
create index if not exists idx_project_tasks_task_id on public.project_tasks (task_id);

create or replace function public.strip_task_components_for_storage(input jsonb)
returns jsonb
language sql
immutable
as $$
  select
    case
      when jsonb_typeof(input) = 'array' then (
        select coalesce(
          jsonb_agg(
            case
              when jsonb_typeof(compact_snake.elem->'templateUpload') = 'object'
                then jsonb_set(
                  compact_snake.elem,
                  '{templateUpload}',
                  (compact_snake.elem->'templateUpload') - 'content',
                  true
                )
              else compact_snake.elem
            end
          ),
          '[]'::jsonb
        )
        from (
          select
            case
              when jsonb_typeof(elem->'template_upload') = 'object'
                then jsonb_set(elem, '{template_upload}', (elem->'template_upload') - 'content', true)
              else elem
            end as elem
          from jsonb_array_elements(input) as elem
        ) as compact_snake
      )
      else '[]'::jsonb
    end;
$$;

-- Keep task snapshot metadata, strip bulky template text, keep affinity upload content for Components recovery.
update public.project_tasks
set components = public.strip_task_components_for_storage(components)
where public.strip_task_components_for_storage(components) is distinct from components;

create or replace function public.strip_task_components_for_list(input jsonb)
returns jsonb
language sql
immutable
as $$
  select
    case
      when jsonb_typeof(input) = 'array' then (
        select coalesce(
          jsonb_agg(
            (
              case
                when coalesce(elem->>'id', '') in ('__affinity_target_upload__', '__affinity_ligand_upload__')
                  or jsonb_typeof(elem->'affinityUpload') = 'object'
                  or jsonb_typeof(elem->'affinity_upload') = 'object'
                  then jsonb_set(elem, '{sequence}', to_jsonb(''::text), true)
                else elem
              end
            ) - 'templateUpload' - 'template_upload' - 'affinityUpload' - 'affinity_upload'
          ),
          '[]'::jsonb
        )
        from jsonb_array_elements(input) as elem
      )
      else '[]'::jsonb
    end;
$$;

create or replace function public.project_tasks_compact_components_for_storage()
returns trigger as $$
begin
  NEW.components = public.strip_task_components_for_storage(NEW.components);
  return NEW;
end;
$$ language plpgsql;

drop view if exists public.project_tasks_list;
create or replace view public.project_tasks_list as
select
  id,
  project_id,
  name,
  summary,
  task_id,
  task_state,
  status_text,
  error_text,
  backend,
  seed,
  protein_sequence,
  ligand_smiles,
  public.strip_task_components_for_list(components) as components,
  properties,
  confidence,
  structure_name,
  submitted_at,
  completed_at,
  duration_seconds,
  created_at,
  updated_at
from public.project_tasks;

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

drop trigger if exists trg_project_tasks_updated_at on public.project_tasks;
create trigger trg_project_tasks_updated_at
before update on public.project_tasks
for each row
execute procedure public.set_updated_at();

drop trigger if exists trg_api_tokens_updated_at on public.api_tokens;
create trigger trg_api_tokens_updated_at
before update on public.api_tokens
for each row
execute procedure public.set_updated_at();

drop trigger if exists trg_project_tasks_compact_components on public.project_tasks;
create trigger trg_project_tasks_compact_components
before insert or update of components on public.project_tasks
for each row
execute procedure public.project_tasks_compact_components_for_storage();

alter table public.app_users enable row level security;
alter table public.projects enable row level security;
alter table public.project_tasks enable row level security;
alter table public.api_tokens enable row level security;
alter table public.api_token_usage enable row level security;

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
drop policy if exists project_tasks_anon_select on public.project_tasks;
drop policy if exists project_tasks_anon_insert on public.project_tasks;
drop policy if exists project_tasks_anon_update on public.project_tasks;
drop policy if exists project_tasks_anon_delete on public.project_tasks;
drop policy if exists api_tokens_anon_select on public.api_tokens;
drop policy if exists api_tokens_anon_insert on public.api_tokens;
drop policy if exists api_tokens_anon_update on public.api_tokens;
drop policy if exists api_tokens_anon_delete on public.api_tokens;
drop policy if exists api_token_usage_anon_select on public.api_token_usage;
drop policy if exists api_token_usage_anon_insert on public.api_token_usage;
drop policy if exists api_token_usage_anon_update on public.api_token_usage;
drop policy if exists api_token_usage_anon_delete on public.api_token_usage;

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

create policy project_tasks_anon_select
on public.project_tasks
for select
to anon
using (true);

create policy project_tasks_anon_insert
on public.project_tasks
for insert
to anon
with check (true);

create policy project_tasks_anon_update
on public.project_tasks
for update
to anon
using (true)
with check (true);

create policy project_tasks_anon_delete
on public.project_tasks
for delete
to anon
using (true);

create policy api_tokens_anon_select
on public.api_tokens
for select
to anon
using (true);

create policy api_tokens_anon_insert
on public.api_tokens
for insert
to anon
with check (true);

create policy api_tokens_anon_update
on public.api_tokens
for update
to anon
using (true)
with check (true);

create policy api_tokens_anon_delete
on public.api_tokens
for delete
to anon
using (true);

create policy api_token_usage_anon_select
on public.api_token_usage
for select
to anon
using (true);

create policy api_token_usage_anon_insert
on public.api_token_usage
for insert
to anon
with check (true);

create policy api_token_usage_anon_update
on public.api_token_usage
for update
to anon
using (true)
with check (true);

create policy api_token_usage_anon_delete
on public.api_token_usage
for delete
to anon
using (true);

drop view if exists public.api_token_usage_daily;
create or replace view public.api_token_usage_daily as
select
  token_id,
  date_trunc('day', created_at)::date as usage_day,
  count(*)::bigint as total_count,
  count(*) filter (where succeeded)::bigint as success_count,
  count(*) filter (where not succeeded)::bigint as error_count
from public.api_token_usage
group by token_id, date_trunc('day', created_at)::date;

grant usage on schema public to anon, authenticated, service_role;
grant select, insert, update, delete on public.app_users to anon, authenticated, service_role;
grant select, insert, update, delete on public.projects to anon, authenticated, service_role;
grant select, insert, update, delete on public.project_tasks to anon, authenticated, service_role;
grant select, insert, update, delete on public.api_tokens to anon, authenticated, service_role;
grant select, insert, update, delete on public.api_token_usage to anon, authenticated, service_role;
grant select on public.project_tasks_list to anon, authenticated, service_role;
grant select on public.api_token_usage_daily to anon, authenticated, service_role;
