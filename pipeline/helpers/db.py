import logging
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

logger = logging.getLogger(__name__)


def upsert_rows(
    db: Session,
    model,
    rows: list[dict],
    conflict_columns: list[str] | None,
    index_where: ColumnElement | None = None,
) -> int:
    """
    Bulk-insert rows into model's table, ignoring conflicts on conflict_columns.
    Commits the session and returns the number of newly inserted rows.
    Returns 0 immediately if rows is empty.

    Pass index_where when conflict_columns targets a PARTIAL unique index
    (e.g. instagram_users.username, unique only for user_type='commenter')
    — Postgres requires the ON CONFLICT clause's predicate to match the
    index's, or it can't infer which index you mean.

    Pass conflict_columns=None to skip specifying a target altogether —
    Postgres then applies DO NOTHING for a conflict on ANY unique/exclusion
    constraint on the table, not just one. Use this when a row could
    plausibly violate more than one constraint at once (e.g. instagram_users
    has both a partial unique index on username-where-commenter AND a
    separate global unique constraint on post_id — a commenter's own most
    recent post can already exist under a different username's creator row,
    which a username-only target doesn't catch and crashes on instead of
    skipping).
    """
    if not rows:
        return 0
    insert_stmt = pg_insert(model).values(rows)
    stmt = (
        insert_stmt.on_conflict_do_nothing(index_elements=conflict_columns, index_where=index_where)
        if conflict_columns else
        insert_stmt.on_conflict_do_nothing()
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount
