"""Several columns' DISTINCT values in one pass over the table.

Dropdown screens (product options, physical-audit filters) ask a dozen
columns of the products table for their distinct values. As a dozen
`SELECT DISTINCT col` statements that is a dozen scans of 400K products. On
PostgreSQL this asks once, `GROUP BY GROUPING SETS ((c1), (c2), …)`: one scan,
one group per distinct value of each column, and GROUPING(ck) = 0 marks which
column a row belongs to. GROUP BY and DISTINCT group with the same equality, so
the values are the same values (NULL included, once). Elsewhere (SQLite) the
per-column statements are kept.
"""
from sqlalchemy import func


def distinct_values(query, columns):
    """{column.key: set of distinct values} for `columns` over `query`'s rows.

    `query` is a Query carrying the filters (its selected entities are replaced)."""
    columns = list(columns)
    if not columns:
        return {}
    bind = query.session.get_bind()
    if bind.dialect.name != "postgresql":
        return {c.key: {v for (v,) in query.with_entities(c).distinct()} for c in columns}
    marks = [func.grouping(c) for c in columns]
    q = (query.with_entities(*columns, *marks)
              .group_by(func.grouping_sets(*columns)))      # one single-column set each
    out = {c.key: set() for c in columns}
    n = len(columns)
    for row in q:
        vals, flags = row[:n], row[n:]
        for k in range(n):
            if flags[k] == 0:
                out[columns[k].key].add(vals[k])
                break
    return out
