"""
pipeline/migrate.py

Run the database migrations on their own:

    python -m pipeline.migrate                        # apply them
    python -m pipeline.migrate --dry-run              # check the settings; connects to nothing
    python -m pipeline.migrate --env-file /root/.mavlit-migrate.env --lock-timeout-ms 5000

Which database: the migration-only settings of pipeline/migration_settings.py (a root-only env file, or the
MIGRATION_DATABASE_URL variable of this process). Without one it stops with exit code 2: it never falls back to the
runtime DATABASE_URL, which is the restricted application role. The API never reads those settings. RUN_MIGRATIONS only controls the API's startup and has no effect here.

Exit codes: 0 done, 1 failed, 2 bad configuration, 3 a lock could not be acquired in time (the step was rolled back;
run it again). Neither a URL nor a password is ever printed. Importing this module starts nothing and connects to nothing.
"""

import argparse
import logging
import os
import sys

from pipeline.db_grants import GrantAuditError
from pipeline.migration_settings import (
    MigrationConfigError,
    build_engine,
    redact,
    resolve_grant_role,
    resolve_lock_timeout_ms,
    resolve_migration_target,
)
from pipeline.migrations import MigrationLockTimeout, run_migrations

logger = logging.getLogger("pipeline.migrate")

EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_LOCK_TIMEOUT = 0, 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply the MAVLIT database migrations.")
    parser.add_argument("--dry-run", action="store_true", help="validate the settings and say what would be used; connect to nothing")
    parser.add_argument("--env-file", help="file that defines MIGRATION_DATABASE_URL (must be chmod 600)")
    parser.add_argument("--lock-timeout-ms", help="give up if a table lock is not acquired within this many ms (0 = no limit; default 10000)")
    parser.add_argument("--grant-role", help="after migrating, give this runtime role exactly the grants of pipeline/db_grants.py (default: grant nothing)")
    parser.add_argument("--traceback", action="store_true", help="on failure also print the traceback (may name the database host and user)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        target = resolve_migration_target(env_file=args.env_file)
        lock_timeout_ms = resolve_lock_timeout_ms(args.lock_timeout_ms)
        grant_role = resolve_grant_role(args.grant_role, file_values=target.file_values)
    except MigrationConfigError as exc:
        logger.error("Migration configuration error: %s", exc)
        return EXIT_CONFIG

    limit = f"{lock_timeout_ms} ms" if lock_timeout_ms else "no limit"
    grants = f"re-grant the runtime role {grant_role}" if grant_role else "no grants"
    if args.dry_run:
        logger.info("Dry run: the migrations would use %s, lock timeout %s, %s (nothing was connected to)", target.source, limit, grants)
        return EXIT_OK

    logger.info("Running migrations with %s, lock timeout %s, %s", target.source, limit, grants)
    secrets = (target.url, os.environ.get("DATABASE_URL"))
    engine = None
    try:
        engine = build_engine(target.url)
        run_migrations(engine, lock_timeout_ms, grant_role)
    except MigrationConfigError as exc:
        logger.error("Migration configuration error: %s", exc)
        return EXIT_CONFIG
    except MigrationLockTimeout as exc:
        logger.error("Migrations stopped: %s", exc)
        return EXIT_LOCK_TIMEOUT
    except GrantAuditError as exc:   # the schema is migrated, but the runtime role's grants were refused and left unchanged
        logger.error("Runtime-role grants refused (the migrations themselves are done): %s", exc)
        return EXIT_FAILED
    except Exception as exc:
        logger.error("Migrations failed: %s: %s", type(exc).__name__, redact(str(exc), *secrets)[:2000])
        if args.traceback:
            logger.error("%s", redact(_format_traceback(exc), *secrets))
        return EXIT_FAILED
    finally:
        if engine is not None:
            engine.dispose()
    logger.info("Migrations finished")
    return EXIT_OK


def _format_traceback(exc: BaseException) -> str:
    import traceback
    return "".join(traceback.format_exception(exc))


if __name__ == "__main__":
    sys.exit(main())
