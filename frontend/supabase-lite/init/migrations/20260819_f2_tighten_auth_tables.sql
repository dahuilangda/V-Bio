-- F2 migration: drop anonymous access to the two sensitive tables.
-- app_users  -> anon keeps a COLUMN-LIMITED read (display joins); no anon writes, no
--                password_hash, no is_admin, no email.
-- api_tokens -> anon gets NOTHING (the management API reads/writes as service_role).
-- service_role (the management API via its signed JWT) keeps full access.
-- Rollback: 20260819_f2_rollback.sql

-- ── app_users ────────────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS app_users_anon_select ON app_users;
DROP POLICY IF EXISTS app_users_anon_insert ON app_users;
DROP POLICY IF EXISTS app_users_anon_update ON app_users;
DROP POLICY IF EXISTS app_users_anon_delete ON app_users;

CREATE POLICY app_users_service_all ON app_users
  FOR ALL TO service_role USING (true) WITH CHECK (true);

REVOKE ALL ON app_users FROM anon;
GRANT SELECT (id, username, name, avatar_url, created_at, last_login_at) ON app_users TO anon;

-- ── api_tokens ───────────────────────────────────────────────────────────────────────────
DROP POLICY IF EXISTS api_tokens_anon_select ON api_tokens;
DROP POLICY IF EXISTS api_tokens_anon_insert ON api_tokens;
DROP POLICY IF EXISTS api_tokens_anon_update ON api_tokens;
DROP POLICY IF EXISTS api_tokens_anon_delete ON api_tokens;

CREATE POLICY api_tokens_service_all ON api_tokens
  FOR ALL TO service_role USING (true) WITH CHECK (true);

REVOKE ALL ON api_tokens FROM anon;

-- Column grants alone yield empty results under RLS: allow the SELECT, the column grant
-- is the actual restriction.
-- The policy — not the client — owns the "hide deactivated users" invariant: anon holds no
-- SELECT grant on deleted_at, so any client-side `deleted_at IS NULL` filter would fail with
-- permission denied (Postgres requires SELECT privilege on columns referenced in WHERE).
CREATE POLICY app_users_anon_display ON app_users FOR SELECT TO anon USING (deleted_at IS NULL);
