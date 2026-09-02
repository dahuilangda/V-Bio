-- Rollback for 20260819_f2_tighten_auth_tables.sql: restore the previous anonymous access.
DROP POLICY IF EXISTS app_users_service_all ON app_users;
DROP POLICY IF EXISTS app_users_anon_display ON app_users;
DROP POLICY IF EXISTS api_tokens_service_all ON api_tokens;

GRANT SELECT, INSERT, UPDATE, DELETE ON app_users TO anon;
CREATE POLICY app_users_anon_select ON app_users FOR SELECT TO anon USING (true);
CREATE POLICY app_users_anon_insert ON app_users FOR INSERT TO anon WITH CHECK (true);
CREATE POLICY app_users_anon_update ON app_users FOR UPDATE TO anon USING (true) WITH CHECK (true);
CREATE POLICY app_users_anon_delete ON app_users FOR DELETE TO anon USING (true);

GRANT SELECT, INSERT, UPDATE, DELETE ON api_tokens TO anon;
CREATE POLICY api_tokens_anon_select ON api_tokens FOR SELECT TO anon USING (true);
CREATE POLICY api_tokens_anon_insert ON api_tokens FOR INSERT TO anon WITH CHECK (true);
CREATE POLICY api_tokens_anon_update ON api_tokens FOR UPDATE TO anon USING (true) WITH CHECK (true);
CREATE POLICY api_tokens_anon_delete ON api_tokens FOR DELETE TO anon USING (true);
