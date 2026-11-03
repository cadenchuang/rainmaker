"""Deterministic synthetic U.S. election markets and fake execution only.

No Kalshi clients, credentials, HTTP requests, or production execution interface.
"""
from dataclasses import dataclass, replace
from decimal import Decimal
import threading
import time

from approval import TradeProposal, Refused, fingerprint, exact
from uuid import uuid4


@dataclass(frozen=True)
class FixtureMarket:
    ticker: str
    office: str
    contract_id: str
    rules: str
    country: str = 'US'
    identity_verified: bool = True
    rules_verified: bool = True
    synchronized: bool = True
    recording_status: str = 'COMPLETE'
    reconciliation_status: str = 'CLEAR'
    received_ns: int | None = None
    source_ns: int | None = None
    tick: Decimal = Decimal('.01')
    fee_per_contract: Decimal = Decimal('.02')
    yes_bids: tuple = ((Decimal('.49'), 100),)
    no_bids: tuple = ((Decimal('.49'), 100),)

    @property
    def rules_hash(self):
        return fingerprint(self.rules)

    def levels(self, outcome, side):
        bids = self.yes_bids if outcome == 'YES' else self.no_bids
        if side == 'SELL':
            return bids
        opposite = self.no_bids if outcome == 'YES' else self.yes_bids
        return tuple((Decimal(1) - price, size) for price, size in opposite)


class FixtureFeed:
    """Each read generates a new SYNTHETIC quote, not a heartbeat from Kalshi.

    Tests disable refresh to exercise stale, missing, or inconsistent data.
    """
    def __init__(self, *, clock=time.time_ns, refresh=True):
        self.clock, self.refresh = clock, refresh
        self.markets = {
            'SIM-KXSENATE-PA': FixtureMarket('SIM-KXSENATE-PA', 'SENATE', 'fixture-us-senate-pa',
                'SIMULATION RULES: YES if the fixture candidate wins the U.S. Senate race in Pennsylvania.'),
            'SIM-KXGOVERNOR-MI': FixtureMarket('SIM-KXGOVERNOR-MI', 'GOVERNOR', 'fixture-us-governor-mi',
                'SIMULATION RULES: YES if the fixture candidate wins the Michigan governor race.'),
        }
        now = self.clock()
        self.markets = {key: replace(value, received_ns=now, source_ns=now) for key, value in self.markets.items()}

    def get(self, ticker):
        value = self.markets.get(ticker)
        if value is not None and self.refresh:
            now = self.clock()
            return replace(value, received_ns=now, source_ns=now)
        return value


def proposal_fixture(feed, ticker, *, clock=time.time_ns, quantity=5, price='0.55',
                     max_fee='0.20', scenario='fill', outcome='YES', side='BUY', lifetime_ns=120_000_000_000):
    market = feed.get(ticker)
    if market is None:
        raise Refused('Unknown fixture market')
    now = clock()
    price = exact(price)
    return TradeProposal(str(uuid4()), ticker, market.office, market.contract_id, market.rules_hash,
                         outcome, side, price, quantity, price * quantity, max_fee,
                         now, now + lifetime_ns, 'synthetic-fixture-v1', market.source_ns,
                         simulation_scenario=scenario, rules_text=market.rules)


class SimulationAdapter:
    """One idempotent fake attempt. Lookup never creates or resubmits an order."""
    def __init__(self):
        self._lock = threading.Lock()
        self._orders = {}
        self.submissions = []

    def submit(self, client_id, proposal):
        with self._lock:
            if client_id in self._orders:
                return dict(self._orders[client_id])
            self.submissions.append((client_id, proposal))
            fill = {'state': 'FILLED', 'price': str(proposal.limit_price),
                    'quantity': proposal.quantity, 'fee': str(Decimal('.02') * proposal.quantity),
                    'mode': 'SIMULATION'}
            scenario = proposal.simulation_scenario
            result = {'fill': fill, 'ack': {'state': 'ACKNOWLEDGED'},
                      'reject': {'state': 'REJECTED'}, 'timeout': {'state': 'UNKNOWN'},
                      'ambiguous': fill}[scenario]
            self._orders[client_id] = dict(result)
            if scenario == 'timeout':
                raise TimeoutError('SIMULATION: no acknowledgment received')
            if scenario == 'ambiguous':
                return {'state': 'UNKNOWN', 'mode': 'SIMULATION'}
            return dict(result)

    def lookup(self, client_id):
        with self._lock:
            return dict(self._orders.get(client_id, {'state': 'UNKNOWN', 'mode': 'SIMULATION'}))
