"""Calendar-day filters on timestamp columns, written so an index can answer them.

`func.date(col) >= start` asks the database to compute date() for EVERY row
before it can compare, so the index on the column is never used and each report
figure is a pass over the whole table (726K invoices in the migrated shop).
The same condition, for a timestamp, is a range on the column itself:

    date(col) >= d    ⇔    col >= d 00:00
    date(col) <= d    ⇔    col <  (d + 1 day) 00:00

— exact for PostgreSQL's `timestamp` (checked day by day over the whole
invoice history: same rows, same totals).

The range is used only where that equivalence is certain: PostgreSQL, a
DateTime column, and a plain `date` bound. Anything else — a string bound, a
Date column, SQLite, where timestamps are text and a bare "YYYY-MM-DD" stored in
a DateTime column would compare differently — gets the original date()
expression, unchanged.
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy import DateTime, func

from app import db


def _ranged(col, d):
    return (type(d) is date
            and isinstance(getattr(col, "type", None), DateTime)
            and db.engine.dialect.name == "postgresql")


def day_from(col, d):
    """`date(col) >= d`."""
    if _ranged(col, d):
        return col >= datetime.combine(d, time.min)
    return func.date(col) >= d


def day_to(col, d):
    """`date(col) <= d`."""
    if _ranged(col, d):
        return col < datetime.combine(d + timedelta(days=1), time.min)
    return func.date(col) <= d


def day_on(col, d):
    """`date(col) == d`."""
    if _ranged(col, d):
        return db.and_(day_from(col, d), day_to(col, d))
    return func.date(col) == d
