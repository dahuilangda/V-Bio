-- Auth hardening: reset-request cooldown, session invalidation epoch, unique email among live accounts.
alter table public.app_users add column if not exists reset_requested_at double precision;
alter table public.app_users add column if not exists sessions_valid_after double precision;
create unique index if not exists app_users_email_unique
  on public.app_users (lower(email))
  where email is not null and deleted_at is null;
