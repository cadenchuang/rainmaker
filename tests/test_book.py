from decimal import Decimal
import pytest
from orderbook import Book, BookError


def snapshot():
    return {"yes_dollars_fp": [["0.48", "10"]], "no_dollars_fp": [["0.49", "20"]]}


def test_decimal_updates_and_quotes():
    b = Book()
    b.replace_snapshot(snapshot())
    assert b.top() == ("0.48", "0.51", "0.49", "0.52")
    b.apply_delta({"side": "yes", "price_dollars": "0.48", "delta_fp": "0.1"})
    assert b.yes[Decimal('.48')] == Decimal('10.1')
    b.apply_delta({"side": "yes", "price_dollars": "0.48", "delta_fp": "-10.1"})
    assert not b.yes


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '1.01', 0.5])
def test_atomic_invalid_snapshot(value):
    b = Book()
    b.replace_snapshot(snapshot())
    old = b.yes.copy(), b.no.copy()
    with pytest.raises(BookError):
        b.replace_snapshot({"yes_dollars_fp": [], "no_dollars_fp": [[value, '1']]})
    assert (b.yes, b.no) == old
    with pytest.raises(BookError):
        b.quote()


@pytest.mark.parametrize('side,quantity', [('maybe', '1'), ('yes', '-11'), ('no', 'NaN')])
def test_bad_delta_is_atomic(side, quantity):
    b = Book()
    b.replace_snapshot(snapshot())
    old = b.yes.copy(), b.no.copy()
    with pytest.raises(BookError):
        b.apply_delta({'side': side, 'price_dollars': '.48', 'delta_fp': quantity})
    assert (b.yes, b.no) == old
    assert not b.valid


def test_requires_snapshot_and_empty_snapshot():
    b = Book()
    with pytest.raises(BookError):
        b.apply_delta({})
    b.replace_snapshot({'yes_dollars_fp': [], 'no_dollars_fp': []})
    assert b.quote() == (None, None, None, None)
