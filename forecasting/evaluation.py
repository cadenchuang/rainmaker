"""Independent artifact scoring. No estimator, ingestion, or trading policy.

Cutoffs are midnight in the election's named civil timezone minus calendar days.
Callbacks receive only eligible training cases and the target definition/cutoff,
never the target outcome. Authenticity still requires external source verification.
"""
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo

from forecasting.contracts import (
    RaceDefinition, EvidenceEnvelope, ForecastRecord, CountingObservation,
    dependency_evidence, validate_dependency_graph, materialize_counts, content_id,
)


@dataclass(frozen=True)
class Outcome:
    race: RaceDefinition
    cycle: int
    candidate_ids: tuple[str, ...]
    votes: tuple[int | None, ...]
    evidence: EvidenceEnvelope

    def __post_init__(self):
        if self.cycle != self.race.election_date.year:
            raise ValueError('Cycle must be election year')
        if self.candidate_ids != tuple(c.candidate_id for c in self.race.candidates):
            raise ValueError('Wrong outcome candidate mapping')
        if len(self.votes) != len(self.candidate_ids) or any(
                v is not None and (type(v) is not int or v < 0) for v in self.votes):
            raise ValueError('Invalid outcome votes')
        if self.evidence.source_kind != 'OFFICIAL_RESULTS':
            raise ValueError('Outcome requires official-result source kind')

    @property
    def origin(self):
        return 'SYNTHETIC' if any(e.origin == 'SYNTHETIC' for e in
            (self.evidence, self.race.rules_evidence, self.race.definition_evidence)) else 'REAL'


def cutoff_for(race, days):
    if days not in (30, 7, 1):
        raise ValueError('Unsupported horizon')
    return (datetime.combine(race.election_date - timedelta(days=days), time(),
                             ZoneInfo(race.election_timezone))).astimezone(timezone.utc)


def chronological_training(outcomes, target, cutoff):
    """Forward cycle holdout; excludes ALL target/future cycle labels."""
    return tuple(o for o in outcomes if o.cycle < target.cycle
                 and o.race.election_date < target.race.election_date
                 and o.evidence.received_at <= cutoff and scorable(o))


def scorable(o):
    return (all(v is not None for v in o.votes) and sum(o.votes) > 0
            and o.evidence.quality in ('VERIFIED_OFFICIAL', 'SYNTHETIC'))


def audit_forecast(record, target, cutoff, training):
    validate_dependency_graph(record)
    if record.prior.race.ref != target.race.ref or record.evidence_cutoff != cutoff:
        raise ValueError('Wrong race mapping or as-of cutoff')
    if record.counting_state is not None:
        raise ValueError('Pre-election scoring cannot consume election-night counts')
    allowed = {o.race.race_id for o in training}
    # The adapter must expose label membership, not just an opaque model hash.
    for o in training:
        if o.cycle >= target.cycle or o.evidence.received_at > cutoff:
            raise ValueError('Training label leakage')
    for model in record.models:
        if model.training_manifest.training_cutoff > cutoff:
            raise ValueError('Training cutoff leakage')
    if any(e.received_at > cutoff for e in dependency_evidence(record)):
        raise ValueError('Future evidence')
    if target.race.race_id in allowed:
        raise ValueError('Target in training')


def quantile(values, q):
    values = sorted(values)
    x = (len(values) - 1) * q
    lo = math.floor(x)
    return values[lo] + (values[math.ceil(x)] - values[lo]) * (x - lo)


def score(record, outcome, pair, interval=0.9):
    """Pairwise margin, candidate-vs-field win event; ties excluded, not guessed."""
    if not scorable(outcome) or not record.output_draws:
        raise ValueError('Missing outcome or draws')
    if record.prior.race.ref != outcome.race.ref:
        raise ValueError('Wrong race mapping')
    if not 0 < interval < 1 or len(pair) != 2 or pair[0] == pair[1]:
        raise ValueError('Invalid interval/pair')
    i, j = (outcome.candidate_ids.index(c) for c in pair)
    votes = outcome.votes
    winners = [k for k, v in enumerate(votes) if v == max(votes)]
    if len(winners) != 1:
        raise ValueError('Unresolved observed winner')
    if any(c.winner_eligibility != 'ELIGIBLE' for c in outcome.race.candidates):
        raise ValueError('Unresolved candidate eligibility')
    draws = record.output_draws
    if any(d.shares.count(max(d.shares)) != 1 for d in draws):
        raise ValueError('Unresolved simulated winner')
    margins = [d.shares[i] - d.shares[j] for d in draws]
    actual = (votes[i] - votes[j]) / sum(votes)
    p = sum(d.shares[i] == max(d.shares) for d in draws) / len(draws)
    y = int(winners[0] == i)
    # Explicit numerical floor only for log loss; retain raw probabilities elsewhere.
    clipped = min(1 - 1e-15, max(1e-15, p))
    low, high = (quantile(margins, q) for q in ((1-interval)/2, (1+interval)/2))
    return dict(margin_absolute_error=abs(sum(margins)/len(margins)-actual),
                brier=(p-y)**2, log_loss=-y*math.log(clipped)-(1-y)*math.log1p(-clipped),
                covered=int(low <= actual <= high), interval_width=high-low,
                probability=p, observed=y,
                baseline_margin_absolute_error=abs(actual), baseline_brier=0.25,
                baseline_log_loss=math.log(2))


def summarize(rows):
    if not rows:
        return {'n': 0, 'metrics': None, 'calibration': []}
    keys = ('margin_absolute_error', 'brier', 'log_loss', 'covered', 'interval_width',
            'baseline_margin_absolute_error', 'baseline_brier', 'baseline_log_loss')
    bins = []
    for b in range(10):
        group = [r for r in rows if min(9, int(r['probability']*10)) == b]
        bins.append(dict(lower=b/10, upper=(b+1)/10, n=len(group),
            mean_probability=sum(r['probability'] for r in group)/len(group) if group else None,
            observed_frequency=sum(r['observed'] for r in group)/len(group) if group else None))
    n = len(rows)
    coverage = sum(r['covered'] for r in rows) / n
    z = 1.959963984540054
    center = (coverage + z*z/(2*n)) / (1 + z*z/n)
    radius = z*math.sqrt(coverage*(1-coverage)/n + z*z/(4*n*n)) / (1+z*z/n)
    return dict(n=n, coverage_wilson_95=[center-radius, center+radius], metrics={k: sum(r[k] for r in rows)/len(rows) for k in keys}, calibration=bins)


