"""
pipeline/db_grants.py

The least-privilege plan for the API's runtime database role (production: `mavlit_app`; the schema owner stays
`mavlit_user`). One source of truth, used three ways:

  * `python -m pipeline.db_grants ...` prints the reviewable SQL script (scripts/sql/runtime_role_grants.sql is that
    output; a test keeps the two identical). It connects to nothing.
  * `python -m pipeline.migrate --grant-role mavlit_app` re-applies the same grants after every migration, so tables,
    sequences and the recreated `creator_llm_cost` view always carry them (apply_grants below).
  * tests/test_db_grants.py and tests/test_runtime_role_postgres.py check the plan against the models, SQLAdmin and a
    real (disposable) PostgreSQL.

Why explicit grants and not ALTER DEFAULT PRIVILEGES: default privileges apply to EVERY future table the owner creates,
including bak_* safety copies and experiment tables made by hand, which would be exposed to the API without anyone deciding
so. Here only the objects named below are granted (each with REVOKE ALL first, so the result is exactly the listed set),
and a new model table that is not classified in this file fails a test.

Granted: SELECT / INSERT / UPDATE / DELETE per table, USAGE on the sequences those tables own, SELECT on the view,
CONNECT on the database, USAGE on the schema. Never granted: ownership, superuser, CREATE, TRUNCATE, REFERENCES, TRIGGER.

Pre-existing state is handled in two ways, never by guessing: the role's OWN direct grants outside the plan (on other tables,
views, sequences, schemas, the database) are revoked, because the plan is the role's complete privilege set; anything the
script must not or cannot undo, inherited access through role membership, PUBLIC grants, default ACLs, objects owned by the role,
elevated role attributes, makes the whole script / grant step FAIL with instructions (audit_sql below). Other roles' grants are
never touched.

A table's privileges are the union of what the routes and background jobs need (CODE below) and what SQLAdmin's views allow
(ADMIN below: SELECT for the list pages, INSERT / UPDATE / DELETE when the view's can_create / can_edit / can_delete is on),
because the admin runs inside the API process under the same role. Making an admin view read-only is how a table's write
privileges can later be taken away.
"""

import argparse
import re
import sys


class GrantAuditError(RuntimeError):
    """The runtime role has (or would keep) access the plan does not allow. The message lists what and how to fix it."""

LETTERS = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE"}
ORDER = "SIUD"

# --- what routes and background jobs (everything that runs inside the API process) need ---------------------------------
# INSERT always comes with SELECT: SQLAlchemy inserts with RETURNING, which needs SELECT on the returned columns.
# Evidence per table: reads and writes found by tracing the 55 modules reachable from api.app, plus the write counters
# production showed in the read-only audit. "" = no runtime route or job touches it (CLI scripts only).
CODE = {
    "apify_usage": "SI",                          # gpt_llm.record_apify_scrape
    "brand_contacts": "SIU",                      # apollo_contacts in the own-brand job; prod: 8312 ins / 8352 upd
    "brand_instagram_users": "SI",                # instagram_users / signals
    "brand_match_profile": "SIU",                 # brand_signals upserts
    "brands_niches": "SIU",                       # shopify_detect
    "brands_raw": "SIU",                          # seed, enrichment, own-brand job
    "content_creator_re": "SIU",                  # /content-creator-re/bulk-add
    "contract_reviews": "SI",                     # contract_advice
    "contact_reveals": "SI",                      # api/contact_access.py: the daily cap on contact reveals
    "creator_brand_llm_rankings": "SID",          # v2 ranking refresh = delete + insert
    "creator_brand_llm_v3_rankings": "SID",       # v3 ranking refresh = delete + insert
    "creator_instagram_posts": "SIUD",            # /creator-profile/me/posts
    "creator_niches": "S",                        # matcher reads; writes are CLI-only
    "creator_profiles": "SIU",                    # auth, profile, ranking status
    "early_access_signups": "SIU",                # POST /early-access (upsert)
    "facebook_branded_content_checks": "",        # CLI only
    "initial_brand_score": "SIU",                 # /score/brands, own-brand job
    "feature_usage": "SIU",                       # api/usage_limits.py: per-user daily caps on LLM / Apify features
    "feature_locks": "SID",                       # api/usage_limits.py: one in-flight pitch per user
    "login_failures": "SID",                      # api/login_limits.py: failed-login lockout (rows removed on success and after a day)
    "instagram_creator_commenters": "S",          # read by the matching; writes are CLI-only
    "instagram_posts": "SIU",                     # own-brand job, sponsorship scoring
    "instagram_users": "SIU",                     # own-brand job
    "llm_usage": "SI",                            # gpt_llm usage tracking
    "meta_ads": "",                               # the step is disabled; CLI only
    "own_brand_jobs": "SIU",                      # POST /own-brands and the job's progress
    "pitches": "SIUD",                            # /pitches
    "prompts": "S",                               # read by every LLM call; seeded by the migrations
    "rate_estimates": "SI",                       # /rate-intelligence
    "saved_brands": "SID",                        # /saved-brands
    "test_brands_with_instagram_posts": "",       # CLI only
    "test_creator_brand_partnership_posts": "SIU",  # partnership evidence read by pitches / matching, written by the own-brand job
    "test_niches": "",                            # CLI only
    "youtube_sponsorships": "SI",                 # youtube_sponsorship signal
}

# --- what SQLAdmin allows (can_create, can_edit, can_delete); None = no admin view ---------------------------------------
# tests/test_db_grants.py compares this with the live ModelView flags, so a change there fails until this is updated.
ADMIN = {
    "apify_usage": None,
    "brand_contacts": (True, True, True),
    "brand_instagram_users": (True, True, True),
    "brand_match_profile": (True, True, True),
    "brands_niches": (True, True, True),
    "brands_raw": (True, True, True),
    "content_creator_re": (True, True, True),
    "contract_reviews": (True, True, True),
    "contact_reveals": None,
    "creator_brand_llm_rankings": None,
    "creator_brand_llm_v3_rankings": None,
    "creator_instagram_posts": (False, False, True),
    "creator_niches": (True, True, True),
    "creator_profiles": (True, True, False),   # admin can create and edit, not delete (api/app.py CreatorProfileAdmin)
    "early_access_signups": (False, True, True),
    "facebook_branded_content_checks": (True, True, True),
    "initial_brand_score": (True, True, True),
    "feature_usage": None,
    "feature_locks": None,
    "login_failures": None,
    "instagram_creator_commenters": (True, True, True),
    "instagram_posts": (True, True, True),
    "instagram_users": (True, True, True),
    "llm_usage": (False, False, False),
    "meta_ads": (True, True, True),
    "own_brand_jobs": (False, False, True),
    "pitches": (True, True, True),
    "prompts": (True, True, True),
    "rate_estimates": (True, True, True),
    "saved_brands": (True, True, True),
    "test_brands_with_instagram_posts": (True, True, True),
    "test_creator_brand_partnership_posts": (True, True, True),
    "test_niches": (True, True, True),
    "youtube_sponsorships": (True, True, True),
}

VIEWS = {"creator_llm_cost": "S"}   # SQL view rebuilt by the migrations; its admin page is read-only

IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def admin_privileges(flags) -> str:
    if flags is None:
        return ""
    create, edit, delete = flags
    return "S" + ("I" if create else "") + ("U" if edit else "") + ("D" if delete else "")


def table_privileges(include_admin: bool = True) -> dict[str, str]:
    """Final privileges per table, as letters in SIUD order."""
    out = {}
    for table, code in CODE.items():
        letters = set(code) | (set(admin_privileges(ADMIN[table])) if include_admin else set())
        out[table] = "".join(c for c in ORDER if c in letters)
    return {t: p for t, p in out.items() if p}


def planned_relations(include_admin: bool = True) -> dict[str, str]:
    """Tables and views in the plan, with their privilege letters."""
    return {**table_privileges(include_admin), **VIEWS}


def validate_identifier(name: str, what: str = "role") -> str:
    if not isinstance(name, str) or not IDENTIFIER.match(name):
        raise ValueError(f"{what} must be a plain lower-case identifier (letters, digits, underscore), got {name!r}")
    return name


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _privs(letters: str) -> str:
    return ", ".join(LETTERS[c] for c in letters)


# The SQL below deliberately contains no percent sign and no :name, so the very same text runs from psql (the script)
# and through the DBAPI (apply_grants). Role and schema names are validated identifiers; table names come from this file.
_SYSTEM_SCHEMAS = "('pg_catalog', 'information_schema')"
_USER_SCHEMA_FILTER = "n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname !~ '^pg_(toast|temp)'"


def _plan_values(include_admin: bool) -> str:
    return ", ".join(f"({literal(t)}, {literal(_privs(p))})" for t, p in sorted(planned_relations(include_admin).items()))


def _planned_sequences_sql(schema: str, include_admin: bool) -> str:
    """SELECT of the oids of the sequences owned by the planned tables (serial / identity columns)."""
    tables = ", ".join(literal(t) for t in sorted(table_privileges(include_admin)))
    return (
        "SELECT s.oid FROM pg_class s JOIN pg_depend d ON d.objid = s.oid AND d.classid = 'pg_class'::regclass "
        "AND d.refclassid = 'pg_class'::regclass AND d.deptype IN ('a', 'i') JOIN pg_class t ON t.oid = d.refobjid "
        f"JOIN pg_namespace tn ON tn.oid = t.relnamespace WHERE s.relkind = 'S' AND tn.nspname = {literal(schema)} AND t.relname IN ({tables})"
    )


def guard_planned_objects_sql(schema: str, include_admin: bool = True) -> str:
    """Refuse to continue when a planned table or view does not exist (run the migrations first)."""
    return "\n".join([
        "DO $planned$",
        "DECLARE missing text;",
        "BEGIN",
        "    SELECT string_agg(p.obj, ', ' ORDER BY p.obj) INTO missing",
        f"    FROM (VALUES {_plan_values(include_admin)}) AS p(obj, privs)",
        f"    WHERE to_regclass(quote_ident({literal(schema)}) || '.' || quote_ident(p.obj)) IS NULL;",
        "    IF missing IS NOT NULL THEN",
        "        RAISE EXCEPTION USING MESSAGE = 'planned tables or views do not exist yet: ' || missing || ' - run the migrations first (python -m pipeline.migrate)';",
        "    END IF;",
        "END",
        "$planned$",
    ])


def database_statement(role: str) -> str:
    """Reset the role's own direct database privileges (CREATE, TEMPORARY, CONNECT), then give CONNECT back."""
    r = literal(validate_identifier(role))
    return "\n".join([
        "DO $database$",
        "BEGIN",
        f"    EXECUTE 'REVOKE ALL ON DATABASE ' || quote_ident(current_database()) || ' FROM ' || quote_ident({r});",
        f"    EXECUTE 'GRANT CONNECT ON DATABASE ' || quote_ident(current_database()) || ' TO ' || quote_ident({r});",
        "END",
        "$database$",
    ])


def database_revoke_statement(role: str) -> str:
    """Rollback only: remove the role's own direct database privileges (it keeps whatever PUBLIC gives everyone)."""
    r = literal(validate_identifier(role))
    return "\n".join([
        "DO $rbdb$",
        "BEGIN",
        f"    EXECUTE 'REVOKE ALL ON DATABASE ' || quote_ident(current_database()) || ' FROM ' || quote_ident({r});",
        "END",
        "$rbdb$",
    ])


def planned_sequences(conn, schema: str = "public", include_admin: bool = True) -> list[tuple[str, str]]:
    """(schema, name) of the sequences owned by the planned tables, read from the catalog with an open connection."""
    sql = ("SELECT sn.nspname, s.relname FROM pg_class s JOIN pg_namespace sn ON sn.oid = s.relnamespace "
           f"WHERE s.oid IN ({_planned_sequences_sql(schema, include_admin)}) ORDER BY 1, 2")
    return [(row[0], row[1]) for row in conn.exec_driver_sql(sql, ())]


def grant_statements(role: str, schema: str = "public", include_admin: bool = True) -> list[str]:
    """Schema, table and view statements, in order: every object is reset with REVOKE ALL (this role only), then granted."""
    validate_identifier(role)
    r, s = quote(role), quote(schema)
    statements = [f"REVOKE ALL ON SCHEMA {s} FROM {r}", f"GRANT USAGE ON SCHEMA {s} TO {r}"]
    for relation, letters in sorted(planned_relations(include_admin).items()):
        statements += [f"REVOKE ALL ON TABLE {s}.{quote(relation)} FROM {r}", f"GRANT {_privs(letters)} ON TABLE {s}.{quote(relation)} TO {r}"]
    return statements


def view_statements(role: str, schema: str = "public") -> list[str]:
    """Just the view's reset + grant (the migrations run it right after they recreate the view)."""
    validate_identifier(role)
    r, s = quote(role), quote(schema)
    out = []
    for view, letters in sorted(VIEWS.items()):
        out += [f"REVOKE ALL ON TABLE {s}.{quote(view)} FROM {r}", f"GRANT {_privs(letters)} ON TABLE {s}.{quote(view)} TO {r}"]
    return out


def sequence_statement(role: str, schema: str = "public", include_admin: bool = True) -> str:
    """USAGE (reset first) on the sequences owned by the planned tables."""
    r = literal(validate_identifier(role))
    return "\n".join([
        "DO $sequences$",
        "DECLARE seq record;",
        "BEGIN",
        "    FOR seq IN",
        "        SELECT sn.nspname AS seq_schema, s.relname AS seq_name FROM pg_class s JOIN pg_namespace sn ON sn.oid = s.relnamespace",
        f"        WHERE s.oid IN ({_planned_sequences_sql(schema, include_admin)}) ORDER BY 1, 2",
        "    LOOP",
        f"        EXECUTE 'REVOKE ALL ON SEQUENCE ' || quote_ident(seq.seq_schema) || '.' || quote_ident(seq.seq_name) || ' FROM ' || quote_ident({r});",
        f"        EXECUTE 'GRANT USAGE ON SEQUENCE ' || quote_ident(seq.seq_schema) || '.' || quote_ident(seq.seq_name) || ' TO ' || quote_ident({r});",
        "    END LOOP;",
        "END",
        "$sequences$",
    ])


