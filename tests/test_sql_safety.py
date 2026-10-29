"""
SQL-injection regression tests (checklist 2.7 / 2.8).

Static part (no database): no production code builds SQL text from f-strings, concatenation, % or .format()/.replace() outside the two modules that
generate DDL from validated identifiers; LIKE patterns are escaped. Behavioural part (needs the disposable cluster of tests/test_migration_differential.py:
MAVLIT_TEST_PG_URL + MAVLIT_EXPECT_PG_MAJOR; skipped otherwise): the queries that used to be assembled as strings now take bound parameters and still return
what they returned before.

Run:  python -m pytest tests/test_sql_safety.py -q
"""

import ast
import os
import re
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"
os.environ["JWT_SECRET"] = os.environ.get("JWT_SECRET") or "test-jwt-secret"
os.environ["RUN_MIGRATIONS"] = ""
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from pipeline.helpers.db import escape_like  # noqa: E402

SKIP_DIRS = {".venv", "node_modules", "production_dump", "__pycache__", ".git", "rate_csv", "rate_analysis", "frontend", "tests"}
SQL_WORDS = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+|CREATE\s+|TRUNCATE|GRANT|REVOKE|SET\s+LOCAL|WHERE)\b", re.I)
SINKS = {"text", "execute", "exec_driver_sql", "executemany", "from_statement", "literal_column"}

# Where SQL text is legitimately assembled, and why. Identifiers and keywords cannot be bound parameters in PostgreSQL; here they are validated first.
ALLOWED_DYNAMIC = {
    "pipeline/db_grants.py": "DDL generator: role / database names pass IDENTIFIER, tables come from a fixed registry, literals go through literal()/quote()",
    "pipeline/migrations.py": "two DDL steps: a type-cast expression chosen from a fixed dict, and a GRANT whose privilege is checked against a fixed set and whose role is quoted",
    "pipeline/enrichment/wikidata_socials.py": "SPARQL, not SQL",
    "pipeline/enrichment_re/brand_wikidata_lookup.py": "SPARQL, not SQL",
    "pipeline/sources/wikidata.py": "SPARQL, not SQL",
}


def _production_files():
    for path in sorted(ROOT.rglob("*.py")):
        if not (set(path.relative_to(ROOT).parts) & SKIP_DIRS):
            yield path


