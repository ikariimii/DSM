-- v1.7 Lakebase ownership / append-only validator (QA-045b residual risks). READ ONLY.
-- Returns one row per violation (check, detail); ZERO rows = pass. Run after lakebase/001_app_schema.sql on every
-- deploy, as any role that can read the catalogs:
--   PGOPTIONS='-c dam.app_role=<app SP role>' psql "$DSN" -X -At -v ON_ERROR_STOP=1 -f lakebase/validate_append_only.sql
-- or tools/validate_lakebase.py (fails the step on any row). Needs PostgreSQL >= 15 (has_parameter_privilege).
-- Split ownership (see lakebase/001): the app role legitimately owns the MUTABLE app tables and has CREATE on the
-- schema; only the schema, the three append-only tables and the trigger functions must be owner-owned.
-- Fails if: the owner role is missing or can log in / is superuser / replication / bypassrls; the schema, an
-- append-only table or a trigger function is not owned by dam_lakebase_owner; the app role is missing, superuser,
-- REPLICATION, BYPASSRLS, a member of dam_lakebase_owner or of any owner of those objects, holds TRUNCATE, UPDATE
-- (table or column), DELETE or TRIGGER on an append-only table, may SET
-- session_replication_role, or has it preset; a guard trigger is missing, not on forbid_mutation(), or not ENABLE
-- ALWAYS ('A'); any trigger on an append-only table is disabled (pg_trigger.tgenabled = 'D').
-- v1.7 publication outboxes (ref_change_publication, onboarding_publication, dssmp_registration_outbox): not owned
-- by dam_lakebase_owner, or the app holds anything beyond SELECT / INSERT on them; dam_lakebase_publisher missing,
-- able to log in / privileged, holding anything beyond USAGE on the schema, SELECT and UPDATE (status, message,
-- applied_at) on those three tables (any other table, column, sequence, INSERT, DELETE, TRUNCATE, TRIGGER,
-- REFERENCES), a member of the owner, or named in a default ACL; the app role a member of the publisher role. With
-- SET dam.extract_role: the extract role holding anything beyond SELECT (plus INSERT / UPDATE on extract_watermark),
-- or no default ACL giving it SELECT on future tables of dam_lakebase_owner.
WITH cfg AS (SELECT nullif(current_setting('dam.app_role', true), '') AS app,
                    'dam_lakebase_owner'::text AS own),
guarded(t) AS (VALUES ('exceedance_status'), ('review_signoff'), ('manual_reading')),
gt AS (SELECT g.t, c.oid, c.relowner FROM guarded g
       LEFT JOIN pg_class c ON c.relname = g.t AND c.relnamespace = to_regnamespace('dam_app')),
want(t, tg, kind) AS (SELECT t, 't_noupd_' || t, 'row' FROM guarded UNION ALL SELECT t, 't_notrunc_' || t, 'truncate' FROM guarded),
app AS (SELECT r.* FROM cfg JOIN pg_roles r ON r.rolname = cfg.app),
pub AS (SELECT r.* FROM pg_roles r WHERE r.rolname = 'dam_lakebase_publisher'),
ext AS (SELECT r.* FROM pg_roles r WHERE r.rolname = nullif(current_setting('dam.extract_role', true), '')),
outbox(t) AS (VALUES ('ref_change_publication'), ('onboarding_publication'), ('dssmp_registration_outbox')),
ot AS (SELECT o.t, c.oid, c.relowner FROM outbox o
       LEFT JOIN pg_class c ON c.relname = o.t AND c.relnamespace = to_regnamespace('dam_app')),
tabs AS (SELECT c.oid, c.relname FROM pg_class c WHERE c.relnamespace = to_regnamespace('dam_app')
         AND c.relkind IN ('r', 'p', 'v', 'm')),
cols AS (SELECT a.attrelid, a.attname FROM pg_attribute a JOIN tabs ON tabs.oid = a.attrelid
         WHERE a.attnum > 0 AND NOT a.attisdropped),
allp(p) AS (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')),
own AS (SELECT r.* FROM cfg JOIN pg_roles r ON r.rolname = cfg.own),
privs(p) AS (VALUES ('TRUNCATE'), ('UPDATE'), ('DELETE'), ('TRIGGER'))
SELECT * FROM (
  SELECT 'owner_role_missing' AS check, 'role dam_lakebase_owner does not exist' AS detail
    WHERE NOT EXISTS (SELECT 1 FROM own)
  UNION ALL SELECT 'owner_role_attrs', 'dam_lakebase_owner must be NOLOGIN NOSUPERUSER NOREPLICATION NOBYPASSRLS'
    FROM own WHERE rolcanlogin OR rolsuper OR rolreplication OR rolbypassrls
  UNION ALL SELECT 'schema_missing', 'schema dam_app does not exist' WHERE to_regnamespace('dam_app') IS NULL
  UNION ALL SELECT 'schema_owner', 'dam_app owned by ' || n.nspowner::regrole::text
    FROM pg_namespace n WHERE n.nspname = 'dam_app' AND n.nspowner::regrole::text <> 'dam_lakebase_owner'
  UNION ALL SELECT 'table_missing', 'dam_app.' || t FROM gt WHERE oid IS NULL
  UNION ALL SELECT 'table_owner', 'dam_app.' || t || ' owned by ' || relowner::regrole::text
    FROM gt WHERE oid IS NOT NULL AND relowner::regrole::text <> 'dam_lakebase_owner'
  UNION ALL SELECT 'function_owner', p.oid::regprocedure::text || ' owned by ' || p.proowner::regrole::text
    FROM pg_proc p WHERE p.pronamespace = to_regnamespace('dam_app')
     AND p.proname IN ('forbid_mutation', 'app_forbid_mutation') AND p.proowner::regrole::text <> 'dam_lakebase_owner'
  UNION ALL SELECT 'guard_trigger_missing', w.tg || ' on dam_app.' || w.t
    FROM want w JOIN gt ON gt.t = w.t WHERE gt.oid IS NOT NULL AND NOT EXISTS (
      SELECT 1 FROM pg_trigger tr WHERE tr.tgrelid = gt.oid AND tr.tgname = w.tg AND NOT tr.tgisinternal
        AND tr.tgfoid = to_regprocedure('dam_app.forbid_mutation()')
        AND ((w.kind = 'row' AND (tr.tgtype & 1) = 1 AND (tr.tgtype & 24) = 24)      -- ROW, DELETE+UPDATE
          OR (w.kind = 'truncate' AND (tr.tgtype & 1) = 0 AND (tr.tgtype & 32) = 32)))  -- STATEMENT, TRUNCATE
  UNION ALL SELECT 'guard_trigger_not_always', w.tg || ' tgenabled=' || tr.tgenabled::text
    FROM want w JOIN gt ON gt.t = w.t JOIN pg_trigger tr ON tr.tgrelid = gt.oid AND tr.tgname = w.tg
    WHERE tr.tgenabled <> 'A'
  UNION ALL SELECT 'trigger_disabled', tr.tgname || ' on dam_app.' || gt.t
    FROM gt JOIN pg_trigger tr ON tr.tgrelid = gt.oid WHERE NOT tr.tgisinternal AND tr.tgenabled = 'D'
  UNION ALL SELECT 'app_role_unset', 'SET dam.app_role to the app service principal''s Postgres role'
    FROM cfg WHERE app IS NULL
  UNION ALL SELECT 'app_role_missing', 'role ' || app || ' does not exist'
    FROM cfg WHERE app IS NOT NULL AND NOT EXISTS (SELECT 1 FROM app)
  UNION ALL SELECT 'app_role_attrs', rolname || ': ' || concat_ws(' ', CASE WHEN rolsuper THEN 'SUPERUSER' END,
      CASE WHEN rolreplication THEN 'REPLICATION' END, CASE WHEN rolbypassrls THEN 'BYPASSRLS' END)
    FROM app WHERE rolsuper OR rolreplication OR rolbypassrls
  UNION ALL SELECT 'app_member_of_owner', app.rolname || ' is a member of dam_lakebase_owner'
    FROM app, own WHERE pg_has_role(app.oid, own.oid, 'MEMBER')
  UNION ALL SELECT 'app_owns', app.rolname || ' owns or is a member of the owner of dam_app.' || gt.t
    FROM app, gt WHERE gt.oid IS NOT NULL AND pg_has_role(app.oid, gt.relowner, 'MEMBER')
  UNION ALL SELECT 'app_owns', app.rolname || ' owns or is a member of the owner of schema dam_app'
    FROM app, pg_namespace n WHERE n.nspname = 'dam_app' AND pg_has_role(app.oid, n.nspowner, 'MEMBER')
  UNION ALL SELECT 'app_owns', app.rolname || ' owns or is a member of the owner of ' || p.oid::regprocedure::text
    FROM app, pg_proc p WHERE p.pronamespace = to_regnamespace('dam_app')
     AND p.proname IN ('forbid_mutation', 'app_forbid_mutation') AND pg_has_role(app.oid, p.proowner, 'MEMBER')
  UNION ALL SELECT 'app_privilege', app.rolname || ' has ' || privs.p || ' on dam_app.' || gt.t
    FROM app, gt, privs WHERE gt.oid IS NOT NULL AND has_table_privilege(app.oid, gt.oid, privs.p)
  UNION ALL SELECT 'app_privilege', app.rolname || ' has column UPDATE on dam_app.' || gt.t
    FROM app, gt WHERE gt.oid IS NOT NULL AND NOT has_table_privilege(app.oid, gt.oid, 'UPDATE')
     AND has_any_column_privilege(app.oid, gt.oid, 'UPDATE')
  UNION ALL SELECT 'app_can_set_replication_role', app.rolname || ' may SET session_replication_role'
    FROM app WHERE has_parameter_privilege(app.oid, 'session_replication_role', 'SET')
  UNION ALL SELECT 'app_preset_replication_role', 'pg_db_role_setting presets session_replication_role'
    FROM app, pg_db_role_setting s WHERE s.setrole IN (app.oid, 0)
     AND array_to_string(s.setconfig, ',') ILIKE '%session_replication_role%'
  UNION ALL SELECT 'outbox_missing', 'dam_app.' || t FROM ot WHERE oid IS NULL
  UNION ALL SELECT 'outbox_owner', 'dam_app.' || t || ' owned by ' || relowner::regrole::text
    FROM ot WHERE oid IS NOT NULL AND relowner::regrole::text <> 'dam_lakebase_owner'
  UNION ALL SELECT 'app_outbox_privilege', app.rolname || ' has ' || allp.p || ' on dam_app.' || ot.t
    FROM app, ot, allp WHERE ot.oid IS NOT NULL AND allp.p NOT IN ('SELECT', 'INSERT')
     AND has_table_privilege(app.oid, ot.oid, allp.p)
  UNION ALL SELECT 'app_outbox_privilege', app.rolname || ' has column UPDATE on dam_app.' || ot.t
    FROM app, ot WHERE ot.oid IS NOT NULL AND has_any_column_privilege(app.oid, ot.oid, 'UPDATE')
  UNION ALL SELECT 'publisher_role_missing', 'role dam_lakebase_publisher does not exist'
    WHERE NOT EXISTS (SELECT 1 FROM pub)
  UNION ALL SELECT 'publisher_role_attrs', 'dam_lakebase_publisher must be NOLOGIN and unprivileged'
    FROM pub WHERE rolcanlogin OR rolsuper OR rolreplication OR rolbypassrls OR rolcreaterole OR rolcreatedb
  UNION ALL SELECT 'publisher_member_of_owner', 'dam_lakebase_publisher is a member of dam_lakebase_owner'
    FROM pub, own WHERE pg_has_role(pub.oid, own.oid, 'MEMBER')
  UNION ALL SELECT 'app_member_of_publisher', app.rolname || ' is a member of dam_lakebase_publisher'
    FROM app, pub WHERE pg_has_role(app.oid, pub.oid, 'MEMBER')
  UNION ALL SELECT 'publisher_privilege', 'dam_lakebase_publisher has ' || allp.p || ' on dam_app.' || tabs.relname
    FROM pub, tabs, allp WHERE has_table_privilege(pub.oid, tabs.oid, allp.p)
     AND NOT (allp.p = 'SELECT' AND tabs.relname IN (SELECT t FROM outbox))
  UNION ALL SELECT 'publisher_privilege', 'dam_lakebase_publisher has column ' || cp.p || ' on dam_app.'
      || tabs.relname || '.' || cols.attname
    FROM pub, tabs JOIN cols ON cols.attrelid = tabs.oid, (VALUES ('INSERT'), ('UPDATE'), ('REFERENCES')) cp(p)
    WHERE has_column_privilege(pub.oid, tabs.oid, cols.attname, cp.p)
     AND NOT (cp.p = 'UPDATE' AND tabs.relname IN (SELECT t FROM outbox)
              AND cols.attname IN ('status', 'message', 'applied_at'))
  UNION ALL SELECT 'publisher_privilege', 'dam_lakebase_publisher lacks UPDATE (' || c.col || ') on dam_app.' || ot.t
    FROM pub, ot, (VALUES ('status'), ('message'), ('applied_at')) c(col)
    WHERE ot.oid IS NOT NULL AND NOT has_column_privilege(pub.oid, ot.oid, c.col, 'UPDATE')
  UNION ALL SELECT 'publisher_privilege', 'dam_lakebase_publisher has a privilege on sequence ' || c.relname
    FROM pub, pg_class c WHERE c.relnamespace = to_regnamespace('dam_app') AND c.relkind = 'S'
     AND (has_sequence_privilege(pub.oid, c.oid, 'USAGE') OR has_sequence_privilege(pub.oid, c.oid, 'UPDATE'))
  UNION ALL SELECT 'publisher_privilege', 'dam_lakebase_publisher may CREATE in schema dam_app'
    FROM pub WHERE has_schema_privilege(pub.oid, 'dam_app', 'CREATE')
  UNION ALL SELECT 'publisher_default_privilege', 'a default ACL grants dam_lakebase_publisher: '
      || array_to_string(d.defaclacl, ',')
    FROM pub, pg_default_acl d WHERE array_to_string(d.defaclacl, ',') ~ '(^|,)dam_lakebase_publisher='
  UNION ALL SELECT 'extract_privilege', ext.rolname || ' has ' || allp.p || ' on dam_app.' || tabs.relname
    FROM ext, tabs, allp WHERE allp.p <> 'SELECT' AND has_table_privilege(ext.oid, tabs.oid, allp.p)
     AND NOT (tabs.relname = 'extract_watermark' AND allp.p IN ('INSERT', 'UPDATE'))
  UNION ALL SELECT 'extract_privilege', ext.rolname || ' has column UPDATE on dam_app.' || tabs.relname
    FROM ext, tabs WHERE tabs.relname <> 'extract_watermark' AND has_any_column_privilege(ext.oid, tabs.oid, 'UPDATE')
  UNION ALL SELECT 'extract_default_select_missing', 'no ALTER DEFAULT PRIVILEGES FOR ROLE dam_lakebase_owner IN '
      || 'SCHEMA dam_app GRANT SELECT ON TABLES TO ' || ext.rolname
    FROM ext WHERE NOT EXISTS (SELECT 1 FROM pg_default_acl d, own WHERE d.defaclrole = own.oid
      AND d.defaclnamespace = to_regnamespace('dam_app') AND d.defaclobjtype = 'r'
      AND array_to_string(d.defaclacl, ',') ~ ('(^|,)"?' || ext.rolname || '"?=r/'))
) v ORDER BY 1, 2;
