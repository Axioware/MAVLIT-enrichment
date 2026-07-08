import logging

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def upsert_rows(
    db: Session,
    model,
    rows: list[dict],
    conflict_columns: list[str],
) -> int:
    """
    Bulk-insert rows into model's table, ignoring conflicts on conflict_columns.
    Commits the session and returns the number of newly inserted rows.
    Returns 0 immediately if rows is empty.
    """
    if not rows:
        return 0
    stmt = (
        pg_insert(model)
        .values(rows)
        .on_conflict_do_nothing(index_elements=conflict_columns)
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount
