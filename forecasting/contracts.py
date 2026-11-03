"""Version-one immutable forecasting interchange. No models, I/O or market mapping."""
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
import re
from types import UnionType
from typing import Literal, get_args, get_origin, get_type_hints


def require(condition: bool, message: str):
    if not condition:
        raise ValueError(message)


def _typed(value, annotation):
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        return value in args and type(value) in tuple(type(a) for a in args)
    if origin is UnionType:
        return any(_typed(value, a) for a in args)
    if origin is tuple:
        return type(value) is tuple and (all(_typed(v, args[0]) for v in value)
            if len(args) == 2 and args[1] is Ellipsis else
            len(value) == len(args) and all(_typed(v, a) for v, a in zip(value, args)))
    return type(value) is annotation


@dataclass(frozen=True, kw_only=True)
class Contract:
    schema_version: int = 1

    def __post_init__(self):
        require(self.schema_version == 1, 'Unsupported schema version')
        for f in fields(self):
            value = getattr(self, f.name)
            require(_typed(value, get_type_hints(type(self))[f.name]), f'Invalid type: {f.name}')
            if isinstance(value, str):
                require(bool(value.strip()), f'Empty {f.name}')
            if isinstance(value, datetime):
                require(value.tzinfo is not None and value.utcoffset() is not None,
                        f'Naive timestamp: {f.name}')
            if isinstance(value, float):
                require(math.isfinite(value), f'Nonfinite {f.name}')
        self.validate()

    def validate(self):
        pass


@dataclass(frozen=True, kw_only=True)
class ArtifactRef(Contract):
    uri: str
    sha256: str

    def validate(self):
        require(re.fullmatch('[0-9a-f]{64}', self.sha256) is not None, 'Invalid SHA256')


@dataclass(frozen=True, kw_only=True)
class Candidate(Contract):
    candidate_id: str
    name: str
    party: str
    kind: Literal['NAMED', 'WRITE_IN_AGGREGATE'] = 'NAMED'


@dataclass(frozen=True, kw_only=True)
class ReportingUnit(Contract):
    unit_id: str
    state: str
    level: Literal['STATE', 'COUNTY']
    parent_id: str | None = None

    def validate(self):
        require(self.state in STATES, 'Unsupported state')
        require((self.level == 'STATE') == (self.parent_id is None), 'Invalid unit parent')


