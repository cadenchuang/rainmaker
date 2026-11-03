from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import json
import sqlite3
import threading

import pytest
from approval import ApprovalService, Refused, RiskLimits, TradeProposal
from simulation import FixtureFeed, SimulationAdapter, proposal_fixture


class Clock:
    def __init__(self):
        self.now = 1_800_000_000_000_000_000
    def __call__(self):
        return self.now


@pytest.fixture
def system(tmp_path):
    clock = Clock()
    feed = FixtureFeed(clock=clock, refresh=False)
    adapter = SimulationAdapter()
    service = ApprovalService(tmp_path / 'approval.sqlite', feed, adapter, clock=clock)
    return service, feed, adapter, clock


def create(system, **kwargs):
    service, feed, _, clock = system
    p = proposal_fixture(feed, 'SIM-KXSENATE-PA', clock=clock, **kwargs)
    service.create(p)
    return p


def state(service, pid):
    return next(row['state'] for row in service.listing() if row['id'] == pid)


def test_proposal_is_immutable_and_exact(system):
    p = create(system)
    assert p.limit_price == Decimal('.55') and p.max_principal == Decimal('2.75')
    with pytest.raises(FrozenInstanceError):
        p.quantity = 100
    with system[0].transaction() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE proposals SET body='{}' WHERE id=?", (p.proposal_id,))


@pytest.mark.parametrize('decision', ['none', 'reject'])
def test_unapproved_and_rejected_never_submit(system, decision):
    service, _, adapter, _ = system
    p = create(system)
    if decision == 'reject':
        service.reject(p.proposal_id, p.digest)
    expected = 'REJECTED' if decision == 'reject' else 'PROPOSED'
    assert service.simulate(p.proposal_id, p.digest) == expected
    assert not adapter.submissions and not service.orders()


def test_approval_then_submission_are_distinct_and_audited(system):
    service, _, adapter, _ = system
    p = create(system)
    assert service.approve(p.proposal_id, p.digest) == 'APPROVED'
    assert not adapter.submissions
    assert service.simulate(p.proposal_id, p.digest) == 'SIMULATED_FILLED'
    assert service.simulate(p.proposal_id, p.digest) == 'SIMULATED_FILLED'
    assert len(adapter.submissions) == 1
    with service.transaction() as db:
        events = [r[0] for r in db.execute('SELECT kind FROM audit ORDER BY ordinal')]
        assert events[:5] == ['PROPOSED', 'APPROVED', 'REVALIDATING', 'SIMULATED_SUBMISSION', 'SIMULATED_FILLED']
        assert db.execute('SELECT count(*) FROM fills').fetchone()[0] == 1
        assert db.execute('SELECT quantity FROM positions').fetchone()[0] == p.quantity
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM audit")


@pytest.mark.parametrize('approve_first', [False, True])
def test_expiration_prevents_submission(system, approve_first):
    service, _, adapter, clock = system
    p = create(system)
    if approve_first:
        service.approve(p.proposal_id, p.digest)
    clock.now = p.expires_ns
    if not approve_first:
        assert service.approve(p.proposal_id, p.digest) == 'EXPIRED'
    assert service.simulate(p.proposal_id, p.digest) == 'EXPIRED'
    assert not adapter.submissions


def test_changed_terms_or_hash_cannot_reuse_approval(system):
    service, _, adapter, _ = system
    p = create(system)
    altered = replace(p, quantity=4)
    with pytest.raises(Refused):
        service.approve(p.proposal_id, altered.digest)
    service.approve(p.proposal_id, p.digest)
    with pytest.raises(Refused):
        service.simulate(p.proposal_id, altered.digest)
    with service.transaction() as db:
        db.execute('DROP TRIGGER immutable_proposal')
        db.execute('UPDATE proposals SET body=? WHERE id=?', (altered.body(), p.proposal_id))
    with pytest.raises(Refused, match='integrity'):
        service.simulate(p.proposal_id, p.digest)
    assert not adapter.submissions


