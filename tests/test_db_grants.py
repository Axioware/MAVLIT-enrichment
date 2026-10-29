"""
Tests of the runtime-role grant plan (pipeline/db_grants.py) that need no database: the plan covers every model table, the
SQLAdmin flags it assumes are the real ones, the generated SQL never grants what must not be granted, and the checked-in
script is exactly what the generator prints.

Run:  python -m pytest tests/test_db_grants.py -q
"""

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
os.environ["DATABASE_URL"] = "postgresql+psycopg2://nobody:nobody@127.0.0.1:1/unreachable"   # config.py needs one; nothing connects
os.environ["RUN_MIGRATIONS"] = ""
os.environ.pop("MIGRATION_DATABASE_URL", None)
sys.path.insert(0, str(ROOT))

from pipeline import db_grants, migrate, migration_settings, migrations  # noqa: E402
from pipeline.migration_settings import MigrationConfigError  # noqa: E402

SCRIPT = ROOT / "scripts" / "sql" / "runtime_role_grants.sql"
ROLLBACK = ROOT / "scripts" / "sql" / "runtime_role_rollback.sql"


def _statements_only(path: Path) -> str:
    """The SQL without comment lines (comments may explain what is excluded)."""
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("--"))


class Plan(unittest.TestCase):
    def test_every_model_table_is_classified(self):   # a new model without a decision fails here, not in production
        from pipeline.db import Base
        model_tables = {t.name for t in Base.metadata.sorted_tables}
        self.assertEqual(model_tables - set(db_grants.CODE), set(), "add the new table to CODE (even as \"\") and to ADMIN")
        self.assertEqual(set(db_grants.CODE) - model_tables, set(), "CODE names a table that is not a model")
        self.assertEqual(set(db_grants.ADMIN), set(db_grants.CODE))

    def test_the_plan_matches_the_live_sqladmin_flags(self):
        import api.app as api_app
        seen = {}
        for view in api_app.admin.views:
            model = getattr(view, "model", None)
            if model is None:
                continue
            seen[model.__table__.name] = (view.can_create, view.can_edit, view.can_delete)
        for view_name in db_grants.VIEWS:
            self.assertEqual(seen.pop(view_name), (False, False, False), f"the admin page of {view_name} must stay read-only")
        for table, flags in db_grants.ADMIN.items():
            self.assertEqual(seen.get(table), flags, f"SQLAdmin flags of {table} changed: update ADMIN in pipeline/db_grants.py")
            # every configured create / edit / delete has its INSERT / UPDATE / DELETE in the plan (SQLAdmin keeps working)
            self.assertTrue(set(db_grants.admin_privileges(flags)) <= set(db_grants.table_privileges()[table]), table)
        self.assertEqual(set(seen) - set(db_grants.ADMIN), set(), "an admin view has no entry in ADMIN")

    def test_only_planned_letters_and_no_dangerous_privileges(self):
        for table, letters in db_grants.table_privileges().items():
            self.assertTrue(set(letters) <= set("SIUD"), table)
            self.assertIn("S", letters, f"{table}: every planned table is at least readable (INSERT .. RETURNING needs SELECT)")
        sql = "\n".join([*db_grants.grant_statements("mavlit_app"), db_grants.sequence_statement("mavlit_app"), db_grants.database_statement("mavlit_app")])
        for word in ("TRUNCATE", "REFERENCES", "TRIGGER", "ALL PRIVILEGES", "OWNER", "SUPERUSER", "CREATE"):
            for line in sql.splitlines():
                if word in line:
                    self.assertTrue(line.startswith("REVOKE"), f"{word} may only appear in a REVOKE: {line}")

    def test_inserting_tables_can_also_select(self):
        for table, letters in db_grants.table_privileges(include_admin=False).items():
            if "I" in letters:
                self.assertIn("S", letters, table)

    def test_tables_that_only_log_stay_narrow(self):
        privileges = db_grants.table_privileges()
        self.assertEqual(privileges["llm_usage"], "SI")
        self.assertEqual(privileges["apify_usage"], "SI")
        self.assertEqual(privileges["creator_brand_llm_v3_rankings"], "SID")
        self.assertEqual(db_grants.VIEWS, {"creator_llm_cost": "S"})

    def test_without_admin_writes_the_plan_is_the_code_requirements_only(self):
        tight = db_grants.table_privileges(include_admin=False)
        self.assertNotIn("test_niches", tight)
        self.assertEqual(tight["prompts"], "S")
        self.assertEqual(tight["rate_estimates"], "SI")

    def test_backup_and_experiment_tables_are_never_mentioned(self):
        statements = _statements_only(SCRIPT)
        for name in ("bak_", "_bak", "backup", "v3cmp_", "llm_model_test_rankings"):
            self.assertNotIn(name, statements)
        self.assertNotIn("ALL TABLES IN SCHEMA", statements)
        self.assertNotIn("ALTER DEFAULT PRIVILEGES", statements)
        self.assertNotIn("TO PUBLIC", statements)
        self.assertIsNone(re.search(r'GRANT\s+"?\w+"?\s+TO\b', statements), "the script must never grant a role membership")

    def test_identifiers_are_validated_and_quoted(self):
        with self.assertRaises(ValueError):
            db_grants.grant_statements('mavlit_app"; DROP TABLE x; --')
        with self.assertRaises(ValueError):
            db_grants.sql_script("mavlit_app", "mavlit_user", "db; DROP DATABASE x")
        self.assertIn('GRANT USAGE ON SCHEMA "public" TO "mavlit_app"', db_grants.grant_statements("mavlit_app"))