def stray_revoke_statement(role: str, schema: str = "public", include_admin: bool = True) -> str:
    """
    Revoke the role's OWN direct grants (table, column, sequence and schema level) on everything outside the plan, in every
    schema of this database. Only grants to this role; other roles, PUBLIC and inherited access are never touched (the audit
    reports those). Each revocation is announced with a NOTICE.
    """
    r = literal(validate_identifier(role))
    tables = ", ".join(literal(t) for t in sorted(planned_relations(include_admin)))
    return "\n".join([
        "DO $stray$",
        "DECLARE obj record; app oid;",
        "BEGIN",
        f"    SELECT oid INTO app FROM pg_roles WHERE rolname = {r};",
        "    FOR obj IN",
        "        SELECT n.nspname AS sch, c.relname AS rel, c.relkind AS kind",
        "        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace",
        f"        WHERE {_USER_SCHEMA_FILTER} AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')",
        f"          AND NOT (n.nspname = {literal(schema)} AND ((c.relkind <> 'S' AND c.relname IN ({tables})) OR c.oid IN ({_planned_sequences_sql(schema, include_admin)})))",
        "          AND (EXISTS (SELECT 1 FROM aclexplode(c.relacl) a WHERE a.grantee = app)",
        "               OR EXISTS (SELECT 1 FROM pg_attribute at, aclexplode(at.attacl) a WHERE at.attrelid = c.oid AND a.grantee = app))",
        "        ORDER BY 1, 2",
        "    LOOP",
        "        EXECUTE 'REVOKE ALL ON ' || CASE WHEN obj.kind = 'S' THEN 'SEQUENCE ' ELSE 'TABLE ' END || quote_ident(obj.sch) || '.' || quote_ident(obj.rel) || ' FROM ' || quote_ident(" + r + ");",
        "        RAISE NOTICE USING MESSAGE = 'revoked a direct grant outside the plan from ' || " + r + " || ' on ' || obj.sch || '.' || obj.rel;",
        "    END LOOP;",
        "    FOR obj IN",
        "        SELECT n.nspname AS sch FROM pg_namespace n",
        f"        WHERE {_USER_SCHEMA_FILTER} AND n.nspname <> {literal(schema)} AND EXISTS (SELECT 1 FROM aclexplode(n.nspacl) a WHERE a.grantee = app)",
        "        ORDER BY 1",
        "    LOOP",
        "        EXECUTE 'REVOKE ALL ON SCHEMA ' || quote_ident(obj.sch) || ' FROM ' || quote_ident(" + r + ");",
        "        RAISE NOTICE USING MESSAGE = 'revoked a direct schema grant outside the plan from ' || " + r + " || ' on ' || obj.sch;",
        "    END LOOP;",
        "END",
        "$stray$",
    ])


