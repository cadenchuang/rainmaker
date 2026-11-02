import pytest
from monitor import Book


def test_snapshot_and_delta():
    b = Book()
    b.snapshot({"seq": 5, "msg": {"yes_dollars_fp": [["0.48", "10"]], "no_dollars_fp": [["0.49", "20"]]}})
    assert b.top() == ("0.48", "0.51", "0.49", "0.52")
    b.delta({"seq": 6, "msg": {"side": "yes", "price_dollars": "0.48", "delta_fp": "-3"}})
    assert b.valid


def test_sequence_gap_invalidates():
    b = Book()
    b.snapshot({"seq": 5, "msg": {"yes_dollars_fp": [], "no_dollars_fp": []}})
    with pytest.raises(ValueError):
        b.delta({"seq": 7, "msg": {"side": "yes", "price_dollars": "0.40", "delta_fp": "1"}})
    assert not b.valid
