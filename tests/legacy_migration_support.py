"""
Test support for tests/test_migration_differential.py (not a test module itself).

  * load_baseline_migration(): the ORIGINAL migration, taken verbatim from `git show <rev>:api/app.py` and executed against an engine
    that the test passes in. api/app.py itself is never imported or run, so no API, background task or startup hook can start and no
    other database can be touched. Nothing here is a second copy of the migrations: the source comes from git every time.
  * build_legacy_database(): turns a freshly created current-schema database into the shape older releases left behind, so that the legacy
    branches of the migration have something to upgrade.
  * snapshot(): everything the comparison looks at (schema, indexes, constraints, views, grants, sequences, data).
"""

import ast
import builtins
import importlib
import logging
import re
import subprocess
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("tests.baseline_migration")


class BaselineUnavailable(RuntimeError):
    """The baseline revision has no in-place _run_migrations (e.g. HEAD already contains the extraction)."""


def baseline_source(rev: str) -> str:
    result = subprocess.run(["git", "show", f"{rev}:api/app.py"], cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise BaselineUnavailable(f"git show {rev}:api/app.py failed: {result.stderr.strip()[:200]}")
    return result.stdout


def baseline_function(source: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    found = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_run_migrations"]
    if not found or (found[0].end_lineno - found[0].lineno) < 200:
        raise BaselineUnavailable("the baseline revision has no full in-place _run_migrations; set MAVLIT_MIGRATION_BASELINE_REV to the commit before the extraction")
    return found[0]


def load_baseline_migration(engine, rev: str):
    """Return the original _run_migrations as a no-argument callable bound to `engine` (api/app.py is not imported)."""
    source = baseline_source(rev)
    function = baseline_function(source)
    tree = ast.parse(source)
    imported = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                imported[alias.asname or alias.name] = (node.module, alias.name)
    used = {n.id for n in ast.walk(function) if isinstance(n, ast.Name)}
    local = {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    namespace = {"engine": engine, "SessionLocal": sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False),
                 "logger": logger, "text": text}
    for name in sorted(used - local - set(dir(builtins)) - set(namespace)):
        if name not in imported:
            raise BaselineUnavailable(f"the baseline migration uses {name!r}, which is not an import of api/app.py")
        module, attribute = imported[name]
        namespace[name] = getattr(importlib.import_module(module), attribute)
    module_ast = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module_ast)
    exec(compile(module_ast, f"<{rev}:api/app.py::_run_migrations>", "exec"), namespace)
    return namespace["_run_migrations"], source


# ---- the legacy database --------------------------------------------------------------------------------------------------------
def _vector(dimensions: int) -> str:
    return "[" + ",".join(["0.25"] * dimensions) + "]"


def build_legacy_database(engine, baseline: str, scenario: str, reader_role: str) -> None:
    """
    `engine` points at a database that already has the vector extension and the CURRENT schema (Base.metadata.create_all). Age it into
    the shape older releases left, then seed rows that exercise the migration's data paths. Scenarios differ only in the prompts table.
    """
    from pipeline.helpers import prompts as P

    added = re.findall(r"ALTER TABLE (?:IF EXISTS )?(\w+) ADD COLUMN IF NOT EXISTS (\w+)", baseline)
    dropped = re.findall(r"ALTER TABLE (\w+) DROP COLUMN IF EXISTS (\w+)", baseline)
    keep = {("creator_profiles", c) for c in ("email", "full_name", "creator_handle", "content_niche", "password_hash")} \
        | {("brand_contacts", "brand_raw_id"), ("instagram_users", "user_type")} | {("pitches", c) for c in ("status",)}   # the seed rows below use these
    with engine.begin() as conn:
        # 1. columns older releases did not have (the migration adds them again, with its defaults, over existing rows)
        for table, column in sorted(set(added) - keep):
            exists = conn.execute(text("SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = :t AND column_name = :c"), {"t": table, "c": column}).first()
            is_pk = conn.execute(text("SELECT 1 FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) WHERE i.indisprimary AND i.indrelid = to_regclass(:t) AND a.attname = :c"), {"t": table, "c": column}).first()
            if exists and not is_pk:
                conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN "{column}" CASCADE'))
        # 2. columns older releases had and the migration removes
        for table, column in dropped:
            conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{column}" TEXT'))
        conn.execute(text('CREATE UNIQUE INDEX uq_creator_profiles_google_id ON creator_profiles (google_id)'))
        # 3. legacy tables the migration drops, with rows
        for table in ("tiktok_posts", "twitter_posts", "instagram_commenters", "contacts", "brands", "rate_intelligence_logs"):
            conn.execute(text(f"CREATE TABLE {table} (id serial PRIMARY KEY, note text)"))
            conn.execute(text(f"INSERT INTO {table} (note) VALUES ('legacy row')"))
        conn.execute(text("CREATE TABLE login_credentials (email text PRIMARY KEY, password_hash text NOT NULL)"))
        # 4. legacy constraints and indexes the migration replaces
        conn.execute(text("ALTER TABLE instagram_users ADD CONSTRAINT instagram_users_username_key UNIQUE (username)"))
        conn.execute(text("CREATE UNIQUE INDEX uq_instagram_users_username_default ON instagram_users (username)"))
        for table, name in (("instagram_users", "uq_instagram_users_post_id"), ("creator_profiles", "uq_creator_profiles_email"), ("brand_contacts", "uq_brand_contact_person")):
            conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))   # unique constraints and unique indexes are dropped differently
            conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
        # 5. NOT NULL constraints the migration relaxes
        for table, column in (("brands_raw", "name"), ("brands_raw", "name_normalized"), ("brands_raw", "niche"), ("brands_raw", "source"), ("instagram_posts", "post_id"),
                              ("creator_profiles", "full_name"), ("creator_profiles", "creator_handle"), ("creator_profiles", "content_niche"), ("rate_estimates", "brand_raw_id")):
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} SET NOT NULL"))
        # 6. column types: TEXT timestamps, unconstrained vectors, the old status default
        conn.execute(text("ALTER TABLE brand_match_profile ADD COLUMN IF NOT EXISTS youtube_last_sponsorship TEXT"))   # (step 1 may have dropped it: the migration adds it)
        conn.execute(text("ALTER TABLE brand_match_profile ALTER COLUMN youtube_last_sponsorship TYPE TEXT USING youtube_last_sponsorship::text"))
        for table in ("brand_match_profile", "creator_profiles"):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS embedding vector"))
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector USING embedding::vector"))
        conn.execute(text("ALTER TABLE pitches ALTER COLUMN status SET DEFAULT 'proposal_sent'"))
        # 7. the creator niche table under its old name, without its uniqueness rules
        conn.execute(text("ALTER TABLE creator_niches DROP CONSTRAINT IF EXISTS uq_creator_niches_username"))
        for index in ("uq_creator_niches_username", "uq_creator_niches_username_normalized"):
            conn.execute(text(f"DROP INDEX IF EXISTS {index}"))
        conn.execute(text("ALTER TABLE creator_niches RENAME TO creator_profiles_test"))
        conn.execute(text("ALTER TABLE creator_profiles_test RENAME CONSTRAINT creator_niches_pkey TO creator_profiles_test_pkey"))
        conn.execute(text("ALTER SEQUENCE creator_niches_id_seq RENAME TO creator_profiles_test_id_seq"))
        for suffix in ("username", "niche", "instagram_user_id"):
            conn.execute(text(f"ALTER INDEX IF EXISTS ix_creator_niches_{suffix} RENAME TO ix_creator_profiles_test_{suffix}"))
        conn.execute(text("CREATE UNIQUE INDEX uq_creator_profiles_test_instagram_user_id ON creator_profiles_test (instagram_user_id)"))
        # 8. an old definition of the cost view, readable by another role
        conn.execute(text("CREATE VIEW creator_llm_cost AS SELECT 1::int AS creator_profile_id, 0::numeric AS total_cost_usd"))
        conn.execute(text(f"GRANT SELECT ON creator_llm_cost TO {reader_role}"))

    with engine.begin() as conn:
        # rows ------------------------------------------------------------------------------------------------------------------------
        conn.execute(text("INSERT INTO brands_raw (id, name, name_normalized, niche, source) VALUES "
                          "(1, 'Alpha', 'alpha', 'beauty', 'legacy'), (2, 'Beta', 'beta', 'fitness', 'legacy'), (3, 'Gamma', 'gamma', 'music', 'legacy')"))
        conn.execute(text("INSERT INTO instagram_posts (id, brand_raw_id, instagram_handle, post_id) VALUES (1, 1, 'alpha', 'p1'), (2, 2, 'beta', 'p2')"))
        conn.execute(text("INSERT INTO creator_profiles (id, email, full_name, creator_handle, content_niche, password_hash, embedding) VALUES "
                          "(1, 'one@example.com', 'One', 'one', 'beauty', NULL, CAST(:v768 AS vector)), "
                          "(2, 'two@example.com', 'Two', 'two', 'music', 'already-set', CAST(:v1024 AS vector)), "
                          "(3, 'three@example.com', 'Three', 'three', 'fitness', NULL, NULL)"), {"v768": _vector(768), "v1024": _vector(1024)})
        conn.execute(text("INSERT INTO login_credentials VALUES ('one@example.com', 'hash-one'), ('two@example.com', 'hash-two'), ('nobody@example.com', 'hash-orphan')"))
        conn.execute(text("INSERT INTO brand_match_profile (brand_raw_id, youtube_last_sponsorship, embedding) VALUES "
                          "(1, '2026-07-01T10:30:00+00:00', CAST(:v768 AS vector)), (2, '2026-08-02', CAST(:v1024 AS vector)), (3, '', NULL)"),
                     {"v768": _vector(768), "v1024": _vector(1024)})
        conn.execute(text("INSERT INTO pitches (id, creator_profile_id, brand_name, story, status) VALUES "
                          "(1, 1, 'Alpha', 's', 'proposal_sent'), (2, 1, 'Beta', 's', 'sent'), (3, 2, 'Gamma', 's', 'proposal_sent'), (4, 2, 'Delta', 's', 'generated')"))
        conn.execute(text("INSERT INTO instagram_users (id, username, user_type) VALUES (1, 'alice_a', 'contentcreatorRE'), (2, 'alice_b', 'contentcreatorRE'), (3, 'alice_c', 'contentcreatorRE'), (4, 'bob_a', 'contentcreatorRE')"))
        # duplicates under LOWER(TRIM(username)): the newest row of each group must survive
        conn.execute(text("INSERT INTO creator_profiles_test (id, instagram_user_id, username, niche, created_at) VALUES "
                          "(1, 1, 'Alice', 'beauty', '2026-01-01'), (2, 2, ' alice ', 'beauty', '2026-03-01'), (3, 3, 'ALICE', 'fashion', '2026-02-01'), (4, 4, 'bob', 'music', '2026-01-05')"))
        conn.execute(text("INSERT INTO brand_contacts (id, brand_raw_id, department, title_score) VALUES (1, 1, 'marketing', '9'), (2, 1, 'sales', '4')"))
        conn.execute(text("UPDATE brands_raw SET male_pct = '50', twitter_handle = '@alpha', enriched = 'yes' WHERE id = 1"))
        conn.execute(text("UPDATE creator_profiles SET google_id = 'g-1', avatar_url = 'http://x', tiktok_stats = 'old' WHERE id = 1"))
        conn.execute(text("INSERT INTO llm_usage (model, endpoint, prompt_tokens, cached_tokens, completion_tokens, reasoning_tokens) VALUES ('m', 'e', 1, 0, 1, 0)"))

        # prompts (scenario specific) ---------------------------------------------------------------------------------------------------
        def prompt(name, content):
            conn.execute(text("INSERT INTO prompts (name, content) VALUES (:n, :c)"), {"n": name, "c": content})
        prompt("my_custom_prompt", "an operator's own prompt: never touched")
        if scenario == "stale":
            prompt(P.PITCH_PROMPT_NAME, "old pitch prompt without the audience input")
            prompt(P.BRAND_CHECK_PROMPT_NAME, "old brand check without the referral flag")
            prompt(P.LINKEDIN_COMPANY_MATCH_PROMPT_NAME, "old linkedin prompt without the work history")
            prompt(P.CONTRACT_ADVICE_PROMPT_NAME, "old contract prompt without deal terms")
            prompt(P.RATE_INTEL_PROMPT_NAME, "old rate prompt {creator_highest_likes} but nothing else")
            prompt("instagram_creator_profile", "old creator description prompt (renamed by the migration)")
        elif scenario == "edited":
            prompt(P.PITCH_PROMPT_NAME, "edited pitch prompt that already uses {brand_target_audience}")
            prompt(P.BRAND_CHECK_PROMPT_NAME, "edited brand check with has_referral_code")
            prompt(P.LINKEDIN_COMPANY_MATCH_PROMPT_NAME, "edited linkedin prompt with {experience}")
            prompt(P.CONTRACT_ADVICE_PROMPT_NAME, "edited contract prompt with {exclusivity}")
            prompt(P.RATE_INTEL_PROMPT_NAME, "edited rate {creator_highest_likes} {views_adjustment} speaking directly to the creator")
            prompt("instagram_creator_profile", "old creator description prompt (deleted: the new name exists)")
            prompt(P.CREATOR_NICHE_DESCRIPTION_PROMPT_NAME, "the current creator description prompt")
        elif scenario == "previous_default":
            previous = next(p for p in P.PITCH_PREVIOUS_DEFAULT_PROMPTS if "{brand_target_audience}" in p)   # unedited earlier release
            prompt(P.PITCH_PROMPT_NAME, previous)
            prompt("creator_profile", "older name of the creator description prompt")
        else:
            raise ValueError(scenario)