class GeneratedScript(unittest.TestCase):
    def test_checked_in_script_is_what_the_generator_prints(self):
        expected = db_grants.sql_script("mavlit_app", "mavlit_user", "mavlit_enrichment_test")
        self.assertEqual(SCRIPT.read_text(), expected, "regenerate: python -m pipeline.db_grants --write scripts/sql/runtime_role_grants.sql")
        result = subprocess.run([sys.executable, "-m", "pipeline.db_grants", "--check", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_script_is_guarded_and_scoped(self):
        text = SCRIPT.read_text()
        self.assertIn("current_database() <> 'mavlit_enrichment_test'", text)
        self.assertIn("pg_has_role(current_user, 'mavlit_user', 'USAGE')", text)
        self.assertEqual(text.count("BEGIN;"), 1)
        self.assertEqual(text.count("COMMIT;"), 1)
        for line in text.splitlines():
            if line.startswith(("GRANT", "REVOKE")):
                self.assertIn('"mavlit_app"', line)
                self.assertTrue('"public"' in line or "DATABASE" in line, line)

    def test_every_planned_table_has_a_reset_and_a_grant(self):
        text = SCRIPT.read_text()
        for table, letters in db_grants.table_privileges().items():
            self.assertIn(f'REVOKE ALL ON TABLE "public"."{table}" FROM "mavlit_app";', text)
            self.assertIn(f'GRANT {db_grants._privs(letters)} ON TABLE "public"."{table}" TO "mavlit_app";', text)
        self.assertIn('GRANT SELECT ON TABLE "public"."creator_llm_cost" TO "mavlit_app";', text)
        self.assertEqual(len(re.findall(r"GRANT .* ON TABLE", text)), len(db_grants.table_privileges()) + 1)

    def test_generation_connects_to_nothing_and_imports_no_database_code(self):
        result = subprocess.run(
            [sys.executable, "-c", "import sys; from pipeline import db_grants; db_grants.sql_script('a','b','c'); print('pipeline.db' in sys.modules, 'sqlalchemy' in sys.modules)"],
            cwd=ROOT, capture_output=True, text=True, env={**os.environ, "DATABASE_URL": ""})
        self.assertEqual(result.stdout.strip(), "False False", result.stderr)


class MigrationIntegration(unittest.TestCase):
    def test_grant_role_setting(self):
        self.assertIsNone(migration_settings.resolve_grant_role(None, {}))
        self.assertEqual(migration_settings.resolve_grant_role(None, {"MIGRATION_GRANT_ROLE": "mavlit_app"}), "mavlit_app")
        self.assertEqual(migration_settings.resolve_grant_role("other_role", {"MIGRATION_GRANT_ROLE": "mavlit_app"}), "other_role")
        for bad in ('x"; drop', "Upper", "a b", "1abc"):
            with self.assertRaises(MigrationConfigError):
                migration_settings.resolve_grant_role(bad, {})

    def test_the_grant_role_can_live_in_the_migration_env_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "migrate.env"
            path.write_text("MIGRATION_DATABASE_URL=postgresql://u:p@h/db\nMIGRATION_GRANT_ROLE=mavlit_app\n")
            path.chmod(0o600)
            target = migration_settings.resolve_migration_target(env_file=str(path), environ={})
            self.assertEqual(migration_settings.resolve_grant_role(None, {}, target.file_values), "mavlit_app")
            self.assertEqual(migration_settings.resolve_grant_role("other_role", {}, target.file_values), "other_role")
            with mock.patch.object(migrate, "run_migrations") as run_mock, mock.patch.object(migrate, "build_engine"):
                self.assertEqual(migrate.main(["--env-file", str(path)]), 0)
            self.assertEqual(run_mock.call_args.args[2], "mavlit_app")
        self.assertIsNone(migration_settings.resolve_grant_role(None, {}, None))

    def test_a_refused_grant_step_exits_with_the_findings_and_no_traceback(self):
        error = db_grants.GrantAuditError("the runtime role mavlit_app would not have exactly the planned access; nothing was changed:\n  - mavlit_app: is a member of the role mavlit_user\n      fix: REVOKE ...")
        with mock.patch.object(migrate, "run_migrations", side_effect=error), mock.patch.object(migrate, "build_engine"), \
                mock.patch.dict(os.environ, {"MIGRATION_DATABASE_URL": "postgresql://u:p@h/db"}), self.assertLogs("pipeline.migrate", level="ERROR") as logs:
            self.assertEqual(migrate.main(["--grant-role", "mavlit_app"]), 1)
        text = "\n".join(logs.output)
        self.assertIn("member of the role mavlit_user", text)
        self.assertIn("migrations themselves are done", text)

    def test_the_command_passes_the_role_and_defaults_to_no_grants(self):
        with mock.patch.object(migrate, "run_migrations") as run_mock, mock.patch.object(migrate, "build_engine"):
            with mock.patch.dict(os.environ, {"MIGRATION_DATABASE_URL": "postgresql://u:p@h/db"}):
                migrate.main(["--grant-role", "mavlit_app"])
                migrate.main([])
        self.assertEqual([c.args[2] for c in run_mock.call_args_list], ["mavlit_app", None])

    def test_dry_run_reports_the_role_without_connecting(self):
        with mock.patch.object(migrate, "run_migrations") as run_mock, self.assertLogs("pipeline.migrate", level="INFO") as logs:
            self.assertEqual(migrate.main(["--dry-run", "--grant-role", "mavlit_app"]), 0)
        run_mock.assert_not_called()
        self.assertIn("re-grant the runtime role mavlit_app", "\n".join(logs.output))

    def test_the_api_startup_never_grants(self):
        import api.app as api_app
        with mock.patch.object(api_app, "run_migrations") as shared:
            api_app._run_migrations()
        shared.assert_called_once_with()

    def test_the_view_grant_runs_right_after_the_view_is_created(self):
        source = (ROOT / "pipeline" / "migrations.py").read_text()
        create = source.index("CREATE VIEW creator_llm_cost AS")
        step = source.index("_grant_creator_llm_cost(grant_role),")
        self.assertLess(create, step)
        self.assertLess(step - create, 4500)
        self.assertEqual(source[create:step].count("DROP VIEW"), 0)

    def test_an_invalid_role_is_rejected_before_any_sql(self):
        with self.assertRaises(ValueError):
            migrations.run_migrations(mock.MagicMock(), None, 'bad"role')

    def test_apply_grants_runs_the_scripts_steps_then_the_audit(self):
        executed = []

        class Result(list):
            def all(self):
                return list(self)

        class Conn:
            def exec_driver_sql(self, statement, params=None):
                executed.append(statement)
                return Result()

            def execute(self, statement, params=None):
                return Result([("llm_usage",), ("creator_llm_cost",), ("bak_20261009_foo",), ("brand_new_table",)])

        unclassified = db_grants.apply_grants(Conn(), "mavlit_app")
        joined = "\n".join(executed)
        self.assertEqual(unclassified, ["bak_20261009_foo", "brand_new_table"])
        self.assertIn('GRANT SELECT, INSERT ON TABLE "public"."llm_usage" TO "mavlit_app"', joined)
        self.assertIn('GRANT SELECT ON TABLE "public"."creator_llm_cost" TO "mavlit_app"', joined)
        self.assertNotIn("bak_20261009_foo\" TO", joined)
        self.assertNotIn("brand_new_table\" TO", joined)
        order = [joined.index(marker) for marker in ("DO $planned$", "DO $database$", 'GRANT USAGE ON SCHEMA', "DO $sequences$", "DO $stray$")]
        self.assertEqual(order, sorted(order), "same order as the script")
        self.assertTrue(executed[-1].startswith("WITH app AS"), "the audit runs last")
        self.assertIsNone(re.search(r"ALTER DEFAULT PRIVILEGES|TO PUBLIC", joined))

    def test_a_violation_aborts_with_the_finding_and_its_fix(self):
        class Result(list):
            def all(self):
                return list(self)

        class Conn:
            def exec_driver_sql(self, statement, params=None):
                if statement.startswith("WITH app AS"):
                    return Result([("violation", "mavlit_app", "is a member of the role mavlit_user", "REVOKE mavlit_user FROM mavlit_app"),
                                   ("warning", "database x", "TEMPORARY", "optional")])
                return Result()

        with self.assertRaises(db_grants.GrantAuditError) as ctx:
            db_grants.apply_grants(Conn(), "mavlit_app")
        message = str(ctx.exception)
        self.assertIn("member of the role mavlit_user", message)
        self.assertIn("fix: REVOKE mavlit_user FROM mavlit_app", message)
        self.assertNotIn("TEMPORARY", message, "warnings do not abort")

    def test_the_audit_checks_what_revoking_direct_grants_cannot_remove(self):
        sql = db_grants.audit_sql("mavlit_app")
        for needle in ("pg_has_role(app.oid, g.oid, 'MEMBER')", "pg_default_acl", "relowner", "nspowner", "datdba", "rolsuper", "rolcreatedb",
                       "rolbypassrls", "has_any_column_privilege", "has_schema_privilege", "has_database_privilege", "has_sequence_privilege"):
            self.assertIn(needle, sql)

    def test_the_sql_text_is_safe_to_run_through_a_database_driver(self):
        for name, sql in {"stray": db_grants.stray_revoke_statement("mavlit_app"), "sequences": db_grants.sequence_statement("mavlit_app"),
                          "database": db_grants.database_statement("mavlit_app"), "planned": db_grants.guard_planned_objects_sql("public"),
                          "audit": db_grants.audit_sql("mavlit_app"), "audit block": db_grants.audit_block_sql("mavlit_app")}.items():
            self.assertNotIn("%", sql, f"{name}: a percent sign would be read as a parameter by the driver")
            self.assertIsNone(re.search(r"(?<!:):[a-z_]+\b", sql), f"{name}: a :name would be read as a bind parameter")


class RollbackScript(unittest.TestCase):
    def test_checked_in_rollback_is_what_the_generator_prints(self):
        self.assertEqual(ROLLBACK.read_text(), db_grants.rollback_sql("mavlit_app", "mavlit_user", "mavlit_enrichment_test"),
                         "regenerate: python -m pipeline.db_grants --rollback --write scripts/sql/runtime_role_rollback.sql")
        result = subprocess.run([sys.executable, "-m", "pipeline.db_grants", "--rollback", "--check", str(ROLLBACK)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rollback_only_revokes_and_never_destroys(self):
        statements = _statements_only(ROLLBACK)
        for forbidden in ("DROP ", "DELETE", "TRUNCATE", "ALTER ROLE", "ALTER DEFAULT", "CREATE ROLE", "TO PUBLIC", "FROM PUBLIC", "GRANT "):
            self.assertNotIn(forbidden, statements, forbidden)
        self.assertIn("REVOKE ALL ON ALL TABLES IN SCHEMA", statements)
        self.assertIn("REVOKE ALL ON ALL SEQUENCES IN SCHEMA", statements)
        self.assertIn("REVOKE ALL ON SCHEMA", statements)
        self.assertIn("REVOKE ALL ON DATABASE", statements)
        self.assertIn("current_database() <> 'mavlit_enrichment_test'", statements)
        self.assertIn("pg_has_role(current_user, 'mavlit_user', 'USAGE')", statements)
        for line in statements.splitlines():
            if line.startswith("REVOKE"):
                self.assertTrue(line.rstrip(";").endswith('FROM "mavlit_app"') or "FROM " in line and '"mavlit_app"' in line, line)
        self.assertEqual(statements.count("BEGIN;"), 1)
        self.assertEqual(statements.count("COMMIT;"), 1)


if __name__ == "__main__":
    unittest.main()
