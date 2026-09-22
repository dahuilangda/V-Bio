-- Self-service password reset: single-use token hash + expiry on the user row.
alter table public.app_users add column if not exists reset_token_hash text;
alter table public.app_users add column if not exists reset_token_expires double precision;
