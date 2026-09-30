"""One page of a long list, for the screens that used to send the whole table.

The shop's catalogue is the warehouse's — 416K products after the migration —
and its customer book is 150K. Screens that rendered all of it (a QR code per
row on Inventory) took minutes, and while they rendered every other request
waited behind them. Those screens now show PER_PAGE rows at a time, with the
same search and filters, and the pager (templates/_pager.html) carries every
filter from page to page.

The ORDER BY a caller passes must be total (end it with the id), so a row can
never appear on two pages or on none.
"""
from flask import request

PER_PAGE = 100


def paged(query, per_page=PER_PAGE):
    """`query` (ordered) → a Flask-SQLAlchemy Pagination for ?page=N.

    `.items` are this page's rows, `.total` the full count — the count is one
    COUNT(*) over the same filters, so "showing 1–100 of 416,873" is exact."""
    page = request.args.get("page", 1, type=int) or 1
    return query.paginate(page=max(1, page), per_page=per_page, error_out=False, count=True)