def audit_sql(role: str, schema: str = "public", include_admin: bool = True) -> str:
    """
    A SELECT returning one row (severity, object, problem, fix) per finding about the role's EFFECTIVE state: direct,
    inherited through membership, and PUBLIC. severity 'violation' means the grants must not be accepted; 'warning' is
    informational. An empty result means the role has exactly the planned access and nothing more.
    """
    r, sc = literal(validate_identifier(role)), literal(schema)
    sch_filter = _USER_SCHEMA_FILTER
    fix_public = "revoke it from PUBLIC on that object yourself (it also affects other roles), or classify the object in pipeline/db_grants.py"
    return f"""
WITH app AS (SELECT oid, rolname FROM pg_roles WHERE rolname = {r}),
plan(obj, privs) AS (VALUES {_plan_values(include_admin)}),
planned_seq AS ({_planned_sequences_sql(schema, include_admin)}),
rels AS (
    SELECT c.oid, c.relname, c.relkind, c.relowner, n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE {sch_filter} AND c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
),
held AS (
    SELECT r.nspname, r.relname, r.relkind, r.oid, p.priv
    FROM rels r CROSS JOIN app CROSS JOIN (VALUES ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'), ('TRUNCATE'), ('REFERENCES'), ('TRIGGER'), ('MAINTAIN')) p(priv)
    WHERE r.relkind <> 'S' AND CASE
              -- MAINTAIN (VACUUM, ANALYZE, REINDEX, LOCK ...) exists from PostgreSQL 17; the nested CASE never evaluates it on older servers
              WHEN p.priv = 'MAINTAIN' THEN (CASE WHEN current_setting('server_version_num')::int >= 170000 THEN has_table_privilege(app.oid, r.oid, p.priv) ELSE false END)
              WHEN p.priv IN ('SELECT', 'INSERT', 'UPDATE', 'REFERENCES') THEN has_any_column_privilege(app.oid, r.oid, p.priv)
              ELSE has_table_privilege(app.oid, r.oid, p.priv) END
    UNION ALL
    SELECT r.nspname, r.relname, r.relkind, r.oid, q.priv
    FROM rels r CROSS JOIN app CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) q(priv)
    WHERE r.relkind = 'S' AND has_sequence_privilege(app.oid, r.oid, q.priv)
),
findings AS (
    -- 1. access the plan does not give: unplanned objects, or planned objects with extra privileges
    SELECT 'violation' AS severity, h.nspname || '.' || h.relname AS object,
           'holds ' || h.priv || ' (directly, through a role membership, or through PUBLIC), which is not in the plan' AS problem,
           'if it was granted directly, re-run the grant step (it revokes the role''s own direct grants); otherwise ' || {literal(fix_public)} AS fix
    FROM held h
    WHERE NOT (h.nspname = {sc} AND (
              (h.relkind <> 'S' AND EXISTS (SELECT 1 FROM plan p WHERE p.obj = h.relname AND h.priv = ANY (string_to_array(p.privs, ', '))))
           OR (h.relkind = 'S' AND h.oid IN (SELECT oid FROM planned_seq) AND h.priv = 'USAGE')))
    UNION ALL
    -- 2. planned access that is missing
    SELECT 'violation', {sc} || '.' || p.obj, 'is missing ' || x.priv, 'run the grant step again'
    FROM plan p CROSS JOIN app CROSS JOIN LATERAL unnest(string_to_array(p.privs, ', ')) AS x(priv)
    WHERE to_regclass(quote_ident({sc}) || '.' || quote_ident(p.obj)) IS NOT NULL
      AND NOT has_table_privilege(app.oid, (quote_ident({sc}) || '.' || quote_ident(p.obj))::regclass, x.priv)
    UNION ALL
    SELECT 'violation', s.relname, 'is missing USAGE', 'run the grant step again'
    FROM pg_class s CROSS JOIN app WHERE s.oid IN (SELECT oid FROM planned_seq) AND NOT has_sequence_privilege(app.oid, s.oid, 'USAGE')
    UNION ALL
    -- 3. role attributes and memberships (inherited access cannot be undone by revoking direct grants)
    SELECT 'violation', app.rolname, 'has the elevated attribute ' || a.attr,
           'ALTER ROLE ' || app.rolname || ' NO' || a.attr
    FROM app CROSS JOIN LATERAL (
        SELECT 'SUPERUSER' AS attr FROM pg_roles WHERE oid = app.oid AND rolsuper UNION ALL
        SELECT 'CREATEDB' FROM pg_roles WHERE oid = app.oid AND rolcreatedb UNION ALL
        SELECT 'CREATEROLE' FROM pg_roles WHERE oid = app.oid AND rolcreaterole UNION ALL
        SELECT 'REPLICATION' FROM pg_roles WHERE oid = app.oid AND rolreplication UNION ALL
        SELECT 'BYPASSRLS' FROM pg_roles WHERE oid = app.oid AND rolbypassrls) a
    UNION ALL
    SELECT 'violation', app.rolname, 'is a member of the role ' || g.rolname || ' (it can use, or SET ROLE to, everything that role can)',
           'REVOKE ' || quote_ident(g.rolname) || ' FROM ' || quote_ident(app.rolname) || ' (as a superuser or the grantor), then run this again'
    FROM app JOIN pg_roles g ON g.oid <> app.oid AND pg_has_role(app.oid, g.oid, 'MEMBER')
    UNION ALL
    -- 4. default privileges and ownership
    SELECT 'violation', 'default privileges of ' || d.defaclrole::regrole::text,
           'grant ' || x.privilege_type || ' on future ' || CASE d.defaclobjtype WHEN 'r' THEN 'tables' WHEN 'S' THEN 'sequences' WHEN 'f' THEN 'functions' WHEN 'T' THEN 'types' ELSE 'objects' END || ' to the role',
           'remove the default-privilege entry for ' || {r} || ' held under the role ' || d.defaclrole::regrole::text || ' (see pg_default_acl): it would expose every future table'
    FROM pg_default_acl d CROSS JOIN app CROSS JOIN LATERAL aclexplode(d.defaclacl) x WHERE x.grantee = app.oid
    UNION ALL
    SELECT 'violation', r.nspname || '.' || r.relname, 'is owned by the runtime role', 'ALTER TABLE ... OWNER TO the owner role'
    FROM rels r JOIN app ON r.relowner = app.oid
    UNION ALL
    SELECT 'violation', 'schema ' || n.nspname, 'is owned by the runtime role', 'ALTER SCHEMA ... OWNER TO the owner role'
    FROM pg_namespace n JOIN app ON n.nspowner = app.oid WHERE {sch_filter}
    UNION ALL
    SELECT 'violation', 'database ' || current_database(), 'is owned by the runtime role', 'ALTER DATABASE ... OWNER TO the owner role'
    FROM pg_database d JOIN app ON d.datdba = app.oid WHERE d.datname = current_database()
    UNION ALL
    -- 5. schemas and the database
    SELECT 'violation', 'schema ' || n.nspname, 'allows the role to ' || x.priv || ' (not in the plan)',
           'revoke it (the grant step removes direct schema grants; PUBLIC grants must be revoked by you)'
    FROM pg_namespace n CROSS JOIN app CROSS JOIN (VALUES ('CREATE'), ('USAGE')) x(priv)
    WHERE {sch_filter} AND has_schema_privilege(app.oid, n.oid, x.priv)
      AND NOT (n.nspname = {sc} AND x.priv = 'USAGE')
    UNION ALL
    SELECT 'violation', 'schema ' || {sc}, 'is missing USAGE', 'run the grant step again'
    FROM app WHERE NOT has_schema_privilege(app.oid, {sc}, 'USAGE')
    UNION ALL
    SELECT 'violation', 'database ' || current_database(), 'allows the role to CREATE schemas', 'REVOKE CREATE ON DATABASE ' || quote_ident(current_database()) || ' FROM ' || {r} || ' (and from PUBLIC if it comes from there)'
    FROM app WHERE has_database_privilege(app.oid, current_database(), 'CREATE')
    UNION ALL
    SELECT 'violation', 'database ' || current_database(), 'is missing CONNECT', 'run the grant step again'
    FROM app WHERE NOT has_database_privilege(app.oid, current_database(), 'CONNECT')
    UNION ALL
    SELECT 'warning', 'database ' || current_database(), 'allows the role to create TEMPORARY tables (a PUBLIC default; nothing in the API uses them)',
           'optional: REVOKE TEMPORARY ON DATABASE ' || quote_ident(current_database()) || ' FROM PUBLIC'
    FROM app WHERE has_database_privilege(app.oid, current_database(), 'TEMPORARY')
)
SELECT severity, object, problem, fix FROM findings ORDER BY severity DESC, object, problem
""".strip()


def audit_block_sql(role: str, schema: str = "public", include_admin: bool = True) -> str:
    """The audit as a DO block that aborts the surrounding transaction when there is any violation."""
    return "\n".join([
        "DO $audit$",
        "DECLARE report text;",
        "BEGIN",
        "    SELECT string_agg('  - ' || object || ': ' || problem || E'\\n      fix: ' || fix, E'\\n' ORDER BY object, problem) INTO report",
        "    FROM (",
        audit_sql(role, schema, include_admin),
        "    ) f WHERE severity = 'violation';",
        "    IF report IS NOT NULL THEN",
        f"        RAISE EXCEPTION USING MESSAGE = 'the runtime role {role} would not have exactly the planned access; nothing was changed:' || E'\\n' || report;",
        "    END IF;",
        "END",
        "$audit$",
    ])


def apply_grants(conn, role: str, schema: str = "public", include_admin: bool = True) -> list[str]:
    """
    Apply the whole plan with an open SQLAlchemy connection (the migration role, inside its transaction): the same statements
    the script runs. Raises GrantAuditError, with the findings and how to fix each, when the role would keep access the plan
    does not allow (the caller's transaction then rolls back). Returns the names of tables that exist but are not classified
    in this file (the API cannot use them).
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    steps = [guard_planned_objects_sql(schema, include_admin), database_statement(role), *grant_statements(role, schema, include_admin),
             sequence_statement(role, schema, include_admin), stray_revoke_statement(role, schema, include_admin)]
    try:
        for statement in steps:
            conn.exec_driver_sql(statement, ())
        findings = conn.exec_driver_sql(audit_sql(role, schema, include_admin), ()).all()
    except DBAPIError as exc:
        raise GrantAuditError(str(getattr(exc.orig, "diag", None) and exc.orig.diag.message_primary or exc.orig).strip()) from None
    violations = [row for row in findings if row[0] == "violation"]
    if violations:
        raise GrantAuditError(
            f"the runtime role {role} would not have exactly the planned access; nothing was changed:\n"
            + "\n".join(f"  - {obj}: {problem}\n      fix: {fix}" for _, obj, problem, fix in violations))
    classified = set(CODE) | set(VIEWS)
    existing = {row[0] for row in conn.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'v')"), {"schema": schema})}
    return sorted(existing - classified)


def verification_report(conn, role: str, schema: str = "public", include_admin: bool = True) -> list[tuple]:
    """The audit's findings (violations and warnings) without changing anything."""
    return [tuple(r) for r in conn.exec_driver_sql(audit_sql(role, schema, include_admin), ())]