STATES = frozenset('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split())


@dataclass(frozen=True, kw_only=True)
class RaceDefinition(Contract):
    race_id: str
    state: str
    office: Literal['US_SENATE', 'GOVERNOR']
    election_date: date
    seat: str
    contest: Literal['REGULAR', 'SPECIAL']
    rules: Literal['SINGLE_SEAT_PLURALITY']
    rules_evidence: ArtifactRef
    candidates: tuple[Candidate, ...]
    reporting_units: tuple[ReportingUnit, ...]
    ballot_methods: tuple[str, ...]

    def validate(self):
        require(self.state in STATES, 'Unsupported state')
        ids = tuple(c.candidate_id for c in self.candidates)
        require(len(ids) >= 2 and len(set(ids)) == len(ids), 'Candidate identities must be unique')
        units = self.reporting_units
        require(len({u.unit_id for u in units}) == len(units), 'Duplicate units')
        roots = [u for u in units if u.level == 'STATE']
        require(len(roots) == 1 and all(u.state == self.state for u in units), 'Invalid geography')
        require(all(u.parent_id == roots[0].unit_id for u in units if u.level == 'COUNTY'), 'Invalid parent')
        require(bool(self.ballot_methods) and len(set(self.ballot_methods)) == len(self.ballot_methods)
                and all(m.strip() for m in self.ballot_methods), 'Invalid ballot methods')
        require('ALL' not in self.ballot_methods, 'ALL is coverage, not a disjoint method')


@dataclass(frozen=True, kw_only=True)
class EvidenceEnvelope(Contract):
    evidence_id: str
    source_id: str
    source_url: str
    published_at: datetime | None
    received_at: datetime
    raw_payload: ArtifactRef
    quality: Literal['UNVERIFIED', 'VERIFIED_OFFICIAL', 'VERIFIED_NONOFFICIAL', 'REJECTED']
    revision: int = 0
    supersedes_id: str | None = None

    def validate(self):
        require(self.revision >= 0 and (self.revision == 0) == (self.supersedes_id is None), 'Invalid revision')
        require(self.supersedes_id != self.evidence_id, 'Self revision')
        require(self.published_at is None or self.published_at.utcoffset() is not None, 'Naive publication')
        require(self.published_at is None or self.published_at <= self.received_at, 'Future publication')


@dataclass(frozen=True, kw_only=True)
class Coverage(Contract):
    unit_id: str
    ballot_method: str  # ALL or one disjoint RaceDefinition method


@dataclass(frozen=True, kw_only=True)
class PollObservation(Contract):
    poll_id: str
    race_id: str
    evidence: EvidenceEnvelope
    field_start: date
    field_end: date
    sample_size: int
    population: Literal['LV', 'RV', 'ADULTS']
    candidate_ids: tuple[str, ...]
    shares: tuple[float, ...]
    undecided_share: float
    other_share: float

    def validate(self):
        require(self.field_start <= self.field_end and self.sample_size > 0, 'Invalid poll fieldwork')
        require(bool(self.candidate_ids) and len(self.candidate_ids) == len(self.shares) and len(set(self.candidate_ids)) == len(self.candidate_ids), 'Invalid poll candidates')
        values = (*self.shares, self.undecided_share, self.other_share)
        require(all(math.isfinite(v) and 0 <= v <= 1 for v in values) and abs(sum(values)-1) < 1e-9, 'Invalid poll shares')


@dataclass(frozen=True, kw_only=True)
class CountingObservation(Contract):
    observation_id: str
    race_id: str
    evidence: EvidenceEnvelope
    coverage: Coverage
    candidate_ids: tuple[str, ...]
    votes: tuple[int, ...]
    semantics: Literal['CUMULATIVE', 'INCREMENTAL', 'CORRECTION']
    base_observation_id: str | None = None
    certified: bool = False

    def validate(self):
        require(bool(self.candidate_ids) and len(self.candidate_ids) == len(self.votes) and len(set(self.candidate_ids)) == len(self.candidate_ids), 'Invalid vote vector')
        require(all(type(v) is int and v >= 0 for v in self.votes), 'Votes must be nonnegative integers')
        require((self.semantics == 'CUMULATIVE') == (self.base_observation_id is None), 'Missing/unexpected base')
        require(self.base_observation_id != self.observation_id, 'Self base')


@dataclass(frozen=True, kw_only=True)
class ModelArtifact(Contract):
    model_id: str
    role: Literal['STATEWIDE_PRIOR', 'LOCAL_ALLOCATION', 'COUNTING', 'EVALUATION']
    status: Literal['EXPERIMENTAL', 'HISTORICALLY_EVALUATED']
    implementation: ArtifactRef
    parameters: ArtifactRef
    training_manifest: ArtifactRef
    evaluation_report: ArtifactRef | None = None

    def validate(self):
        require(self.status != 'HISTORICALLY_EVALUATED' or self.evaluation_report is not None, 'Evaluation report required')


@dataclass(frozen=True, kw_only=True)
class JointDraw(Contract):
    shares: tuple[float, ...]
    total_valid_votes: int

    def validate(self):
        require(self.total_valid_votes > 0 and len(self.shares) >= 2, 'Invalid draw')
        require(all(math.isfinite(v) and 0 <= v <= 1 for v in self.shares) and abs(sum(self.shares)-1) < 1e-9, 'Invalid simplex')


@dataclass(frozen=True, kw_only=True)
class ElectionPrior(Contract):
    prior_id: str
    race: RaceDefinition
    evidence_cutoff: datetime
    evidence: tuple[EvidenceEnvelope, ...]
    model: ModelArtifact
    local_allocation_model: ModelArtifact
    local_allocation: ArtifactRef
    draws: tuple[JointDraw, ...]

    def validate(self):
        require(self.model.role == 'STATEWIDE_PRIOR' and self.local_allocation_model.role == 'LOCAL_ALLOCATION', 'Wrong model role')
        require(bool(self.draws) and all(len(d.shares) == len(self.race.candidates) for d in self.draws), 'Invalid joint draws')
        validate_evidence(self.evidence, self.evidence_cutoff)


def validate_evidence(evidence, cutoff):
    require(len({e.evidence_id for e in evidence}) == len(evidence), 'Duplicate evidence')
    require(all(e.received_at <= cutoff and e.quality != 'REJECTED' for e in evidence), 'Ineligible evidence at cutoff')


def validate_count(race, observation):
    require(observation.race_id == race.race_id, 'Wrong race')
    require(observation.candidate_ids == tuple(c.candidate_id for c in race.candidates), 'Wrong candidate order')
    require(observation.coverage.unit_id in {u.unit_id for u in race.reporting_units}, 'Unknown unit')
    require(observation.coverage.ballot_method in ('ALL', *race.ballot_methods), 'Unknown method')
    require(observation.evidence.quality == 'VERIFIED_OFFICIAL', 'Official verified results required')


@dataclass(frozen=True, kw_only=True)
class CountingState(Contract):
    state_id: str
    prior: ElectionPrior
    evidence_cutoff: datetime
    observations: tuple[CountingObservation, ...]
    materialized_counts: ArtifactRef
    previous_state: ArtifactRef | None = None

    def validate(self):
        require(self.prior.evidence_cutoff <= self.evidence_cutoff, 'Prior after counting cutoff')
        seen = {}
        latest = {}
        for obs in self.observations:
            validate_count(self.prior.race, obs)
            if obs.base_observation_id is not None:
                base = seen.get(obs.base_observation_id)
                require(base is not None and base.coverage == obs.coverage
                        and latest.get(obs.coverage) == base.observation_id, 'Base must be latest in same coverage')
            require(obs.observation_id not in seen, 'Duplicate observation')
            seen[obs.observation_id] = obs
            latest[obs.coverage] = obs.observation_id
        evidence_by_id = {}
        for obs in self.observations:
            existing = evidence_by_id.get(obs.evidence.evidence_id)
            require(existing is None or existing == obs.evidence, 'Conflicting evidence identity')
            evidence_by_id[obs.evidence.evidence_id] = obs.evidence
        validate_evidence(tuple(evidence_by_id.values()), self.evidence_cutoff)


@dataclass(frozen=True, kw_only=True)
class ForecastRecord(Contract):
    forecast_id: str
    created_at: datetime
    evidence_cutoff: datetime
    prior: ElectionPrior
    counting_state: CountingState | None
    models: tuple[ModelArtifact, ...]
    evidence_manifest: ArtifactRef
    data_version: str
    code: ArtifactRef
    environment: ArtifactRef
    configuration: ArtifactRef
    random_seed: int
    output_draws: tuple[JointDraw, ...]
    quality: Literal['EXPERIMENTAL', 'EVALUATED', 'DEGRADED', 'WITHHELD']
    quality_reasons: tuple[str, ...]

    def validate(self):
        require(self.prior.evidence_cutoff <= self.evidence_cutoff <= self.created_at, 'Invalid forecast times')
        require(bool(self.models), 'Model manifest required')
        required = (self.prior.model, self.prior.local_allocation_model)
        require(all(m in self.models for m in required), 'Missing prior models')
        if self.counting_state is not None:
            require(self.counting_state.prior == self.prior and self.counting_state.evidence_cutoff == self.evidence_cutoff, 'Counting handoff mismatch')
            require(any(m.role == 'COUNTING' for m in self.models), 'Missing counting model')
        require(self.quality != 'EVALUATED' or all(m.status == 'HISTORICALLY_EVALUATED' for m in self.models), 'Experimental model')
        require(self.quality != 'EVALUATED' or all(e.quality in ('VERIFIED_OFFICIAL', 'VERIFIED_NONOFFICIAL')
                for e in self.prior.evidence), 'Unverified evidence')
        require(self.quality not in ('DEGRADED', 'WITHHELD') or bool(self.quality_reasons), 'Quality reason required')
        require((bool(self.output_draws) or self.quality == 'WITHHELD') and all(len(d.shares) == len(self.prior.race.candidates) for d in self.output_draws), 'Invalid forecast draws')


def canonical_json(value: Contract) -> str:
    """Stable UTF-8 JSON; aware instants normalized to UTC. Hash is storage identity."""
    def encode(v):
        if is_dataclass(v):
            return {'type': type(v).__name__, **{f.name: encode(getattr(v, f.name)) for f in fields(v)}}
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).isoformat(timespec='microseconds')
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, tuple):
            return [encode(x) for x in v]
        return v
    return json.dumps(encode(value), sort_keys=True, separators=(',', ':'), allow_nan=False)


def content_id(value: Contract) -> str:
    return sha256(canonical_json(value).encode('utf-8')).hexdigest()


def from_json(payload: str) -> Contract:
    """Strict reader: unknown types, versions and fields fail; no implicit migration."""
    registry = {c.__name__: c for c in Contract.__subclasses__()}
    def decode(v):
        if isinstance(v, list):
            return tuple(decode(x) for x in v)
        if not isinstance(v, dict):
            return v
        data = dict(v)
        cls = registry[data.pop('type')]
        require(data.get('schema_version') == 1, 'Explicit supported schema version required')
        hints = get_type_hints(cls)
        for key, item in data.items():
            annotation = hints[key]
            if annotation is datetime or (get_origin(annotation) is UnionType and datetime in get_args(annotation)):
                data[key] = None if item is None else datetime.fromisoformat(item)
            elif annotation is date:
                data[key] = date.fromisoformat(item)
            else:
                data[key] = decode(item)
        return cls(**data)
    return decode(json.loads(payload))