# ---- what the comparison looks at ---------------------------------------------------------------------------------------------------
VOLATILE_COLUMNS = ["created_at", "updated_at"]   # filled with now() when the migration seeds rows: never comparable between two runs


def snapshot(conn) -> dict:
    def q(sql, **params):
        return [tuple(row) for row in conn.execute(text(sql), params)]

    snap = {
        "extensions": q("SELECT extname, extversion FROM pg_extension ORDER BY 1"),
        "tables": [r[0] for r in q("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') ORDER BY 1")],
        "columns": q("""SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid),
                               row_number() OVER (PARTITION BY c.relname ORDER BY a.attnum)
                        FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped ORDER BY 1, 2"""),
        "indexes": q("SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 2"),
        "constraints": q("""SELECT cl.relname, con.conname, con.contype::text, pg_get_constraintdef(con.oid)
                            FROM pg_constraint con JOIN pg_class cl ON cl.oid = con.conrelid JOIN pg_namespace n ON n.oid = cl.relnamespace
                            WHERE n.nspname = 'public' ORDER BY 1, 2"""),
        "views": q("SELECT viewname, definition FROM pg_views WHERE schemaname = 'public' ORDER BY 1"),
        "acls": q("""SELECT c.relname, c.relkind::text, c.relacl::text FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public' AND c.relkind IN ('r', 'v', 'S') ORDER BY 1"""),
    }
    snap["sequences"] = []
    for (name,) in q("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'S' ORDER BY 1"):
        snap["sequences"].append((name, *conn.execute(text(f'SELECT last_value, is_called FROM "{name}"')).one()))
    snap["data"] = {}
    for table in snap["tables"]:
        snap["data"][table] = [r[0] for r in q(f'SELECT (to_jsonb(t) - CAST(:volatile AS text[]))::text FROM "{table}" t ORDER BY 1', volatile="{" + ",".join(VOLATILE_COLUMNS) + "}")]
    return snap
