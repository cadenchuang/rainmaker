"""Simulation-only approval ledger. This module has no Kalshi/network/auth imports."""
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4, uuid5, NAMESPACE_URL


class Refused(ValueError):
    """Safe, locally generated refusal; no remote exception payloads."""


def exact(value):
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise Refused('Use an exact decimal value')
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise Refused('Invalid decimal') from None
    if not result.is_finite() or result < 0 or result > 1000000 or result.as_tuple().exponent < -6:
        raise Refused('Invalid decimal range or precision')
    return result


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def fingerprint(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    market_ticker: str
    office: str
    contract_id: str
    rules_hash: str
    outcome: str
    side: str
    limit_price: Decimal
    quantity: int
    max_principal: Decimal
    max_fee: Decimal
    created_ns: int
    expires_ns: int
    forecast_ref: str | None = None
    source_data_ns: int | None = None
    risk_status: str = 'PASS'
    data_quality: str = 'VALID'
    simulation_scenario: str = 'fill'
    rules_text: str = ''

    def __post_init__(self):
        for name in ('limit_price', 'max_principal', 'max_fee'):
            object.__setattr__(self, name, exact(getattr(self, name)))
        if self.office not in ('SENATE', 'GOVERNOR') or self.outcome not in ('YES', 'NO'):
            raise Refused('Only U.S. Senate/governor binary outcomes are supported')
        if self.side not in ('BUY', 'SELL'):
            raise Refused('Unsupported order side')
        if type(self.quantity) is not int or not 0 < self.quantity <= 1000000:
            raise Refused('Quantity must be positive whole contracts')
        if not 0 < self.limit_price < 1:
            raise Refused('Limit must be between zero and one dollar')
        if self.limit_price * self.quantity > self.max_principal:
            raise Refused('Principal budget is insufficient')
        if type(self.created_ns) is not int or type(self.expires_ns) is not int or self.expires_ns <= self.created_ns:
            raise Refused('Invalid proposal lifetime')
        if not isinstance(self.rules_text, str) or not self.rules_text or fingerprint(self.rules_text) != self.rules_hash:
            raise Refused('Proposal must retain the exact contract rules')
        if self.simulation_scenario not in ('fill', 'ack', 'reject', 'timeout', 'ambiguous'):
            raise Refused('Unsupported simulation scenario')
        if not all(isinstance(v, str) and 0 < len(v) <= 200 for v in
                   (self.proposal_id, self.market_ticker, self.contract_id, self.rules_hash)):
            raise Refused('Invalid proposal identity')

    def body(self):
        return canonical({k: str(v) if isinstance(v, Decimal) else v for k, v in self.__dict__.items()})

    @property
    def digest(self):
        return fingerprint(self.body())

    @classmethod
    def from_body(cls, body):
        return cls(**json.loads(body))


@dataclass(frozen=True)
class RiskLimits:
    max_quantity: int = 20
    max_principal: Decimal = Decimal('20')
    max_fee: Decimal = Decimal('2')
    max_exposure: Decimal = Decimal('100')
    max_age_ns: int = 10_000_000_000


TRANSITIONS = {
    'PROPOSED': {'APPROVED', 'REJECTED', 'EXPIRED'},
    'APPROVED': {'REVALIDATING', 'EXPIRED'},
    'REVALIDATING': {'SIMULATED_SUBMISSION', 'BLOCKED', 'EXPIRED', 'RECONCILING'},
    'SIMULATED_SUBMISSION': {'SIMULATED_ACKNOWLEDGED', 'SIMULATED_FILLED', 'SIMULATED_REJECTED', 'RECONCILING', 'BLOCKED'},
    'SIMULATED_ACKNOWLEDGED': {'SIMULATED_FILLED', 'SIMULATED_REJECTED', 'RECONCILING'},
    'RECONCILING': {'SIMULATED_ACKNOWLEDGED', 'SIMULATED_FILLED', 'SIMULATED_REJECTED'},
}


class ApprovalService:
    """One SQLite claim/reservation per proposal; adapter calls occur after commit.

    No approved work is automatically dispatched at startup. Interrupted attempts
    retain reservations and become reconciliation cases, never new submissions.
    """
    def __init__(self, path, feed, adapter, *, clock=time.time_ns, limits=RiskLimits()):
        from simulation import FixtureFeed, SimulationAdapter
        if type(feed) is not FixtureFeed or type(adapter) is not SimulationAdapter:
            raise Refused('Phase A accepts only fixture data and the simulation adapter')
        path = Path(path)
        if path.suffix not in ('.sqlite', '.sqlite3', '.db') or path.is_symlink():
            raise Refused('Use a dedicated, nonsymlink simulation SQLite file')
        self.path, self.feed, self.adapter = str(path), feed, adapter
        self.clock, self.limits = clock, limits
        try:
            with path.open('xb'):
                pass
            new_file = True
        except FileExistsError:
            new_file = False
        with self.transaction(verify_account=False) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            required = {'proposals', 'orders', 'fills', 'positions', 'account', 'audit'}
            if tables and not required.issubset(tables):
                raise Refused('Unknown ledger schema; use a dedicated simulation database')
            if not tables and not new_file:
                raise Refused('Existing ledger metadata is missing; trusted repair required')
            if tables and 'clock_history' not in tables:
                raise Refused('Ledger clock-history metadata is missing; trusted migration required')
            if tables:
                self._account(db)
            schema = '''
                CREATE TABLE IF NOT EXISTS proposals (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL,
                    state TEXT NOT NULL, approval_digest TEXT, approved_ns INTEGER,
                    reason TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS orders (
                    client_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE REFERENCES proposals(id),
                    state TEXT NOT NULL, reservation TEXT NOT NULL, quantity INTEGER NOT NULL,
                    mode TEXT NOT NULL CHECK(mode='SIMULATION'));
                CREATE TABLE IF NOT EXISTS fills (
                    client_id TEXT PRIMARY KEY REFERENCES orders(client_id), price TEXT NOT NULL,
                    quantity INTEGER NOT NULL, fee TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode='SIMULATION'));
                CREATE TABLE IF NOT EXISTS positions (
                    ticker TEXT NOT NULL, outcome TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity>=0),
                    PRIMARY KEY(ticker,outcome));
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK(id=1), known INTEGER NOT NULL, fees TEXT NOT NULL,
                    last_clock_ns INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS clock_history (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
                    clock_ns INTEGER NOT NULL CHECK(typeof(clock_ns)='integer' AND clock_ns>=0));
                CREATE TRIGGER IF NOT EXISTS monotonic_clock_history BEFORE INSERT ON clock_history
                    WHEN NEW.clock_ns < (SELECT clock_ns FROM clock_history ORDER BY ordinal DESC LIMIT 1)
                    BEGIN SELECT RAISE(ABORT,'Clock history must be monotonic'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_clock_history_update BEFORE UPDATE ON clock_history
                    BEGIN SELECT RAISE(ABORT,'Clock history is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_clock_history_delete BEFORE DELETE ON clock_history
                    BEGIN SELECT RAISE(ABORT,'Clock history is append-only'); END;
                CREATE TABLE IF NOT EXISTS audit (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, utc_ns INTEGER NOT NULL,
                    proposal_id TEXT, kind TEXT NOT NULL, detail TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode='SIMULATION'));
                CREATE TRIGGER IF NOT EXISTS immutable_proposal BEFORE UPDATE OF body,digest ON proposals
                    BEGIN SELECT RAISE(ABORT,'Proposal terms are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_approval BEFORE UPDATE OF approval_digest ON proposals
                    WHEN OLD.approval_digest IS NOT NULL AND NEW.approval_digest IS NOT OLD.approval_digest
                    BEGIN SELECT RAISE(ABORT,'Approval binding is immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_audit_update BEFORE UPDATE ON audit
                    BEGIN SELECT RAISE(ABORT,'Audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_audit_delete BEFORE DELETE ON audit
                    BEGIN SELECT RAISE(ABORT,'Audit is append-only'); END;
            '''
            # executescript implicitly commits; keep schema and initial state atomic.
            statement = ''
            for line in schema.splitlines():
                statement += line + '\n'
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ''
            if not tables:
                db.execute("INSERT INTO account VALUES (1,1,'0',0)")
                db.execute("INSERT INTO clock_history(clock_ns) VALUES (0)")
            self._account(db)
        # Restart handling is explicit and conservative, never dispatching orders.
        with self.transaction() as db:
            for row in db.execute("SELECT * FROM proposals WHERE state IN ('REVALIDATING','SIMULATED_SUBMISSION')").fetchall():
                order = db.execute('SELECT * FROM orders WHERE proposal_id=?', (row['id'],)).fetchone()
                if order:
                    db.execute("UPDATE orders SET state='UNKNOWN' WHERE client_id=?", (order['client_id'],))
                    self._transition(db, row, 'RECONCILING', 'Interrupted submission; explicit reconciliation required')
                else:
                    self._transition(db, row, 'BLOCKED', 'Interrupted revalidation; create a new proposal')

    @contextmanager
    def transaction(self, *, verify_account=True):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            if verify_account:
                self._account(db)
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _account(db):
        """Validate trusted metadata without inferring or repairing exposure."""
        rows = db.execute('SELECT * FROM account').fetchall()
        try:
            if len(rows) != 1:
                raise ValueError
            row = rows[0]
            if (row['id'] != 1 or type(row['known']) is not int or row['known'] not in (0, 1)
                    or type(row['fees']) is not str or type(row['last_clock_ns']) is not int
                    or row['last_clock_ns'] < 0):
                raise ValueError
            # Compare independently persisted verified samples, not arbitrary audit
            # timestamps. Never reconstruct this witness from a possibly reset row.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clock_history'").fetchone():
                raise ValueError
            witness = db.execute('SELECT clock_ns FROM clock_history ORDER BY ordinal DESC LIMIT 1').fetchone()
            if (witness is None or type(witness[0]) is not int or
                    witness[0] != row['last_clock_ns'] or
                    db.execute('SELECT max(clock_ns) FROM clock_history').fetchone()[0] != witness[0]):
                raise ValueError
            fees = exact(row['fees'])
            recorded_fees = sum((exact(r[0]) for r in db.execute('SELECT fee FROM fills')), Decimal(0))
            if fees != recorded_fees:
                raise ValueError
            for proposal in db.execute('SELECT body,approved_ns FROM proposals'):
                created = TradeProposal.from_body(proposal['body']).created_ns
                approved = proposal['approved_ns']
                if (created > row['last_clock_ns'] or
                        (approved is not None and (type(approved) is not int or
                         not created <= approved <= row['last_clock_ns']))):
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            raise Refused('Ledger account metadata is inconsistent; trusted repair required') from None
        return row

    def _now(self, db):
        return self._remember_clock(db, self.clock())

    @staticmethod
    def _remember_clock(db, now):
        """Account watermark and witness commit or roll back with the activity."""
        previous = db.execute('SELECT last_clock_ns FROM account WHERE id=1').fetchone()[0]
        if type(now) is not int or now < previous:
            raise Refused('Clock regression; execution is blocked')
        db.execute('UPDATE account SET last_clock_ns=? WHERE id=1', (now,))
        db.execute('INSERT INTO clock_history(clock_ns) VALUES (?)', (now,))
        return now

    def _audit(self, db, pid, kind, detail):
        db.execute('INSERT INTO audit(utc_ns,proposal_id,kind,detail,mode) VALUES (?,?,?,?,?)',
                   (self._now(db), pid, kind, detail, 'SIMULATION'))

    def _transition(self, db, row, target, reason=''):
        if target not in TRANSITIONS.get(row['state'], set()):
            raise Refused('Illegal proposal transition')
        db.execute('UPDATE proposals SET state=?,reason=? WHERE id=?', (target, reason, row['id']))
        self._audit(db, row['id'], target, reason)

    def _load(self, db, pid):
        row = db.execute('SELECT * FROM proposals WHERE id=?', (pid,)).fetchone()
        if row is None:
            raise Refused('Unknown proposal')
        p = TradeProposal.from_body(row['body'])
        if p.proposal_id != pid or not hmac.compare_digest(p.digest, row['digest']):
            raise Refused('Proposal integrity failure')
        return row, p

    def create(self, proposal):
        with self.transaction() as db:
            now = self._now(db)
            market = self.feed.get(proposal.market_ticker)
            self._identity(proposal, market)
            if proposal.created_ns > now or proposal.expires_ns <= now:
                raise Refused('Proposal is not current')
            db.execute('INSERT INTO proposals(id,body,digest,state) VALUES (?,?,?,?)',
                       (proposal.proposal_id, proposal.body(), proposal.digest, 'PROPOSED'))
            self._audit(db, proposal.proposal_id, 'PROPOSED', proposal.digest)
        return proposal.proposal_id

    def approve(self, pid, digest):
        with self.transaction() as db:
            row, p = self._load(db, pid)
            now = self._now(db)
            if not hmac.compare_digest(digest, p.digest):
                raise Refused('Displayed terms changed; approval refused')
            if row['state'] != 'PROPOSED':
                return row['state']
            if now >= p.expires_ns:
                self._transition(db, row, 'EXPIRED', 'Proposal expired before approval')
                return 'EXPIRED'
            db.execute('UPDATE proposals SET approval_digest=?,approved_ns=? WHERE id=?', (p.digest, now, pid))
            self._transition(db, row, 'APPROVED', 'Explicit local human approval of ' + p.digest)
        return 'APPROVED'

    def reject(self, pid, digest):
        with self.transaction() as db:
            row, p = self._load(db, pid)
            if not hmac.compare_digest(digest, p.digest):
                raise Refused('Displayed terms do not match')
            if row['state'] == 'PROPOSED':
                self._transition(db, row, 'REJECTED', 'Rejected by local human')
                return 'REJECTED'
            return row['state']

    @staticmethod
    def _identity(p, market):
        if (market is None or market.ticker != p.market_ticker or market.country != 'US' or market.office != p.office or
                market.contract_id != p.contract_id or market.rules_hash != p.rules_hash or
                not market.identity_verified or not market.rules_verified):
            raise Refused('Market identity or settlement rules are unverified or changed')

    def _temporal(self, p, market, now, minimum_ns):
        if type(now) is not int or now < minimum_ns:
            raise Refused('Clock regression; execution is blocked')
        if now >= p.expires_ns:
            raise Refused('Approval expired before adapter call')
        for timestamp in (market.received_ns, market.source_ns):
            if type(timestamp) is not int or not 0 <= now - timestamp <= self.limits.max_age_ns:
                raise Refused('Market data is missing, stale or future-dated')

    def _market_gate(self, p, now):
        # Capture the immutable observation BEFORE its validation clock sample.
        market = self.feed.get(p.market_ticker)
        self._identity(p, market)
        checked_ns = self.clock()
        self._temporal(p, market, checked_ns, now)
        if p.risk_status != 'PASS' or p.data_quality != 'VALID':
            raise Refused('Proposal risk or data-quality status blocks submission')
        if not market.synchronized or market.recording_status != 'COMPLETE' or market.reconciliation_status != 'CLEAR':
            raise Refused('Market synchronization, recording or reconciliation is ambiguous')
        tick, unit_fee = exact(market.tick), exact(market.fee_per_contract)
        if tick <= 0:
            raise Refused('Invalid market tick')
        for levels in (market.yes_bids, market.no_bids):
            seen = set()
            for price, size in levels:
                if price in seen or not 0 < exact(price) < 1 or exact(size) <= 0:
                    raise Refused('Invalid order-book level')
                seen.add(price)
        if market.yes_bids and market.no_bids and max(p for p, _ in market.yes_bids) + max(p for p, _ in market.no_bids) > 1:
            raise Refused('Crossed order book')
        if p.limit_price % tick != 0:
            raise Refused('Limit violates the market tick size')
        if p.quantity > self.limits.max_quantity or p.max_principal > self.limits.max_principal or p.max_fee > self.limits.max_fee:
            raise Refused('Proposal exceeds quantity or budget policy')
        if unit_fee * p.quantity > p.max_fee:
            raise Refused('Fee budget is insufficient')
        principal = p.limit_price * p.quantity
        if principal > p.max_principal:
            raise Refused('Principal budget is insufficient')
        levels = market.levels(p.outcome, p.side)
        usable = sum(q for price, q in levels if (price <= p.limit_price if p.side == 'BUY' else price >= p.limit_price))
        if usable < p.quantity:
            raise Refused('Insufficient usable liquidity at the approved limit')
        return market, checked_ns

    def _gate(self, db, p, now, exclude_client=''):
        market, checked_ns = self._market_gate(p, now)
        self._remember_clock(db, checked_ns)
        account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
        if account['known'] != 1 or db.execute("SELECT 1 FROM orders WHERE state IN ('UNKNOWN','SUBMITTING') AND client_id<>?", (exclude_client,)).fetchone():
            raise Refused('Unknown positions or unresolved exposure blocks submission')
        holdings = db.execute('SELECT COALESCE(sum(quantity),0) FROM positions').fetchone()[0]
        active = db.execute("SELECT reservation FROM orders WHERE state='ACKNOWLEDGED'").fetchall()
        # Conservative full-dollar risk per contract; never net uncertain proceeds.
        reservation = Decimal(p.quantity) + p.max_fee
        exposure = Decimal(holdings) + exact(account['fees']) + sum((exact(o[0]) for o in active), Decimal(0))
        if exposure + reservation > self.limits.max_exposure:
            raise Refused('Maximum conservative total exposure exceeded')
        if p.side == 'SELL':
            position = db.execute('SELECT quantity FROM positions WHERE ticker=? AND outcome=?', (p.market_ticker, p.outcome)).fetchone()
            reserved = db.execute("SELECT p.body FROM orders o JOIN proposals p ON p.id=o.proposal_id WHERE o.state='ACKNOWLEDGED'").fetchall()
            held = sum(TradeProposal.from_body(r[0]).quantity for r in reserved
                       if (TradeProposal.from_body(r[0]).market_ticker, TradeProposal.from_body(r[0]).outcome,
                           TradeProposal.from_body(r[0]).side) == (p.market_ticker, p.outcome, 'SELL'))
            if position is None or position[0] - held < p.quantity:
                raise Refused('SELL requires known unreserved simulated inventory')
        return reservation, market, checked_ns

    def simulate(self, pid, digest):
        with self.transaction() as db:
            row, p = self._load(db, pid)
            if not hmac.compare_digest(digest, p.digest):
                raise Refused('Displayed terms changed')
            if row['state'] != 'APPROVED':
                return row['state']  # Duplicate clicks never create another attempt.
            now = self._now(db)
            if now >= p.expires_ns:
                self._transition(db, row, 'EXPIRED', 'Approval expired before submission')
                return 'EXPIRED'
            self._transition(db, row, 'REVALIDATING')
        with self.transaction() as db:
            row, p = self._load(db, pid)
            if row['state'] != 'REVALIDATING':
                return row['state']
            now = self._now(db)
            try:
                if (row['approval_digest'] is None or not hmac.compare_digest(row['approval_digest'], p.digest)
                        or type(row['approved_ns']) is not int or not p.created_ns <= row['approved_ns'] <= now):
                    raise Refused('No current approval of these exact terms')
                if now >= p.expires_ns:
                    self._transition(db, row, 'EXPIRED', 'Approval expired during revalidation')
                    return 'EXPIRED'
                reservation, _, _ = self._gate(db, p, now)
            except Refused as exc:
                self._transition(db, row, 'BLOCKED', str(exc))
                return 'BLOCKED'
            client_id = str(uuid5(NAMESPACE_URL, 'rainmaker-simulation:' + p.proposal_id + ':' + p.digest))
            db.execute('INSERT INTO orders VALUES (?,?,?,?,?,?)',
                       (client_id, pid, 'SUBMITTING', str(reservation), p.quantity, 'SIMULATION'))
            self._transition(db, row, 'SIMULATED_SUBMISSION', client_id)
        # An attempt is durable BEFORE crossing the fake-adapter boundary.
        # Recheck immediately after the commit, which may itself have been slow.
        refusal = None
        with self.transaction() as db:
            current = db.execute('SELECT state FROM orders WHERE client_id=?', (client_id,)).fetchone()[0]
            if current != 'SUBMITTING':
                return self._load(db, pid)[0]['state']
            try:
                _, market, checked_ns = self._gate(db, p, self._now(db), exclude_client=client_id)
            except Refused as exc:
                refusal = str(exc)
        # No disk I/O or refreshed fixture between this authoritative time check
        # and dispatch. A definite local refusal releases the durable reservation
        # through _outcome; a crash before that remains reconciliation-only.
        if refusal is None:
            try:
                self._temporal(p, market, self.clock(), checked_ns)
            except Refused as exc:
                refusal = str(exc)
        try:
            result = {'state': 'REJECTED'} if refusal else self.adapter.submit(client_id, p)
        except Exception:
            result = {'state': 'UNKNOWN'}
        return self._outcome(pid, client_id, result, refusal_reason=refusal)

    def _outcome(self, pid, client_id, result, *, refusal_reason=None):
        with self.transaction() as db:
            row, p = self._load(db, pid)
            order = db.execute('SELECT * FROM orders WHERE client_id=? AND proposal_id=?', (client_id, pid)).fetchone()
            if order is None:
                raise Refused('Unknown submission identity')
            if order['state'] in ('FILLED', 'REJECTED'):
                return row['state']
            if not isinstance(result, dict):
                result = {'state': 'UNKNOWN'}
            state = result.get('state')
            if state not in ('ACKNOWLEDGED', 'FILLED', 'REJECTED', 'UNKNOWN'):
                state = 'UNKNOWN'
            if state == 'FILLED':
                try:
                    price, fee = exact(result.get('price')), exact(result.get('fee'))
                    quantity = result.get('quantity')
                    if not 0 < price < 1 or quantity != p.quantity or type(quantity) is not int or fee > p.max_fee or price * quantity > p.max_principal:
                        raise Refused('Simulated fill violates approved budgets')
                    if (p.side == 'BUY' and price > p.limit_price) or (p.side == 'SELL' and price < p.limit_price):
                        raise Refused('Simulated fill violates approved limit')
                    position = db.execute('SELECT quantity FROM positions WHERE ticker=? AND outcome=?', (p.market_ticker, p.outcome)).fetchone()
                    current = position[0] if position else 0
                    updated = current + (quantity if p.side == 'BUY' else -quantity)
                    if updated < 0:
                        raise Refused('Unknown sell inventory')
                except (Refused, TypeError):
                    state = 'UNKNOWN'
                else:
                    db.execute('INSERT INTO fills VALUES (?,?,?,?,?)', (client_id, str(price), quantity, str(fee), 'SIMULATION'))
                    db.execute('INSERT INTO positions VALUES (?,?,?) ON CONFLICT(ticker,outcome) DO UPDATE SET quantity=excluded.quantity',
                               (p.market_ticker, p.outcome, updated))
                    fees = exact(db.execute('SELECT fees FROM account WHERE id=1').fetchone()[0]) + fee
                    db.execute('UPDATE account SET fees=? WHERE id=1', (str(fees),))
            target = {'ACKNOWLEDGED': 'SIMULATED_ACKNOWLEDGED', 'FILLED': 'SIMULATED_FILLED',
                      'REJECTED': 'SIMULATED_REJECTED', 'UNKNOWN': 'RECONCILING'}[state]
            db.execute('UPDATE orders SET state=? WHERE client_id=?', (state, client_id))
            reason = {'ACKNOWLEDGED': 'SIMULATION: order acknowledged; reservation retained',
                      'FILLED': 'SIMULATION: complete mock fill recorded',
                      'REJECTED': 'SIMULATION: order rejected',
                      'UNKNOWN': 'SIMULATION: uncertain result; explicit reconciliation required'}[state]
            if refusal_reason is not None:
                reason = 'SIMULATION: pre-submission gate refused: ' + refusal_reason
            if target != row['state']:
                self._transition(db, row, target, reason)
            self._audit(db, pid, 'SIMULATED_ORDER_' + state, client_id)
            return target

    def reconcile(self, pid, digest):
        with self.transaction() as db:
            row, p = self._load(db, pid)
            if not hmac.compare_digest(digest, p.digest):
                raise Refused('Displayed terms changed')
            if row['state'] not in ('RECONCILING', 'SIMULATED_ACKNOWLEDGED'):
                return row['state']
            order = db.execute('SELECT * FROM orders WHERE proposal_id=?', (pid,)).fetchone()
            if order is None:
                raise Refused('Missing order identity; reconciliation blocked')
            client_id = order['client_id']
        try:
            result = self.adapter.lookup(client_id)
        except Exception:
            result = {'state': 'UNKNOWN'}
        return self._outcome(pid, client_id, result)

    def listing(self):
        with self.transaction() as db:
            now = self._now(db)
            for row in db.execute("SELECT * FROM proposals WHERE state IN ('PROPOSED','APPROVED')").fetchall():
                if TradeProposal.from_body(row['body']).expires_ns <= now:
                    self._transition(db, row, 'EXPIRED', 'Proposal expired')
            return [dict(row) for row in db.execute('SELECT * FROM proposals ORDER BY rowid DESC')]

    def orders(self):
        with self.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM orders')]

    def exposure_status(self):
        with self.transaction() as db:
            account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
            holdings = db.execute('SELECT COALESCE(sum(quantity),0) FROM positions').fetchone()[0]
            active = db.execute("SELECT state,reservation FROM orders WHERE state NOT IN ('FILLED','REJECTED')").fetchall()
            known = account['known'] == 1 and not any(o['state'] in ('UNKNOWN', 'SUBMITTING') for o in active)
            bound = Decimal(holdings) + exact(account['fees']) + sum((exact(o['reservation']) for o in active), Decimal(0))
            return known, bound
