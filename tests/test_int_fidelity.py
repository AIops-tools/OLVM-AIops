"""Integer quantities must stay exact integers.

Line-wide invariant (bug class #4), asserted per repo because the helper is
vendored per repo. Routing an int64 through float64 collapses distinct values
above 2**53 onto one displayed number. This repo's contract for anything that
is not a quantity is ``None`` (not reported), never 0.

The bool case is not pedantry: ``bool`` subclasses ``int``, so an int
short-circuit placed before a bool guard would return ``True`` unchanged.
"""

from __future__ import annotations

import pytest

from olvm_aiops.ops._util import as_int


@pytest.mark.unit
def test_as_int_never_round_trips_an_int_through_float64():
    hi = 9007199254740993  # 2**53 + 1 — the smallest int a float64 cannot hold
    lo = 9007199254740992
    assert as_int(hi) == hi
    assert as_int(hi) != as_int(lo), "distinct counts must not collapse"
    assert int(float(hi)) == int(float(lo))  # what the float path would do


@pytest.mark.unit
def test_as_int_rejects_bool():
    assert as_int(True) is None
    assert as_int(False) is None


@pytest.mark.unit
def test_as_int_missing_is_none_not_zero():
    assert as_int(None) is None
    assert as_int(0) == 0
    assert isinstance(as_int(12.0), int) and as_int(12.0) == 12
    assert as_int(12.5) is None