def evaluate(outcomes, predict, *, sparse_threshold=3):
    """predict(training, race, cutoff) -> (ForecastRecord, training_race_ids, poll_count).

    Adapter declares complete training membership for audit; opaque hidden training
    data cannot be independently certified. Sparse = unique selected poll envelopes.
    """
    if type(sparse_threshold) is not int or sparse_threshold < 1:
        raise ValueError('Positive sparse threshold required')
    ids = [o.race.race_id for o in outcomes]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate outcome race')
    rows, exclusions, provenance = [], [], []
    for target in sorted(outcomes, key=lambda o: (o.race.election_date, o.race.race_id)):
        provenance.append(dict(race_id=target.race.race_id, origin=target.origin,
            outcome_evidence=content_id(target.evidence), raw_sha256=target.evidence.raw_payload.sha256,
            source_url=target.evidence.source_url, received_at=target.evidence.received_at.isoformat()))
        for days in (30, 7, 1):
            cutoff = cutoff_for(target.race, days)
            train = chronological_training(outcomes, target, cutoff)
            if not scorable(target):
                exclusions.append(dict(race_id=target.race.race_id, days=days, reason='Missing/unverified outcome'))
                continue
            if not train:
                exclusions.append(dict(race_id=target.race.race_id, days=days, reason='No earlier eligible cycle'))
                continue
            record, used_ids, poll_count = predict(train, target.race, cutoff)
            if len(set(used_ids)) != len(used_ids) or not set(used_ids) <= {o.race.race_id for o in train}:
                raise ValueError('Undeclared or held-out training membership')
            audit_forecast(record, target, cutoff, train)
            polls = {e.evidence_id for e in record.run.inputs.evidence.catalog
                     if e.source_kind == 'POLL' and e.evidence_id in record.run.inputs.evidence.selected_ids}
            if poll_count != len(polls):
                raise ValueError('Poll count disagrees with deduplicated evidence')
            result = score(record, target, target.candidate_ids[:2])
            origin = 'SYNTHETIC' if (target.origin == 'SYNTHETIC' or record.origin == 'SYNTHETIC'
                or any(o.origin == 'SYNTHETIC' for o in train if o.race.race_id in used_ids)) else 'REAL'
            rows.append(dict(**result, origin=origin, office=target.race.office,
                sparse=poll_count < sparse_threshold, days=days, cycle=target.cycle,
                race_id=target.race.race_id, forecast_sha256=content_id(record), training_ids=list(used_ids)))
    groups = {}
    for origin in ('REAL', 'SYNTHETIC'):
        for days in (30, 7, 1):
            for office in ('ALL', 'US_SENATE', 'GOVERNOR'):
                for sparse in ('ALL', True, False):
                    subset = [r for r in rows if r['origin'] == origin and r['days'] == days
                              and (office == 'ALL' or r['office'] == office)
                              and (sparse == 'ALL' or r['sparse'] == sparse)]
                    groups[f'{origin}/{days}/{office}/{sparse}'] = summarize(subset)
    return dict(groups=groups, rows=rows, exclusions=exclusions, provenance=provenance,
        unique_races=len({r['race_id'] for r in rows}),
        configuration=dict(horizons=[30, 7, 1], sparse_threshold=sparse_threshold, interval=0.9,
            margin_units='fraction of all valid contest votes',
            event='first roster candidate wins against field',
            margin_pair='first two roster candidates; no party inference',
            log_probability_floor=1e-15, quantile='linear interpolation',
            cutoff='local midnight minus calendar days',
            holdout='strictly earlier election years with labels received by cutoff'),
        limitations=['Adapter training membership is declared; opaque parameter leakage cannot be proven absent.',
                      'Local performance requires separate authentic artifacts; baseline is zero margin / 50% event probability.',
                      'Coverage Wilson intervals assume independent races within a subgroup; repeated horizons are correlated. No empirical edge claimed.'])


def historical_replay(race, observations, cutoffs, *, verify_raw):
    """Replay only independently verified REAL timestamped official observations.

    verify_raw(envelope) must retrieve/hash-check retained bytes and establish that
    original receipt/source timestamps are authentic. Returns audit materialization,
    never a county-extrapolated forecast or a certified active CountingState.
    """
    if not observations:
        return []
    if len({o.observation_id for o in observations}) != len(observations):
        raise ValueError('Duplicate replay observation')
    for o in observations:
        if (not isinstance(o, CountingObservation) or o.evidence.origin != 'REAL'
            or o.evidence.quality != 'VERIFIED_OFFICIAL' or o.source_as_of is None
            or o.source_as_of > o.evidence.received_at or verify_raw(o.evidence) is not True):
            raise ValueError('Authentic historical timestamped evidence required')
    ordered = tuple(sorted(observations, key=lambda o: (*o.evidence.receipt_key, o.observation_id)))
    materialize_counts(race, ordered)
    result = []
    for cutoff in cutoffs:
        if cutoff.tzinfo is None:
            raise ValueError('Offset cutoff required')
        prefix = tuple(o for o in ordered if o.evidence.received_at <= cutoff)
        result.append((cutoff, materialize_counts(race, prefix)))
    return result


def report_artifact(report, run):
    """Return report bytes/reference paired with caller's complete EVALUATION run.

    Persistence is caller-owned. Run inputs must contain the exact scored forecast
    hashes; this does not promote any model's status.
    """
    import json
    from hashlib import sha256
    from forecasting.contracts import ArtifactRef, canonical_json
    if run.role != 'EVALUATION':
        raise ValueError('Evaluation run required')
    hashes = {item.artifact.sha256 for item in run.inputs.ordered_inputs}
    if not {r['forecast_sha256'] for r in report['rows']} <= hashes:
        raise ValueError('Evaluation run omits scored forecast artifacts')
    payload = json.dumps(dict(report=report, run=json.loads(canonical_json(run))),
                         sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    digest = sha256(payload).hexdigest()
    return ArtifactRef(uri=f'sha256:{digest}', sha256=digest), payload