@pytest.mark.parametrize('change', [
    {'synchronized': False}, {'recording_status': 'OPEN'}, {'reconciliation_status': 'UNKNOWN'},
    {'received_ns': None}, {'source_ns': None}, {'received_ns': 0}, {'source_ns': 0},
    {'identity_verified': False}, {'rules_verified': False}, {'rules': 'Changed settlement rules'},
    {'country': 'CA'}, {'office': 'PRESIDENT'}, {'yes_bids': (), 'no_bids': ()},
    {'fee_per_contract': Decimal('1')},
])
def test_missing_invalid_stale_or_changed_market_blocks(system, change):
    service, feed, adapter, _ = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    feed.markets[p.market_ticker] = replace(feed.markets[p.market_ticker], **change)
    assert service.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert not adapter.submissions and not service.orders()
    assert next(r['reason'] for r in service.listing() if r['id'] == p.proposal_id)


@pytest.mark.parametrize('scenario,target', [('timeout', 'RECONCILING'), ('ambiguous', 'RECONCILING'),
                                           ('ack', 'SIMULATED_ACKNOWLEDGED'), ('reject', 'SIMULATED_REJECTED')])
def test_fake_scenarios_never_blindly_retry(system, scenario, target):
    service, _, adapter, _ = system
    p = create(system, scenario=scenario)
    service.approve(p.proposal_id, p.digest)
    assert service.simulate(p.proposal_id, p.digest) == target
    cid = service.orders()[0]['client_id']
    for _ in range(3):
        service.approve(p.proposal_id, p.digest)
        service.simulate(p.proposal_id, p.digest)
    assert len(adapter.submissions) == 1 and service.orders()[0]['client_id'] == cid
    if scenario in ('timeout', 'ambiguous'):
        other = create(system)
        service.approve(other.proposal_id, other.digest)
        assert service.simulate(other.proposal_id, other.digest) == 'BLOCKED'
        reconciled = service.reconcile(p.proposal_id, p.digest)
        assert reconciled == ('SIMULATED_FILLED' if scenario == 'ambiguous' else 'RECONCILING')
        assert len(adapter.submissions) == 1


def test_concurrent_approvals_and_duplicate_simulation_are_idempotent(system):
    service, _, adapter, _ = system
    p = create(system)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(lambda _: service.approve(p.proposal_id, p.digest), range(16))) == {'APPROVED'}
        list(pool.map(lambda _: service.simulate(p.proposal_id, p.digest), range(16)))
    assert state(service, p.proposal_id) == 'SIMULATED_FILLED'
    assert len(adapter.submissions) == len(service.orders()) == 1
    with service.transaction() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE kind='APPROVED'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM fills').fetchone()[0] == 1


def test_restart_preserves_approval_and_does_not_dispatch(system):
    service, feed, _, clock = system
    unapproved = create(system)
    approved = create(system)
    service.approve(approved.proposal_id, approved.digest)
    new_adapter = SimulationAdapter()
    restarted = ApprovalService(service.path, feed, new_adapter, clock=clock)
    assert state(restarted, unapproved.proposal_id) == 'PROPOSED'
    assert state(restarted, approved.proposal_id) == 'APPROVED'
    assert not new_adapter.submissions


def test_interrupted_submission_enters_reconciliation_and_never_retries(system, monkeypatch):
    service, feed, adapter, clock = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    def crash(client_id, proposal):
        raise SystemExit('Synthetic process interruption')
    monkeypatch.setattr(adapter, 'submit', crash)
    with pytest.raises(SystemExit):
        service.simulate(p.proposal_id, p.digest)
    cid = service.orders()[0]['client_id']
    new_adapter = SimulationAdapter()
    restarted = ApprovalService(service.path, feed, new_adapter, clock=clock)
    assert state(restarted, p.proposal_id) == 'RECONCILING'
    assert restarted.orders()[0]['client_id'] == cid
    restarted.simulate(p.proposal_id, p.digest)
    assert restarted.reconcile(p.proposal_id, p.digest) == 'RECONCILING'
    assert not new_adapter.submissions


def test_unknown_account_and_risk_limits_fail_closed(system):
    service, _, adapter, _ = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    with service.transaction() as db:
        db.execute('UPDATE account SET known=0 WHERE id=1')
    assert service.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert not adapter.submissions


@pytest.mark.parametrize('limits', [RiskLimits(max_quantity=1), RiskLimits(max_principal=Decimal('1')),
                                   RiskLimits(max_fee=Decimal('.01')), RiskLimits(max_exposure=Decimal('1'))])
def test_each_risk_cap_is_enforced(system, limits):
    service, _, adapter, _ = system
    service.limits = limits
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    assert service.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert not adapter.submissions


def test_out_of_scope_and_sell_without_inventory_refused(system):
    service, feed, adapter, clock = system
    with pytest.raises(Refused):
        proposal_fixture(feed, 'PRESIDENT', clock=clock)
    p = create(system, side='SELL', price='.45')
    service.approve(p.proposal_id, p.digest)
    assert service.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert not adapter.submissions


def test_covered_sell_and_governor_fixture(system):
    service, feed, adapter, clock = system
    ticker = 'SIM-KXGOVERNOR-MI'
    buy = proposal_fixture(feed, ticker, clock=clock)
    service.create(buy)
    service.approve(buy.proposal_id, buy.digest)
    assert service.simulate(buy.proposal_id, buy.digest) == 'SIMULATED_FILLED'
    sell = proposal_fixture(feed, ticker, clock=clock, side='SELL', price='.45')
    service.create(sell)
    service.approve(sell.proposal_id, sell.digest)
    assert service.simulate(sell.proposal_id, sell.digest) == 'SIMULATED_FILLED'
    with service.transaction() as db:
        assert db.execute('SELECT quantity FROM positions').fetchone()[0] == 0


def test_no_non_simulation_dependency_injection(system):
    service, feed, adapter, clock = system
    with pytest.raises(Refused):
        ApprovalService(service.path, feed, object(), clock=clock)
    with pytest.raises(Refused):
        ApprovalService(service.path, object(), adapter, clock=clock)


@pytest.mark.parametrize('change', [
    {'no_bids': ((Decimal('1.1'), 100),)},
    {'yes_bids': ((Decimal('.4'), -10),)},
    {'yes_bids': ((Decimal('.4'), 3), (Decimal('.4'), 3))},
    {'no_bids': ((Decimal('NaN'), 100),)},
    {'received_ns': 1.8e18},
])
def test_invalid_book_structure_is_never_usable(system, change):
    service, feed, adapter, _ = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    feed.markets[p.market_ticker] = replace(feed.markets[p.market_ticker], **change)
    assert service.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert not adapter.submissions


@pytest.mark.parametrize('mode', ['stale', 'expired'])
def test_slow_claim_commit_requires_immediate_revalidation(system, monkeypatch, mode):
    service, _, adapter, clock = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    gate = service._gate
    def delayed(db, proposal, now, **kwargs):
        result = gate(db, proposal, now, **kwargs)
        if not kwargs:
            clock.now = p.expires_ns if mode == 'expired' else now + service.limits.max_age_ns + 1
        return result
    monkeypatch.setattr(service, '_gate', delayed)
    assert service.simulate(p.proposal_id, p.digest) == 'SIMULATED_REJECTED'
    assert not adapter.submissions
    assert service.orders()[0]['state'] == 'REJECTED'


@pytest.mark.parametrize('result', [None, {'state': 'FILLED', 'price': '0.99', 'quantity': 5, 'fee': '0.10'},
                                    {'state': 'FILLED', 'price': '.55', 'quantity': 6, 'fee': '.10'},
                                    {'state': 'FILLED', 'price': '.55', 'quantity': 5, 'fee': '5'}])
def test_invalid_fake_results_remain_reserved_and_ambiguous(system, monkeypatch, result):
    service, _, adapter, _ = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    monkeypatch.setattr(adapter, 'submit', lambda *args: result)
    assert service.simulate(p.proposal_id, p.digest) == 'RECONCILING'
    assert service.orders()[0]['state'] == 'UNKNOWN'
    with service.transaction() as db:
        assert db.execute('SELECT count(*) FROM fills').fetchone()[0] == 0


def test_concurrent_proposals_cannot_overreserve_exposure(system):
    service, _, _, _ = system
    service.limits = RiskLimits(max_exposure=Decimal('10'))
    proposals = [create(system, scenario='ack') for _ in range(4)]
    for p in proposals:
        service.approve(p.proposal_id, p.digest)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda p: service.simulate(p.proposal_id, p.digest), proposals))
    orders = service.orders()
    assert len(orders) == 1 and orders[0]['state'] == 'ACKNOWLEDGED'
    assert sum(Decimal(o['reservation']) for o in orders) <= service.limits.max_exposure


def test_missing_approval_binding_and_clock_regression_fail_closed(system):
    service, _, adapter, clock = system
    p = create(system)
    with service.transaction() as db:
        db.execute("UPDATE proposals SET state='APPROVED' WHERE id=?", (p.proposal_id,))
    assert service.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert not adapter.submissions
    clock.now -= 1
    with pytest.raises(Refused, match='Clock regression'):
        service.listing()


@pytest.mark.parametrize('name', ['.env', 'private.pem', 'private.key'])
def test_credential_paths_are_not_valid_simulation_databases(system, tmp_path, name):
    service, feed, adapter, clock = system
    with pytest.raises(Refused):
        ApprovalService(tmp_path / name, feed, adapter, clock=clock)


def test_existing_unrelated_database_is_not_a_known_empty_account(system, tmp_path):
    service, feed, adapter, clock = system
    path = tmp_path / 'unrelated.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE recording_runs (run_id TEXT)')
    with pytest.raises(Refused, match='Unknown ledger schema'):
        ApprovalService(path, feed, adapter, clock=clock)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='account'").fetchone()[0] == 0


def test_reconciliation_timeout_holds_exposure_without_resubmission(system, monkeypatch):
    service, _, adapter, _ = system
    p = create(system, scenario='ack')
    service.approve(p.proposal_id, p.digest)
    assert service.simulate(p.proposal_id, p.digest) == 'SIMULATED_ACKNOWLEDGED'
    def timeout(client_id):
        raise TimeoutError('Synthetic lookup timeout')
    monkeypatch.setattr(adapter, 'lookup', timeout)
    assert service.reconcile(p.proposal_id, p.digest) == 'RECONCILING'
    assert service.orders()[0]['state'] == 'UNKNOWN'
    assert not service.exposure_status()[0]
    assert len(adapter.submissions) == 1


class AdvancingClock(Clock):
    def __call__(self):
        self.now += 100
        return self.now


def test_default_refresh_feed_accepts_advancing_clock(tmp_path):
    clock = AdvancingClock()
    feed = FixtureFeed(clock=clock)  # refresh=True is the dashboard default.
    adapter = SimulationAdapter()
    service = ApprovalService(tmp_path / 'moving-clock.sqlite', feed, adapter, clock=clock)
    p = proposal_fixture(feed, 'SIM-KXSENATE-PA', clock=clock)
    service.create(p)
    service.approve(p.proposal_id, p.digest)
    assert service.simulate(p.proposal_id, p.digest) == 'SIMULATED_FILLED'
    assert len(adapter.submissions) == 1


@pytest.mark.parametrize('field', ['received_ns', 'source_ns'])
def test_genuinely_future_observation_is_refused(system, field):
    service, feed, adapter, clock = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    feed.markets[p.market_ticker] = replace(feed.markets[p.market_ticker], **{field: clock.now + 1})
    assert service.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert not adapter.submissions


@pytest.mark.parametrize('mode', ['expired', 'stale'])
def test_last_preflight_commit_temporal_barrier(system, monkeypatch, mode):
    from contextlib import contextmanager
    service, feed, adapter, clock = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    entered, release = threading.Event(), threading.Event()
    original = service.transaction
    count = 0

    @contextmanager
    def gated_transaction():
        nonlocal count
        count += 1
        ordinal = count
        with original() as db:
            yield db
            if ordinal == 3:  # Last preflight, after its checks and BEFORE commit.
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('Test did not release preflight commit')

    monkeypatch.setattr(service, 'transaction', gated_transaction)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(service.simulate, p.proposal_id, p.digest)
        try:
            assert entered.wait(2)
            assert not adapter.submissions
            # The reservation/attempt is already durable, despite preflight being held.
            with sqlite3.connect(service.path) as db:
                assert db.execute('SELECT state FROM orders').fetchone()[0] == 'SUBMITTING'
            clock.now = (p.expires_ns if mode == 'expired' else
                         feed.markets[p.market_ticker].received_ns + service.limits.max_age_ns + 1)
            release.set()
            assert result.result(2) == 'SIMULATED_REJECTED'
        finally:
            release.set()
    assert not adapter.submissions
    assert service.orders()[0]['state'] == 'REJECTED'
    assert service.exposure_status() == (True, Decimal(0))
    assert service.simulate(p.proposal_id, p.digest) == 'SIMULATED_REJECTED'
    assert len(service.orders()) == 1 and not adapter.submissions


def test_missing_account_metadata_is_not_repaired_or_dispatched(system):
    service, feed, adapter, clock = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    with sqlite3.connect(service.path) as db:
        trusted = db.execute('SELECT * FROM account').fetchone()
        db.execute('DELETE FROM account')
    with pytest.raises(Refused, match='metadata'):
        ApprovalService(service.path, feed, SimulationAdapter(), clock=clock)
    with pytest.raises(Refused, match='metadata'):
        service.simulate(p.proposal_id, p.digest)
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT count(*) FROM account').fetchone()[0] == 0
        assert db.execute('SELECT state FROM proposals').fetchone()[0] == 'APPROVED'
        assert db.execute('SELECT count(*) FROM orders').fetchone()[0] == 0
        # Test-only trusted restoration, not an inferred zero-state repair.
        db.execute('INSERT INTO account VALUES (?,?,?,?)', trusted)
    restarted = ApprovalService(service.path, feed, adapter, clock=clock)
    assert not adapter.submissions
    assert restarted.simulate(p.proposal_id, p.digest) == 'SIMULATED_FILLED'


@pytest.mark.parametrize('column,value', [('known', 2), ('fees', '-1'), ('fees', 'NaN'),
                                        ('fees', '0'), ('fees', 'bad'),
                                        ('last_clock_ns', -1), ('last_clock_ns', 0),
                                        ('last_clock_ns', 'bad')])
def test_inconsistent_account_metadata_refuses_existing_approved_work(system, column, value):
    service, feed, adapter, clock = system
    filled = create(system)
    service.approve(filled.proposal_id, filled.digest)
    assert service.simulate(filled.proposal_id, filled.digest) == 'SIMULATED_FILLED'
    pending = create(system)
    service.approve(pending.proposal_id, pending.digest)
    with sqlite3.connect(service.path) as db:
        db.execute(f'UPDATE account SET {column}=? WHERE id=1', (value,))
        snapshot = db.execute('SELECT * FROM account').fetchone()
    new_adapter = SimulationAdapter()
    with pytest.raises(Refused, match='metadata'):
        ApprovalService(service.path, feed, new_adapter, clock=clock)
    with pytest.raises(Refused, match='metadata'):
        service.simulate(pending.proposal_id, pending.digest)
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT * FROM account').fetchone() == snapshot
        assert db.execute('SELECT state FROM proposals WHERE id=?', (pending.proposal_id,)).fetchone()[0] == 'APPROVED'
    assert not new_adapter.submissions and len(adapter.submissions) == 1


