import logging
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

logger = logging.getLogger(__name__)


def upsert_rows(
    db: Session,
    model,
    rows: list[dict],
    conflict_columns: list[str],
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
    """
    if not rows:
        return 0
    stmt = (
        pg_insert(model)
        .values(rows)
        .on_conflict_do_nothing(index_elements=conflict_columns, index_where=index_where)
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount
