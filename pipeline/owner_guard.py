"""
pipeline/owner_guard.py

Scripts that change the schema or empty a table (ALTER / CREATE / DROP / TRUNCATE) need the table owner. The API's restricted
runtime role (mavlit_app) cannot do that, and the failure it would give ("must be owner of table ...") appears halfway through a
run. require_owner_role() checks up front and says what to do instead. It only runs `SELECT current_user`, so as the owner
a script behaves exactly as before.

The runtime role's name is MAVLIT_RUNTIME_ROLE (default mavlit_app). Importing this module connects to nothing.
"""

import os

from sqlalchemy import text

DEFAULT_RUNTIME_ROLE = "mavlit_app"


class RuntimeRoleError(RuntimeError):
    """The script was started with the API's restricted database role. The message never contains a URL or a password."""


def runtime_role() -> str:
    return (os.environ.get("MAVLIT_RUNTIME_ROLE") or DEFAULT_RUNTIME_ROLE).strip() or DEFAULT_RUNTIME_ROLE


def require_owner_role(bind, script: str = "this script") -> None:
    """
    Raise RuntimeRoleError when `bind` (an Engine, Connection or Session) is connected as the runtime role.
    Does nothing for any other role (the owner, a superuser, a local development role).
    """
    if hasattr(bind, "connect") and not hasattr(bind, "execute"):   # an Engine
        with bind.connect() as conn:
            current = conn.execute(text("SELECT current_user")).scalar()
    else:
        current = bind.execute(text("SELECT current_user")).scalar()
    role = runtime_role()
    if current == role:
        raise RuntimeRoleError(
            f"{script} changes the schema or empties tables, which the API's restricted role {role!r} cannot and must not do. "
            "Run it with the table owner's DATABASE_URL instead (the batch-runner env file, root-only, mode 600):\n"
            "    set -a; . /root/.mavlit-batch.env; set +a\n"
            "then start the script again. See DB_PERMISSIONS.md."
        )