def _guard_block(role: str, owner: str, database: str) -> str:
    q = literal
    return "\n".join([
        "-- Guard: right database, run by the owner or a superuser, role exists.",
        "DO $guard$",
        "BEGIN",
        f"    IF current_database() <> {q(database)} THEN",
        f"        RAISE EXCEPTION 'this script is for database {database}, not %', current_database();",
        "    END IF;",
        f"    IF NOT (pg_has_role(current_user, {q(owner)}, 'USAGE') OR (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)) THEN",
        f"        RAISE EXCEPTION 'run this script as {owner} (or a superuser), not %', current_user;",
        "    END IF;",
        f"    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {q(role)}) THEN",
        f"        RAISE EXCEPTION 'role {role} does not exist';",
        "    END IF;",
        "END",
        "$guard$;",
    ])


def sql_script(role: str, owner: str, database: str, schema: str = "public", include_admin: bool = True) -> str:
    """The reviewable script: run it, once, as the owner role (or a superuser) after the migrations. One transaction."""
    validate_identifier(role, "runtime role")
    validate_identifier(owner, "owner role")
    validate_identifier(database, "database")
    lines = [
        "-- Runtime-role grants for the MAVLIT API. GENERATED by `python -m pipeline.db_grants`; do not edit by hand.",
        f"-- Role {role} (API runtime), schema owner {owner}, database {database}, schema {schema}.",
        "-- Review it, run it on a scratch copy first, then run it as the owner role (or a superuser). Everything below is one",
        f"-- transaction: it either leaves {role} with exactly the planned access or changes nothing. It touches only {role}'s own",
        "-- grants (never another role's), only inside this database, and only the objects named here; backup (bak_*) tables,",
        "-- experiment tables and everything not listed stay inaccessible. Safe to run repeatedly (each object is reset first).",
        "-- It does NOT use ALTER DEFAULT PRIVILEGES, and it never changes role memberships or PUBLIC: if the role is a member of",
        "-- another role, holds PUBLIC-derived access, default privileges, owns objects or has elevated attributes, the final",
        "-- audit step aborts the transaction and prints how to fix each finding.",
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "",
        _guard_block(role, owner, database),
        "",
        "-- Planned tables and views must exist (run the migrations first).",
        guard_planned_objects_sql(schema, include_admin) + ";",
        "",
        "-- Database: reset the role's own direct privileges, then CONNECT.",
        database_statement(role) + ";",
        "",
        "-- Schema, tables and the view (privileges = routes and jobs + SQLAdmin; see pipeline/db_grants.py).",
    ]
    lines += [statement + ";" for statement in grant_statements(role, schema, include_admin)]
    lines += [
        "",
        "-- Sequences owned by the tables above (serial / identity ids): USAGE lets inserts take the next value.",
        sequence_statement(role, schema, include_admin) + ";",
        "",
        f"-- The role's own direct grants outside the plan (other tables, views, sequences, schemas) are revoked; nobody else's are.",
        stray_revoke_statement(role, schema, include_admin) + ";",
        "",
        "-- Audit: the role's EFFECTIVE access (direct, inherited through membership, PUBLIC) must equal the plan, or abort.",
        audit_block_sql(role, schema, include_admin) + ";",
        "",
        "COMMIT;",
        "",
        "-- Optional hardening, NOT part of the grants above (it changes PUBLIC, so decide separately):",
        "--   PostgreSQL 15+ already denies CREATE on schema public to PUBLIC; on older versions run:",
        f"--     REVOKE CREATE ON SCHEMA {schema} FROM PUBLIC;",
        "--   Temporary tables are allowed to PUBLIC by default (nothing in the API uses them; the audit reports it as a warning):",
        f"--     REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC;",
        "-- Not granted on purpose: TRUNCATE, REFERENCES, TRIGGER, CREATE, ownership, superuser, and every table not listed above.",
        "",
    ]
    return "\n".join(lines)


