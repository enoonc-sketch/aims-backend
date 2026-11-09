"""Shared `skip`/`limit` bounds for list endpoints.

Before this, most list endpoints declared plain `skip: int = 0` and
`limit: int = 100` with no validation at all — every router picked its own
(or no) upper bound, and nothing stopped a caller from passing `skip=-1`
(undefined SQL OFFSET behaviour) or `limit=10_000_000` (an accidental
full-table dump). `SkipQuery` / `LimitQuery` give every endpoint the same,
explicit floor and ceiling instead of each route inventing its own.

Usage:
    from app.api.pagination import LimitQuery, SkipQuery

    def list_things(skip: int = SkipQuery, limit: int = LimitQuery(100)):
        ...

`LimitQuery(default)` keeps each endpoint's existing default page size (so
behaviour for well-formed callers is unchanged) while capping every endpoint
at the same `MAX_LIMIT` unless a route has a documented reason to differ.
"""
from fastapi import Query

MAX_LIMIT = 500

SkipQuery = Query(0, ge=0, description="Number of rows to skip")


def LimitQuery(default: int = 100, *, max_limit: int = MAX_LIMIT):
    """A `limit` query parameter bounded to [1, max_limit], defaulting to `default`."""
    return Query(default, ge=1, le=max_limit, description=f"Max rows to return (1-{max_limit})")