def _built(node) -> bool:
    """True for an f-string, concatenation, % formatting, or .format()/.replace() used as SQL text."""
    if isinstance(node, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("format", "replace")


class NoStringBuiltSql(unittest.TestCase):
    def test_no_production_query_is_built_from_strings(self):
        offenders = []
        for path in _production_files():
            relative = str(path.relative_to(ROOT))
            if relative in ALLOWED_DYNAMIC:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
                    if name in SINKS and node.args and _built(node.args[0]):
                        offenders.append(f"{relative}:{node.lineno} {name}(...) is given a built string")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("replace", "format"):
                    receiver = node.func.value
                    named_sql = isinstance(receiver, ast.Name) and re.search(r"(SQL|QUERY)", receiver.id)
                    literal_sql = isinstance(receiver, ast.Constant) and isinstance(receiver.value, str) and SQL_WORDS.search(receiver.value)
                    if named_sql or literal_sql:
                        offenders.append(f"{relative}:{node.lineno} a SQL constant is rewritten with .{node.func.attr}()")
                if isinstance(node, ast.JoinedStr) and any(isinstance(v, ast.FormattedValue) for v in node.values):
                    literal = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                    if SQL_WORDS.search(literal) and re.search(r"\b(SELECT|INSERT|UPDATE|DELETE|ALTER|DROP|CREATE|TRUNCATE|GRANT|REVOKE|SET)\b", literal):
                        offenders.append(f"{relative}:{node.lineno} an f-string that looks like SQL")
        self.assertEqual(offenders, [], "use bound parameters, or add the module to ALLOWED_DYNAMIC with a reason")

    def test_the_allowed_modules_only_interpolate_validated_values(self):
        source = (ROOT / "pipeline" / "migrations.py").read_text()
        self.assertIn("set_config('lock_timeout', :ms, true)", source, "the lock timeout is a bound parameter")
        self.assertNotIn('f"SET LOCAL', source)
        self.assertIn("_VIEW_PRIVILEGES", source, "GRANT privileges are checked against a fixed set")
        grants = (ROOT / "pipeline" / "db_grants.py").read_text()
        self.assertIn("IDENTIFIER = re.compile", grants)

    def test_the_formerly_templated_queries_are_constants_with_parameters(self):
        from gender_check import run_brand_gender_check as gender, run_brand_website_audience_check as audience
        from pipeline import pitching, rate_intelligence
        self.assertNotIn("{", gender._BRAND_QUERY_SQL)
        self.assertIn(":beach_only", gender._BRAND_QUERY_SQL)
        self.assertNotIn("{", audience._BRAND_QUERY_SQL)
        for name in (":force", ":beach_only"):
            self.assertIn(name, audience._BRAND_QUERY_SQL)
        self.assertIn(":restrict_niches", pitching._PARTNER_CREATORS_SQL)
        self.assertFalse(hasattr(rate_intelligence, "_ALL_PARTNER_CREATORS_SQL"))
        for path in ("scripts/embed_test_niches.py", "script_creator_niche/embed_creator_niches.py"):
            text_ = (ROOT / path).read_text()
            self.assertIn("(:force OR embedding IS NULL)", text_)
            self.assertNotIn("{predicate}", text_)

    def test_request_values_never_choose_a_sort_or_column(self):
        """The API takes no sort / column / table name from a request; SQLAdmin's sortBy is checked against column_sortable_list by the library."""
        import api.app as api_app
        from sqladmin import ModelView
        for view in api_app.admin.views:
            if isinstance(view, ModelView):
                self.assertTrue(hasattr(view, "_sort_fields"), type(view).__name__)
        for path in (ROOT / "api").glob("*.py"):
            self.assertIsNone(re.search(r"order_by\(\s*(getattr|text)\(", path.read_text()), f"{path.name}: a computed sort column")


class LikeEscaping(unittest.TestCase):
    def test_wildcards_become_literal(self):
        self.assertEqual(escape_like("50%_off"), r"50\%\_off")
        self.assertEqual(escape_like("a\\b"), "a\\\\b")
        self.assertEqual(escape_like("plain"), "plain")

    def test_every_user_fed_pattern_is_escaped_and_declares_the_escape_character(self):
        app = (ROOT / "api" / "app.py").read_text()
        self.assertIn('pattern = f"%{escape_like(term)}%"', app)
        self.assertEqual(app.count('.ilike(pattern, escape="\\\\")'), 5)
        self.assertIn("ILIKE :pattern ESCAPE '\\\\'", app)
        self.assertIn('{"pattern": f"%{escape_like(name)}%"}', app)
        rate = (ROOT / "pipeline" / "rate_intelligence.py").read_text()
        self.assertIn('BrandRaw.name.ilike(escape_like(name), escape="\\\\")', rate)


# ---- behaviour, on the disposable cluster -------------------------------------------------------------------------------------------------
TEST_URL, EXPECTED_MAJOR = os.environ.get("MAVLIT_TEST_PG_URL"), os.environ.get("MAVLIT_EXPECT_PG_MAJOR")
CONFIGURED = bool(TEST_URL and EXPECTED_MAJOR)


@unittest.skipUnless(CONFIGURED, "needs MAVLIT_TEST_PG_URL (a disposable local cluster) and MAVLIT_EXPECT_PG_MAJOR")
class BoundParameterBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from sqlalchemy import text
        from sqlalchemy.orm import sessionmaker
        import test_migration_differential as guard
        from pipeline.db import Base
        cls.guard, cls.text = guard, staticmethod(text)
        cls.maintenance = guard._engine(TEST_URL, isolation_level="AUTOCOMMIT")
        cls.addClassCleanup(cls.maintenance.dispose)
        guard.assert_isolated(cls.maintenance, TEST_URL)
        cls.name = f"mavlit_difftest_sqlsafety_{uuid.uuid4().hex[:10]}"
        with cls.maintenance.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{cls.name}"'))
        cls.addClassCleanup(cls._drop)
        cls.engine = guard._engine(TEST_URL, cls.name)
        cls.addClassCleanup(cls.engine.dispose)
        with cls.engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(bind=cls.engine, autoflush=False, expire_on_commit=False)

    @classmethod
    def _drop(cls):
        with cls.maintenance.connect() as conn:
            conn.execute(cls.text(f'DROP DATABASE IF EXISTS "{cls.name}" WITH (FORCE)'))

    def setUp(self):
        with self.engine.begin() as conn:
            conn.execute(self.text("TRUNCATE brands_raw, brands_niches, test_niches, creator_niches, instagram_users, instagram_posts RESTART IDENTITY CASCADE"))

    # -- LIKE escaping -----------------------------------------------------------------------------------------------------------------
    def test_brand_lookup_by_name_treats_percent_and_underscore_literally(self):
        from pipeline.db import BrandRaw
        from pipeline.rate_intelligence import _resolve_brand
        with self.Session() as db:
            db.add_all([BrandRaw(name="Acme"), BrandRaw(name="100% Juice"), BrandRaw(name="a_b"), BrandRaw(name="axb")])
            db.commit()
            found = lambda name: (_resolve_brand(db, None, name) or BrandRaw(name=None)).name   # noqa: E731
            self.assertEqual(found("acme"), "Acme", "still a case-insensitive exact match")
            self.assertEqual(found("100% juice"), "100% Juice")
            self.assertIsNone(found("%"), "a bare wildcard no longer matches the first brand")
            self.assertIsNone(found("100%"))
            self.assertEqual(found("a_b"), "a_b")
            self.assertIsNone(found("a_"), "_ is not a one-character wildcard")
            self.assertIsNone(found("Ac%"))

    def test_niche_lookup_endpoint_helper_does_not_expand_wildcards(self):
        import api.app as api_app
        with self.engine.begin() as conn:
            conn.execute(self.text("""INSERT INTO brands_raw (id, name) VALUES (1, 'x')"""))
            conn.execute(self.text("""INSERT INTO brands_niches (brand_raw_id, niche, tags) VALUES (1, 'Health & Wellness', CAST('["yoga","spa"]' AS jsonb))"""))
        with mock.patch.object(api_app, "SessionLocal", self.Session):
            self.assertEqual(api_app.list_brand_category_tags(niche=["Health"])["tags_by_niche"]["Health"], ["spa", "yoga"], "a plain word still matches by 'contains'")
            self.assertEqual(api_app.list_brand_category_tags(niche=["%"])["tags_by_niche"]["%"], [], "% is not a wildcard any more")
            self.assertEqual(api_app.list_brand_category_tags(niche=["_ealth"])["tags_by_niche"]["_ealth"], [])

    def test_admin_search_matches_wildcards_literally(self):
        import api.app as api_app
        from sqlalchemy import select
        from pipeline.db import InstagramCreatorCommenter
        view = next(v for v in api_app.admin.views if getattr(v, "model", None) is InstagramCreatorCommenter)
        stmt = view.search_query(select(InstagramCreatorCommenter), "50%_off")
        rendered = str(stmt.compile(self.engine, compile_kwargs={"literal_binds": True}))
        compiled = rendered.replace("%%", "%").replace("\\\\", "\\")   # undo the driver-level doubling of % and backslashes in the literal-binds rendering
        self.assertIn("ESCAPE '\\'", compiled)
        self.assertIn("%50\\%\\_off%", compiled)
        with self.engine.connect() as conn:
            conn.execute(stmt).fetchall()   # valid SQL

    # -- the queries that used to be assembled as strings --------------------------------------------------------------------------------
    def test_partner_creator_query_binds_both_modes(self):
        from pipeline.pitching import _PARTNER_CREATORS_SQL
        with self.engine.connect() as conn:
            for restrict, niches in ((True, ["music"]), (False, []), (True, [])):
                conn.execute(self.text(_PARTNER_CREATORS_SQL), {"bid": 1, "min_conf": 90, "niches": niches, "restrict_niches": restrict}).fetchall()

    def test_embedding_loaders_use_a_bound_force_flag(self):
        from scripts import embed_test_niches as test_niches_script
        from script_creator_niche import embed_creator_niches as creator_script
        with self.engine.begin() as conn:
            conn.execute(self.text("ALTER TABLE test_niches ADD COLUMN IF NOT EXISTS embedding vector(1024)"))
            conn.execute(self.text("ALTER TABLE creator_niches ADD COLUMN IF NOT EXISTS embedding vector(1024)"))
            conn.execute(self.text("INSERT INTO test_niches (niche, description, embedding) VALUES ('a', 'd', NULL), ('b', 'd', CAST(:v AS vector))"), {"v": "[" + ",".join(["0.1"] * 1024) + "]"})
            conn.execute(self.text("INSERT INTO instagram_users (id, username, user_type) VALUES (1, 'u1', 'contentcreatorRE'), (2, 'u2', 'contentcreatorRE')"))
            conn.execute(self.text("INSERT INTO creator_niches (instagram_user_id, username, niche, description, embedding) VALUES (1, 'u1', 'n', 'd', NULL), (2, 'u2', 'n', 'd', CAST(:v AS vector))"), {"v": "[" + ",".join(["0.1"] * 1024) + "]"})
        self.assertEqual([r["niche"] for r in test_niches_script.load_niches(self.engine, False)], ["a"])
        self.assertEqual(sorted(r["niche"] for r in test_niches_script.load_niches(self.engine, True)), ["a", "b"])
        self.assertEqual([r["username"] for r in creator_script.load_creator_niches(self.engine, False)], ["u1"])
        self.assertEqual(sorted(r["username"] for r in creator_script.load_creator_niches(self.engine, True)), ["u1", "u2"])

    def test_audience_check_queries_keep_their_filters_as_bound_flags(self):
        from gender_check import run_brand_gender_check as gender, run_brand_website_audience_check as audience
        with self.engine.begin() as conn:
            conn.execute(self.text("""INSERT INTO brands_raw (id, name, refferls, geo_reach_score, has_official_website, website, description, beach_brands)
                                      VALUES (1, 'Web', false, 10, true, 'https://web.example', 'a description', false)"""))
            conn.execute(self.text("""INSERT INTO instagram_posts (brand_raw_id, instagram_handle, post_id, sponsorship_confidence)
                                      SELECT 1, 'web', 'p' || g, 95 FROM generate_series(1, 5) g"""))

        def ids(query, **params):
            with self.engine.connect() as conn:
                return [r[0] for r in conn.execute(query, params)]

        # website-audience query: unanalysed only unless forced; beach brands only when asked
        self.assertEqual(ids(audience.BRAND_QUERY, force=False, beach_only=False), [1])
        self.assertEqual(ids(audience.BRAND_QUERY, force=False, beach_only=True), [], "brand 1 is not a beach brand")
        with self.engine.begin() as conn:
            conn.execute(self.text("UPDATE brands_raw SET beach_brands = true, target_audience_gender = 'female' WHERE id = 1"))
        self.assertEqual(ids(audience.BRAND_QUERY, force=False, beach_only=True), [], "already analysed")
        self.assertEqual(ids(audience.BRAND_QUERY, force=True, beach_only=True), [1], "force re-includes analysed brands")
        self.assertEqual(ids(audience.BRAND_QUERY, force=True, beach_only=False), [1])
        # the website-less query takes the same beach flag
        with self.engine.begin() as conn:
            conn.execute(self.text("UPDATE brands_raw SET has_official_website = false, target_audience_gender = NULL, beach_brands = false WHERE id = 1"))
        self.assertEqual(ids(gender.BRAND_QUERY, beach_only=False), [1])
        self.assertEqual(ids(gender.BRAND_QUERY, beach_only=True), [])
        with self.engine.begin() as conn:
            conn.execute(self.text("UPDATE brands_raw SET beach_brands = true WHERE id = 1"))
        self.assertEqual(ids(gender.BRAND_QUERY, beach_only=True), [1])


if __name__ == "__main__":
    unittest.main()