def rollback_sql(role: str, owner: str, database: str, schema: str = "public") -> str:
    """
    Revokes everything the grant script gave the role (and any other direct grant it holds in this database): the database,
    schema, every table / view / sequence of the schema, and its column grants. It does not drop the role, change memberships,
    touch PUBLIC or other roles, or delete data. Repeatable; one transaction; ends with a check that no direct grant remains.
    """
    validate_identifier(role, "runtime role")
    validate_identifier(owner, "owner role")
    validate_identifier(database, "database")
    r, s = quote(role), quote(schema)
    rl = literal(role)
    return "\n".join([
        "-- Rollback of the runtime-role grants. GENERATED by `python -m pipeline.db_grants --rollback`; do not edit by hand.",
        f"-- Revokes every direct grant held by role {role} in database {database}. Does NOT drop the role, change its memberships,",
        "-- touch PUBLIC or any other role, or delete data. Switching DATABASE_URL back to the owner role does not do this.",
        "\\set ON_ERROR_STOP on",
        "BEGIN;",
        "",
        _guard_block(role, owner, database),
        "",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM {r};",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM {r};",
        stray_revoke_statement(role, schema, include_admin=True) + ";   -- any other schema / object outside the plan",
        f"REVOKE ALL ON SCHEMA {s} FROM {r};",
        database_revoke_statement(role) + ";",
        "",
        "-- Verify: no direct grant to the role may remain anywhere in this database.",
        "DO $verify$",
        "DECLARE app oid; left_over text;",
        "BEGIN",
        f"    SELECT oid INTO app FROM pg_roles WHERE rolname = {rl};",
        "    SELECT string_agg(x.what, ', ' ORDER BY x.what) INTO left_over FROM (",
        "        SELECT 'relation ' || n.nspname || '.' || c.relname AS what FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace",
        "            WHERE EXISTS (SELECT 1 FROM aclexplode(c.relacl) a WHERE a.grantee = app)",
        "               OR EXISTS (SELECT 1 FROM pg_attribute at, aclexplode(at.attacl) a WHERE at.attrelid = c.oid AND a.grantee = app)",
        "        UNION ALL SELECT 'schema ' || n.nspname FROM pg_namespace n WHERE EXISTS (SELECT 1 FROM aclexplode(n.nspacl) a WHERE a.grantee = app)",
        "        UNION ALL SELECT 'database ' || d.datname FROM pg_database d WHERE d.datname = current_database() AND EXISTS (SELECT 1 FROM aclexplode(d.datacl) a WHERE a.grantee = app)",
        "    ) x;",
        "    IF left_over IS NOT NULL THEN",
        "        RAISE EXCEPTION USING MESSAGE = 'direct grants remain: ' || left_over;",
        "    END IF;",
        "END",
        "$verify$;",
        "",
        "COMMIT;",
        "",
        "-- Not done here, on purpose: dropping the role (DROP OWNED BY / DROP ROLE), REVOKE of memberships (REVOKE <role> FROM <role>),",
        "-- changes to PUBLIC, and anything touching data.",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the runtime-role grant (or rollback) script. Connects to nothing.")
    parser.add_argument("--role", default="mavlit_app")
    parser.add_argument("--owner", default="mavlit_user")
    parser.add_argument("--database", default="mavlit_enrichment_test")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--rollback", action="store_true", help="print the rollback script instead")
    parser.add_argument("--print-audit", action="store_true", help="print the read-only audit query (psql -f it to check the role's effective access; changes nothing)")
    parser.add_argument("--no-admin-writes", action="store_true", help="plan only what routes and jobs need; SQLAdmin edits would then fail")
    parser.add_argument("--write", metavar="PATH", help="write the script to PATH instead of printing it")
    parser.add_argument("--check", metavar="PATH", help="exit 1 if PATH differs from the script that would be printed")
    args = parser.parse_args(argv)
    if args.print_audit:
        script = audit_sql(args.role, args.schema, include_admin=not args.no_admin_writes) + ";\n"
    elif args.rollback:
        script = rollback_sql(args.role, args.owner, args.database, args.schema)
    else:
        script = sql_script(args.role, args.owner, args.database, args.schema, include_admin=not args.no_admin_writes)
    if args.check:
        with open(args.check, encoding="utf-8") as fh:
            same = fh.read() == script
        print("up to date" if same else f"{args.check} is out of date: regenerate it with --write", file=sys.stderr if not same else sys.stdout)
        return 0 if same else 1
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(script)
        return 0
    sys.stdout.write(script)
    return 0


if __name__ == "__main__":
    sys.exit(main())