def test_valid_restart_preserves_fee_inventory_and_clock_metadata(system):
    service, feed, _, clock = system
    filled = create(system)
    service.approve(filled.proposal_id, filled.digest)
    service.simulate(filled.proposal_id, filled.digest)
    pending = create(system)
    service.approve(pending.proposal_id, pending.digest)
    with sqlite3.connect(service.path) as db:
        before = db.execute('SELECT * FROM account').fetchone()
        holdings = db.execute('SELECT * FROM positions').fetchall()
    new_adapter = SimulationAdapter()
    ApprovalService(service.path, feed, new_adapter, clock=clock)
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT * FROM account').fetchone() == before
        assert db.execute('SELECT * FROM positions').fetchall() == holdings
    assert Decimal(before[2]) == Decimal('.10') and not new_adapter.submissions


def test_existing_empty_database_is_not_a_demonstrably_new_ledger(system, tmp_path):
    _, feed, adapter, clock = system
    path = tmp_path / 'existing-empty.sqlite'
    path.touch()
    with pytest.raises(Refused, match='metadata'):
        ApprovalService(path, feed, adapter, clock=clock)


def test_unknown_account_remains_unknown_on_restart(system):
    service, feed, _, clock = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    with service.transaction() as db:
        db.execute('UPDATE account SET known=0')
    adapter = SimulationAdapter()
    restarted = ApprovalService(service.path, feed, adapter, clock=clock)
    assert restarted.simulate(p.proposal_id, p.digest) == 'BLOCKED'
    assert restarted.exposure_status()[0] is False
    assert not adapter.submissions
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT known FROM account').fetchone()[0] == 0


def test_restart_refuses_plausible_clock_reset_after_later_activity(system):
    service, feed, adapter, clock = system
    feed.refresh = True
    pending = create(system, lifetime_ns=500_000_000)
    service.approve(pending.proposal_id, pending.digest)
    later = create(system)
    service.approve(later.proposal_id, later.digest)
    old_time = clock.now
    clock.now += 1_000_000_000
    assert service.simulate(later.proposal_id, later.digest) == 'SIMULATED_FILLED'
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT max(utc_ns) FROM audit').fetchone()[0] > pending.expires_ns
        db.execute('UPDATE account SET last_clock_ns=?', (old_time,))
    clock.now = old_time
    fresh_adapter = SimulationAdapter()
    with pytest.raises(Refused, match='metadata'):
        ApprovalService(service.path, feed, fresh_adapter, clock=clock)
    with pytest.raises(Refused, match='metadata'):
        service.simulate(pending.proposal_id, pending.digest)
    assert not fresh_adapter.submissions and len(adapter.submissions) == 1
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT last_clock_ns FROM account').fetchone()[0] == old_time
        assert db.execute('SELECT state FROM proposals WHERE id=?', (pending.proposal_id,)).fetchone()[0] == 'APPROVED'
        assert db.execute('SELECT count(*) FROM orders WHERE proposal_id=?', (pending.proposal_id,)).fetchone()[0] == 0


def test_verified_clock_history_survives_advancing_clock_restart(tmp_path):
    clock = AdvancingClock()
    feed, adapter = FixtureFeed(clock=clock), SimulationAdapter()
    service = ApprovalService(tmp_path / 'history.sqlite', feed, adapter, clock=clock)
    p = proposal_fixture(feed, 'SIM-KXSENATE-PA', clock=clock)
    service.create(p)
    service.approve(p.proposal_id, p.digest)
    assert service.simulate(p.proposal_id, p.digest) == 'SIMULATED_FILLED'
    with sqlite3.connect(service.path) as db:
        before = db.execute('SELECT * FROM account').fetchone()
        history = db.execute('SELECT clock_ns FROM clock_history ORDER BY ordinal').fetchall()
        audit = db.execute('SELECT utc_ns FROM audit ORDER BY ordinal').fetchall()
    assert history[0] == (0,)
    assert history == sorted(history) and history[-1][0] == before[3]
    assert all(timestamp in history for timestamp in audit)
    restarted = ApprovalService(service.path, feed, SimulationAdapter(), clock=clock)
    with sqlite3.connect(service.path) as db:
        assert db.execute('SELECT * FROM account').fetchone() == before
        assert db.execute('SELECT clock_ns FROM clock_history ORDER BY ordinal').fetchall() == history
    assert not restarted.adapter.submissions


def test_clock_witness_and_activity_roll_back_together(system):
    service, _, _, clock = system
    p = create(system)
    with sqlite3.connect(service.path) as db:
        before = (db.execute('SELECT * FROM account').fetchall(),
                  db.execute('SELECT * FROM clock_history').fetchall(),
                  db.execute('SELECT * FROM audit').fetchall())
    clock.now += 1_000_000_000
    with pytest.raises(sqlite3.IntegrityError):
        with service.transaction() as db:
            service._now(db)
            service._audit(db, p.proposal_id, 'SYNTHETIC', 'rolled back')
            db.execute("INSERT INTO account VALUES (1,1,'0',0)")  # Force rollback.
    with sqlite3.connect(service.path) as db:
        assert (db.execute('SELECT * FROM account').fetchall(),
                db.execute('SELECT * FROM clock_history').fetchall(),
                db.execute('SELECT * FROM audit').fetchall()) == before
    clock.now -= 1_000_000_000
    service.listing()  # A rolled-back clock sample must not poison healthy state.


def test_clock_regression_during_audit_rolls_back_transition(system):
    service, _, adapter, clock = system
    p = create(system)
    clock.now -= 1
    with pytest.raises(Refused, match='Clock regression'):
        service.reject(p.proposal_id, p.digest)  # This path samples via _audit only.
    clock.now += 1
    assert service.listing()[0]['state'] == 'PROPOSED'
    assert not adapter.submissions


def test_concurrent_activity_preserves_clock_witness(tmp_path):
    class LockedClock(Clock):
        lock = threading.Lock()
        def __call__(self):
            with self.lock:
                self.now += 100
                return self.now
    clock = LockedClock()
    feed, adapter = FixtureFeed(clock=clock), SimulationAdapter()
    service = ApprovalService(tmp_path / 'concurrent-clock.sqlite', feed, adapter, clock=clock)
    proposals = [proposal_fixture(feed, 'SIM-KXSENATE-PA', clock=clock) for _ in range(8)]
    for p in proposals:
        service.create(p)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert set(pool.map(lambda p: service.approve(p.proposal_id, p.digest), proposals)) == {'APPROVED'}
        list(pool.map(lambda _: service.simulate(proposals[0].proposal_id, proposals[0].digest), range(16)))
    assert len(adapter.submissions) == len(service.orders()) == 1
    with sqlite3.connect(service.path) as db:
        history = [r[0] for r in db.execute('SELECT clock_ns FROM clock_history ORDER BY ordinal')]
        assert history == sorted(history)
        assert db.execute('SELECT last_clock_ns FROM account').fetchone()[0] == history[-1]
    ApprovalService(service.path, feed, SimulationAdapter(), clock=clock)


@pytest.mark.parametrize('damage', ['missing_table', 'empty_history', 'ahead_account'])
def test_missing_or_contradictory_clock_witness_is_never_backfilled(system, damage):
    service, feed, adapter, clock = system
    p = create(system)
    service.approve(p.proposal_id, p.digest)
    with sqlite3.connect(service.path) as db:
        if damage == 'missing_table':
            db.execute('DROP TABLE clock_history')
        elif damage == 'empty_history':
            db.execute('DROP TRIGGER immutable_clock_history_delete')
            db.execute('DELETE FROM clock_history')
        else:
            db.execute('UPDATE account SET last_clock_ns=last_clock_ns+1')
    with pytest.raises(Refused, match='metadata'):
        ApprovalService(service.path, feed, SimulationAdapter(), clock=clock)
    with pytest.raises(Refused, match='metadata'):
        service.simulate(p.proposal_id, p.digest)
    assert not adapter.submissions


def test_clock_witness_is_append_only_and_monotonic(system):
    service, _, _, clock = system
    p = create(system)
    with service.transaction() as db:
        for statement in ('DELETE FROM clock_history',
                          'UPDATE clock_history SET clock_ns=0',
                          'INSERT INTO clock_history(clock_ns) VALUES (0)'):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
    assert service.listing()[0]['state'] == 'PROPOSED'
