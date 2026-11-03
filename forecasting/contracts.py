"""Immutable v2 election interchange and deterministic validation; no forecasting or I/O."""
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
import re
from types import UnionType
from typing import Literal, get_args, get_origin, get_type_hints
from zoneinfo import ZoneInfo

SCHEMA_VERSION = 2
STATES = frozenset('AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split())


def require(condition: bool, message: str) -> None:
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


def _nonempty_unique(values, label):
    require(bool(values) and len(set(values)) == len(values), f'Empty/duplicate {label}')
    require(all(not isinstance(v, str) or bool(v.strip()) for v in values), f'Blank {label}')


def _hash(value):
    require(re.fullmatch('[0-9a-f]{64}', value) is not None, 'Invalid SHA256')


def _simplex(values, tolerance=1e-9):
    require(bool(values) and all(math.isfinite(v) and 0 <= v <= 1 for v in values)
            and abs(math.fsum(values) - 1) <= tolerance, 'Invalid simplex')


@dataclass(frozen=True, kw_only=True)
class Contract:
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        require(type(self.schema_version) is int and self.schema_version == SCHEMA_VERSION,
                'Unsupported schema version; v1 requires explicit migration')
        hints = get_type_hints(type(self))
        for f in fields(self):
            value = getattr(self, f.name)
            require(_typed(value, hints[f.name]), f'Invalid type: {f.name}')
            if isinstance(value, str):
                require(bool(value.strip()), f'Empty {f.name}')
            if isinstance(value, datetime):
                require(value.utcoffset() is not None, f'Naive timestamp: {f.name}')
                object.__setattr__(self, f.name, value.astimezone(timezone.utc))
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
        _hash(self.sha256)


@dataclass(frozen=True, kw_only=True)
class EvidenceEnvelope(Contract):
    evidence_id: str
    source_id: str
    source_object_id: str
    source_url: str
    source_kind: Literal['OFFICIAL_RESULTS', 'OFFICIAL_RULES', 'OFFICIAL_ROSTER', 'POLL', 'OTHER']
    origin: Literal['REAL', 'SYNTHETIC']
    published_at: datetime | None
    received_at: datetime
    receipt_order: int
    raw_payload: ArtifactRef
    quality: Literal['UNVERIFIED', 'VERIFIED_OFFICIAL', 'VERIFIED_NONOFFICIAL', 'REJECTED', 'SYNTHETIC']
    revision: int = 0
    supersedes_id: str | None = None

    @property
    def receipt_key(self):
        return self.received_at, self.receipt_order

    def validate(self):
        require(self.receipt_order >= 0, 'Invalid receipt order')
        require(self.revision >= 0 and (self.revision == 0) == (self.supersedes_id is None), 'Invalid revision')
        require(self.supersedes_id != self.evidence_id, 'Self revision')
        require(self.published_at is None or self.published_at <= self.received_at, 'Future publication')
        require((self.origin == 'SYNTHETIC') == (self.quality == 'SYNTHETIC'), 'Synthetic provenance mismatch')
        require(self.quality != 'VERIFIED_OFFICIAL' or self.source_kind.startswith('OFFICIAL_'), 'Not official evidence')


def _evidence_registry(catalog):
    registry = {}
    receipt_keys = {}
    for e in catalog:
        previous = registry.get(e.evidence_id)
        require(previous is None or previous == e, 'Conflicting evidence ID/hash')
        existing = receipt_keys.get(e.receipt_key)
        require(existing is None or existing == e.evidence_id, 'Conflicting receipt key')
        registry[e.evidence_id] = e
        receipt_keys[e.receipt_key] = e.evidence_id
    children = {}
    roots = {}
    for e in registry.values():
        object_key = e.source_id, e.source_object_id
        if e.supersedes_id is None:
            require(object_key not in roots, 'Multiple evidence roots for source object')
            roots[object_key] = e.evidence_id
        else:
            parent = registry.get(e.supersedes_id)
            require(parent is not None, 'Orphan evidence revision')
            require((parent.source_id, parent.source_object_id, parent.origin, parent.source_kind) ==
                    (*object_key, e.origin, e.source_kind), 'Revision changes source identity')
            require(e.revision == parent.revision + 1 and e.receipt_key > parent.receipt_key,
                    'Revision sequence/chronology error')
            require(parent.evidence_id not in children, 'Forked evidence revisions')
            children[parent.evidence_id] = e.evidence_id
    return registry


@dataclass(frozen=True, kw_only=True)
class EvidenceSelection(Contract):
    cutoff: datetime
    catalog: tuple[EvidenceEnvelope, ...]
    selected_ids: tuple[str, ...]

    def validate(self):
        registry = _evidence_registry(self.catalog)
        require(len(registry) == len(self.catalog), 'Duplicate evidence in catalog')
        require(tuple(sorted(self.catalog, key=lambda e: e.receipt_key)) == self.catalog, 'Unordered evidence catalog')
        require(all(e.received_at <= self.cutoff for e in self.catalog), 'Evidence after cutoff')
        require(len(set(self.selected_ids)) == len(self.selected_ids), 'Duplicate evidence selection')
        latest = {}
        for e in self.catalog:
            latest[e.source_id, e.source_object_id] = e
        expected = tuple(e.evidence_id for e in self.catalog
                         if latest[e.source_id, e.source_object_id] == e and e.quality != 'REJECTED')
        require(self.selected_ids == expected, 'Must explicitly select latest applicable non-rejected revisions')


@dataclass(frozen=True, kw_only=True)
class Candidate(Contract):
    candidate_id: str
    name: str
    party: str
    winner_eligibility: Literal['ELIGIBLE', 'INELIGIBLE', 'UNRESOLVED']
    kind: Literal['NAMED', 'WRITE_IN_AGGREGATE'] = 'NAMED'
    status: Literal['ACTIVE', 'WITHDRAWN', 'REPLACED'] = 'ACTIVE'
    replaced_by: str | None = None

    def validate(self):
        require((self.status == 'REPLACED') == (self.replaced_by is not None), 'Replacement identity required')
        require(self.replaced_by != self.candidate_id, 'Self replacement')
        require(self.kind != 'WRITE_IN_AGGREGATE' or self.winner_eligibility == 'UNRESOLVED', 'Aggregate write-ins are not an individual winner')


@dataclass(frozen=True, kw_only=True)
class ReportingUnit(Contract):
    unit_id: str
    state: str
    level: Literal['STATE', 'COUNTY']
    parent_id: str | None = None

    def validate(self):
        require(self.state in STATES, 'Unsupported state')
        root = f'US:{self.state}'
        if self.level == 'STATE':
            require(self.unit_id == root and self.parent_id is None, 'Invalid state unit')
        else:
            require(re.fullmatch(re.escape(root) + r':\d{5}', self.unit_id) is not None
                    and self.parent_id == root, 'Invalid county identity/parent')


@dataclass(frozen=True, kw_only=True)
class RaceRef(Contract):
    race_id: str
    revision: int
    definition_hash: str

    def validate(self):
        require(self.revision >= 0, 'Invalid race revision')
        _hash(self.definition_hash)


@dataclass(frozen=True, kw_only=True)
class RaceDefinition(Contract):
    race_id: str
    revision: int
    previous_definition_hash: str | None
    state: str
    office: Literal['US_SENATE', 'GOVERNOR']
    election_date: date
    election_timezone: str
    seat: Literal['CLASS1', 'CLASS2', 'CLASS3', 'GOVERNOR']
    term_end: date
    contest: Literal['REGULAR', 'SPECIAL']
    rules: Literal['SINGLE_SEAT_PLURALITY']
    rules_evidence: EvidenceEnvelope
    definition_evidence: EvidenceEnvelope
    candidates: tuple[Candidate, ...]
    reporting_units: tuple[ReportingUnit, ...]
    county_partition_complete: bool
    ballot_methods: tuple[str, ...]

    @property
    def ref(self):
        return RaceRef(race_id=self.race_id, revision=self.revision, definition_hash=content_id(self))

    def validate(self):
        require(self.state in STATES, 'Unsupported state')
        ZoneInfo(self.election_timezone)
        require(self.revision >= 0 and (self.revision == 0) == (self.previous_definition_hash is None), 'Invalid definition revision')
        if self.previous_definition_hash is not None:
            _hash(self.previous_definition_hash)
        require((self.office == 'GOVERNOR') == (self.seat == 'GOVERNOR'), 'Invalid seat/office')
        require(self.term_end > self.election_date, 'Term must end after election')
        identity = f'US:{self.state}:{self.election_date.isoformat()}:{self.office}:{self.seat}:{self.contest}'
        if self.contest == 'SPECIAL':
            identity += f':TERM_END:{self.term_end.isoformat()}'
        require(self.race_id == identity, 'Race ID disagrees with stable contest identity')
        ids = tuple(c.candidate_id for c in self.candidates)
        _nonempty_unique(ids, 'candidate identities')
        require(len(ids) >= 2, 'At least two candidate buckets required')
        require(all(c.replaced_by is None or c.replaced_by in ids for c in self.candidates), 'Unknown replacement')
        require(sum(c.kind == 'WRITE_IN_AGGREGATE' for c in self.candidates) <= 1, 'Multiple write-in aggregates')
        units = self.reporting_units
        _nonempty_unique(tuple(u.unit_id for u in units), 'reporting units')
        require(sum(u.level == 'STATE' for u in units) == 1 and all(u.state == self.state for u in units), 'Invalid geography')
        require(not self.county_partition_complete or any(u.level == 'COUNTY' for u in units), 'No complete county partition')
        _nonempty_unique(self.ballot_methods, 'ballot methods')
        require(not {'ALL', 'UNKNOWN', 'MIXED'} & set(self.ballot_methods), 'Reserved ballot method')
        for e, kind in ((self.rules_evidence, 'OFFICIAL_RULES'), (self.definition_evidence, 'OFFICIAL_ROSTER')):
            require(e.source_kind == kind and e.quality in ('VERIFIED_OFFICIAL', 'SYNTHETIC'), 'Unverified election definition')


def validate_race_revision(previous: RaceDefinition, current: RaceDefinition) -> None:
    require(previous.race_id == current.race_id and current.revision == previous.revision + 1
            and current.previous_definition_hash == content_id(previous), 'Broken definition revision chain')
    require((previous.state, previous.office, previous.election_date, previous.seat, previous.term_end, previous.contest) ==
            (current.state, current.office, current.election_date, current.seat, current.term_end, current.contest), 'Contest identity changed')
    old = tuple(c.candidate_id for c in previous.candidates)
    require(tuple(c.candidate_id for c in current.candidates[:len(old)]) == old,
            'Retain historical candidate columns; append replacements')


@dataclass(frozen=True, kw_only=True)
class Coverage(Contract):
    unit_id: str
    ballot_method: str
    method_status: Literal['KNOWN', 'MIXED', 'UNKNOWN'] = 'KNOWN'
    mixed_methods: tuple[str, ...] = ()

    def validate(self):
        if self.method_status == 'KNOWN':
            require(self.ballot_method not in ('UNKNOWN', 'MIXED') and not self.mixed_methods, 'Invalid known method')
        elif self.method_status == 'MIXED':
            _nonempty_unique(self.mixed_methods, 'mixed methods')
            require(self.ballot_method == 'MIXED' and len(self.mixed_methods) >= 2
                    and tuple(sorted(self.mixed_methods)) == self.mixed_methods, 'Invalid mixed method coverage')
        else:
            require(self.ballot_method == 'UNKNOWN' and not self.mixed_methods, 'Invalid unknown method coverage')


def coverage_cells(race: RaceDefinition, coverage: Coverage) -> set[tuple[str, str]]:
    require(coverage.unit_id in {u.unit_id for u in race.reporting_units}, 'Unknown reporting unit')
    if coverage.method_status == 'MIXED':
        methods = set(coverage.mixed_methods)
        require(methods <= set(race.ballot_methods), 'Unknown mixed method')
    elif coverage.ballot_method == 'ALL' or coverage.method_status == 'UNKNOWN':
        methods = set(race.ballot_methods)
    else:
        require(coverage.ballot_method in race.ballot_methods, 'Unknown ballot method')
        methods = {coverage.ballot_method}
    counties = {u.unit_id for u in race.reporting_units if u.level == 'COUNTY'}
    if not race.county_partition_complete:
        counties.add('@UNLISTED')
    units = counties if coverage.unit_id == f'US:{race.state}' else {coverage.unit_id}
    return {(unit, method) for unit in units for method in methods}


def validate_partition(race: RaceDefinition, coverages: tuple[Coverage, ...],
                       require_complete: bool = False) -> None:
    require(bool(coverages), 'Empty aggregation partition')
    cells = set()
    for c in coverages:
        new = coverage_cells(race, c)
        require(not cells & new, 'Overlapping geographic/ballot-method aggregation')
        cells |= new
    if require_complete:
        require(all(c.method_status != 'UNKNOWN' for c in coverages), 'Ambiguous method coverage')
        expected = coverage_cells(race, Coverage(unit_id=f'US:{race.state}', ballot_method='ALL'))
        require(cells == expected, 'Incomplete statewide aggregation partition')


@dataclass(frozen=True, kw_only=True)
class VoteQuantities(Contract):
    ballots_cast: int | None = None
    valid_contest_votes: int | None = None
    undervotes: int | None = None
    overvotes: int | None = None
    invalid_contest_ballots: int | None = None

    def validate(self):
        values = (self.valid_contest_votes, self.undervotes, self.overvotes, self.invalid_contest_ballots)
        require(all(v is None or v >= 0 for v in (self.ballots_cast, *values)), 'Negative vote quantity')
        if self.ballots_cast is not None:
            require(sum(v for v in values if v is not None) <= self.ballots_cast, 'Contest quantities exceed ballots')
            if all(v is not None for v in values):
                require(sum(values) == self.ballots_cast, 'Ballot/contest accounting mismatch')


def _validate_votes(votes, quantities):
    require(all(v is None or type(v) is int and v >= 0 for v in votes), 'Invalid candidate count')
    known = sum(v for v in votes if v is not None)
    if quantities.valid_contest_votes is not None:
        require(known <= quantities.valid_contest_votes, 'Candidate counts exceed contest total')
        if all(v is not None for v in votes):
            require(known == quantities.valid_contest_votes, 'Candidate/contest denominator mismatch')
    if quantities.ballots_cast is not None:
        require(known <= quantities.ballots_cast, 'Candidate votes exceed ballots')


@dataclass(frozen=True, kw_only=True)
class CompletenessMetric(Contract):
    denominator_name: Literal['REPORTING_UNITS', 'EXPECTED_BALLOTS', 'EXPECTED_VALID_CONTEST_VOTES']
    numerator: int | None
    denominator: int | None
    basis: Literal['EXACT', 'ESTIMATED']

    def validate(self):
        require(self.numerator is None or self.numerator >= 0, 'Invalid completeness numerator')
        require(self.denominator is None or self.denominator > 0, 'Invalid completeness denominator')
        if self.numerator is not None and self.denominator is not None:
            require(self.numerator <= self.denominator, 'Completeness exceeds denominator')


@dataclass(frozen=True, kw_only=True)
class PollObservation(Contract):
    poll_id: str
    race_id: str
    race_definition: RaceRef
    evidence: EvidenceEnvelope
    field_start: date
    field_end: date
    field_timezone: str
    sample_size: int
    population: Literal['LV', 'RV', 'ADULTS']
    candidate_ids: tuple[str, ...]
    shares: tuple[float, ...]
    undecided_share: float
    other_share: float

    def validate(self):
        require(self.race_id == self.race_definition.race_id, 'Poll race mismatch')
        require(self.field_start <= self.field_end and self.sample_size > 0, 'Invalid poll fieldwork')
        ZoneInfo(self.field_timezone)
        _nonempty_unique(self.candidate_ids, 'poll candidates')
        require(len(self.candidate_ids) == len(self.shares), 'Invalid poll vector')
        _simplex((*self.shares, self.undecided_share, self.other_share))
        for instant in (self.evidence.published_at, self.evidence.received_at):
            require(instant is None or self.field_end <= instant.astimezone(ZoneInfo(self.field_timezone)).date(),
                    'Fieldwork ends after publication/receipt local date')
        require(self.evidence.source_kind == 'POLL' and self.evidence.quality != 'REJECTED', 'Not eligible poll evidence')


def validate_poll(race: RaceDefinition, observation: PollObservation,
                  cutoff: datetime | None = None) -> None:
    require(observation.race_definition == race.ref and observation.race_id == race.race_id, 'Wrong poll race revision')
    ids = tuple(c.candidate_id for c in race.candidates)
    require(tuple(c for c in ids if c in observation.candidate_ids) == observation.candidate_ids, 'Poll candidates must be ordered roster subset')
    require(observation.field_end <= race.election_date, 'Post-election poll fieldwork')
    if cutoff is not None:
        require(cutoff.utcoffset() is not None and observation.evidence.received_at <= cutoff, 'Poll unavailable at cutoff')
        require(race.definition_evidence.received_at <= cutoff and race.rules_evidence.received_at <= cutoff, 'Definition unavailable at cutoff')


@dataclass(frozen=True, kw_only=True)
class CountingObservation(Contract):
    observation_id: str
    race_id: str
    race_definition: RaceRef
    evidence: EvidenceEnvelope
    coverage: Coverage
    candidate_ids: tuple[str, ...]
    votes: tuple[int | None, ...]
    quantities: VoteQuantities
    reporting_status: Literal['NOT_REPORTED', 'PARTIAL', 'COMPLETE', 'UNKNOWN']
    completeness: tuple[CompletenessMetric, ...]
    source_sequence: int
    source_as_of: datetime | None
    semantics: Literal['CUMULATIVE', 'INCREMENTAL', 'CORRECTION']
    base_observation_id: str | None = None
    certified: bool = False

    def validate(self):
        require(self.race_id == self.race_definition.race_id, 'Counting race mismatch')
        _nonempty_unique(self.candidate_ids, 'counting candidates')
        require(len(self.candidate_ids) == len(self.votes), 'Invalid vote vector')
        _validate_votes(self.votes, self.quantities)
        require(self.source_sequence >= 0, 'Invalid source sequence')
        require(self.source_as_of is None or self.source_as_of <= (self.evidence.published_at or self.evidence.received_at), 'Future source snapshot')
        require((self.semantics == 'CUMULATIVE') == (self.base_observation_id is None), 'Missing/unexpected base')
        require(self.base_observation_id != self.observation_id, 'Self base')
        require(self.reporting_status != 'COMPLETE' or all(v is not None for v in self.votes), 'Complete result has missing candidates')
        require(self.reporting_status != 'NOT_REPORTED' or all(v is None for v in self.votes), 'Unreported unit has candidate counts')
        require(len({m.denominator_name for m in self.completeness}) == len(self.completeness), 'Duplicate completeness denominators')
        if self.semantics == 'INCREMENTAL':
            require(self.reporting_status != 'COMPLETE', 'An increment is not a completed total')


def _validate_count_structure(race: RaceDefinition, observation: CountingObservation) -> None:
    require(observation.race_id == race.race_id and observation.race_definition == race.ref, 'Wrong counting race revision')
    require(observation.candidate_ids == tuple(c.candidate_id for c in race.candidates), 'Wrong candidate order')
    coverage_cells(race, observation.coverage)
    require(observation.evidence.source_kind == 'OFFICIAL_RESULTS', 'Official result provenance required')


def validate_count(race: RaceDefinition, observation: CountingObservation) -> None:
    _validate_count_structure(race, observation)
    require(observation.evidence.quality in ('VERIFIED_OFFICIAL', 'SYNTHETIC'), 'Official result provenance required')


@dataclass(frozen=True, kw_only=True)
class InputArtifact(Contract):
    artifact: ArtifactRef
    kind: Literal['RAW', 'RACE_DEFINITION', 'POLL', 'FEATURES', 'COUNTS', 'PRIOR', 'JOINT_DRAWS', 'LOCAL_ALLOCATION', 'LINEAGE']
    version: str
    available_at: datetime
    source_evidence_ids: tuple[str, ...]

    def validate(self):
        _nonempty_unique(self.source_evidence_ids, 'input source lineage')


@dataclass(frozen=True, kw_only=True)
class InputManifest(Contract):
    race_definition: RaceRef
    evidence: EvidenceSelection
    ordered_inputs: tuple[InputArtifact, ...]
    data_version: str
    training_cutoff: datetime | None

    def validate(self):
        require(bool(self.ordered_inputs), 'Input manifest is empty')
        require(len(set(self.ordered_inputs)) == len(self.ordered_inputs), 'Duplicate input entries')
        registry = {e.evidence_id: e for e in self.evidence.catalog}
        used = set()
        for item in self.ordered_inputs:
            require(item.available_at <= self.evidence.cutoff, 'Input unavailable at evidence cutoff')
            require(set(item.source_evidence_ids) <= registry.keys(), 'Unknown input source lineage')
            require(all(registry[e].received_at <= item.available_at for e in item.source_evidence_ids), 'Input predates its source receipt')
            if item.kind in ('RAW', 'LINEAGE'):
                require(len(item.source_evidence_ids) == 1 and item.artifact.sha256 ==
                        registry[item.source_evidence_ids[0]].raw_payload.sha256, 'Raw input hash/lineage mismatch')
            if item.kind != 'LINEAGE':
                require(set(item.source_evidence_ids) <= set(self.evidence.selected_ids), 'Input consumes superseded/rejected evidence')
                used.update(item.source_evidence_ids)
        require(used == set(self.evidence.selected_ids), 'Selected evidence absent from input lineage')
        raw_hashes = {i.artifact.sha256 for i in self.ordered_inputs if i.kind in ('RAW', 'LINEAGE')}
        require(all(e.raw_payload.sha256 in raw_hashes for e in self.evidence.catalog), 'Missing raw evidence input')
        if self.training_cutoff is not None:
            require(self.training_cutoff <= self.evidence.cutoff and all(i.available_at <= self.training_cutoff for i in self.ordered_inputs), 'Training leakage')
        require(any(i.kind == 'RACE_DEFINITION' and i.artifact.sha256 == self.race_definition.definition_hash for i in self.ordered_inputs), 'Missing versioned race input')


@dataclass(frozen=True, kw_only=True)
class ModelArtifact(Contract):
    model_id: str
    role: Literal['STATEWIDE_PRIOR', 'LOCAL_ALLOCATION', 'COUNTING', 'EVALUATION']
    origin: Literal['REAL', 'SYNTHETIC']
    status: Literal['EXPERIMENTAL', 'HISTORICALLY_EVALUATED']
    implementation: ArtifactRef
    parameters: ArtifactRef
    feature_version: ArtifactRef
    transformation_version: ArtifactRef
    fitted_at: datetime
    training_manifest: InputManifest
    evaluation_report: ArtifactRef | None = None

    def validate(self):
        require(self.training_manifest.training_cutoff is not None and self.training_manifest.training_cutoff <= self.fitted_at, 'Missing/invalid training cutoff')
        synthetic = any(e.origin == 'SYNTHETIC' for e in self.training_manifest.evidence.catalog)
        require(not synthetic or self.origin == 'SYNTHETIC', 'Synthetic training lineage')
        require(self.status != 'HISTORICALLY_EVALUATED' or self.evaluation_report is not None, 'Evaluation report required')
        require(self.origin != 'SYNTHETIC' or self.status == 'EXPERIMENTAL', 'Synthetic model cannot claim historical evaluation')


@dataclass(frozen=True, kw_only=True)
class RNGStream(Contract):
    name: str
    spawn_key: tuple[int, ...]

    def validate(self):
        require(all(i >= 0 for i in self.spawn_key), 'Negative RNG stream key')


@dataclass(frozen=True, kw_only=True)
class RNGSpec(Contract):
    algorithm: str
    implementation_version: str
    stream_derivation: ArtifactRef
    seed: int
    streams: tuple[RNGStream, ...]

    def validate(self):
        require(self.seed >= 0, 'Negative RNG seed')
        _nonempty_unique(tuple(s.name for s in self.streams), 'RNG stream names')
        _nonempty_unique(tuple(s.spawn_key for s in self.streams), 'RNG stream keys')


@dataclass(frozen=True, kw_only=True)
class RunManifest(Contract):
    run_id: str
    role: Literal['STATEWIDE_PRIOR', 'LOCAL_ALLOCATION', 'COUNTING', 'EVALUATION']
    race_definition: RaceRef
    inputs: InputManifest
    fitted_models: tuple[ModelArtifact, ...]
    created_at: datetime
    code: ArtifactRef
    environment: ArtifactRef
    rng: RNGSpec
    sampling_procedure: ArtifactRef
    configuration: ArtifactRef
    numeric_tolerance: float
    arithmetic: Literal['FLOAT64_SHARES_INTEGER_COUNTS'] = 'FLOAT64_SHARES_INTEGER_COUNTS'

    def validate(self):
        require(self.race_definition == self.inputs.race_definition, 'Run/input definition mismatch')
        require(self.inputs.evidence.cutoff <= self.created_at, 'Run before evidence cutoff')
        require(0 <= self.numeric_tolerance <= 1e-6, 'Unsupported numeric tolerance')
        _nonempty_unique(tuple(m.model_id for m in self.fitted_models), 'fitted models')
        require(any(m.role == self.role for m in self.fitted_models), 'Missing model for run role')
        for model in self.fitted_models:
            require(model.training_manifest.race_definition == self.race_definition, 'Model/race mismatch')
            require(model.training_manifest.training_cutoff <= self.inputs.evidence.cutoff, 'Future training inputs')
            require(model.fitted_at <= self.created_at, 'Model fitted after run')
        validate_dependency_graph(self)


@dataclass(frozen=True, kw_only=True)
class JointDraw(Contract):
    shares: tuple[float, ...]
    total_valid_votes: int

    def validate(self):
        require(self.total_valid_votes > 0 and len(self.shares) >= 2, 'Invalid draw')
        _simplex(self.shares)


@dataclass(frozen=True, kw_only=True)
class JointSimulation(Contract):
    race_definition: RaceRef
    candidate_ids: tuple[str, ...]
    draws: tuple[JointDraw, ...]

    def validate(self):
        _nonempty_unique(self.candidate_ids, 'joint candidate axis')
        require(bool(self.draws) and all(len(d.shares) == len(self.candidate_ids) for d in self.draws), 'Invalid joint simulation')


@dataclass(frozen=True, kw_only=True)
class LocalDraw(Contract):
    statewide_draw_index: int
    residual_draw_index: int
    conditional_weight: float
    cell_votes: tuple[tuple[int, ...], ...]
    cell_quantities: tuple[VoteQuantities, ...]

    @property
    def support_shares(self):
        return tuple(tuple(v / sum(row) for v in row) if sum(row) else None for row in self.cell_votes)

    def validate(self):
        require(self.statewide_draw_index >= 0 and self.residual_draw_index >= 0
                and 0 < self.conditional_weight <= 1, 'Invalid conditional draw identity/weight')
        require(bool(self.cell_votes) and len(self.cell_votes) == len(self.cell_quantities), 'Invalid local cell axis')
        for row, quantities in zip(self.cell_votes, self.cell_quantities):
            require(bool(row) and quantities.valid_contest_votes == sum(row), 'Local turnout/count mismatch')
            _validate_votes(row, quantities)


@dataclass(frozen=True, kw_only=True)
class LocalAllocationPayload(Contract):
    race_definition: RaceRef
    unit_ids: tuple[str, ...]
    ballot_methods: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    draws: tuple[LocalDraw, ...]
    count_unit: Literal['INTEGER_CONTEST_VOTES'] = 'INTEGER_CONTEST_VOTES'
    share_unit: Literal['FRACTION_OF_VALID_CONTEST_VOTES'] = 'FRACTION_OF_VALID_CONTEST_VOTES'
    margin_unit: Literal['PAIRWISE_SHARE_DIFFERENCE_ALL_CANDIDATES'] = 'PAIRWISE_SHARE_DIFFERENCE_ALL_CANDIDATES'
    conditional_on: Literal['STATEWIDE_CANDIDATE_COUNTS'] = 'STATEWIDE_CANDIDATE_COUNTS'
    residual_representation: Literal['WEIGHTED_EMPIRICAL_JOINT_DRAWS'] = 'WEIGHTED_EMPIRICAL_JOINT_DRAWS'

    @property
    def cells(self):
        return tuple(Coverage(unit_id=u, ballot_method=m) for u in self.unit_ids for m in self.ballot_methods)

    def validate(self):
        for axis in (self.unit_ids, self.ballot_methods, self.candidate_ids):
            _nonempty_unique(axis, 'allocation axis')
        require(bool(self.draws), 'Missing local uncertainty distribution')
        keys = tuple((d.statewide_draw_index, d.residual_draw_index) for d in self.draws)
        require(keys == tuple(sorted(set(keys))), 'Duplicate/unordered conditional draws')
        for draw in self.draws:
            require(len(draw.cell_votes) == len(self.cells) and all(len(row) == len(self.candidate_ids) for row in draw.cell_votes), 'Allocation axes mismatch')
        for parent in {d.statewide_draw_index for d in self.draws}:
            group = [d for d in self.draws if d.statewide_draw_index == parent]
            require(tuple(d.residual_draw_index for d in group) == tuple(range(len(group))), 'Residual indices must be contiguous')
            _simplex(tuple(d.conditional_weight for d in group))


@dataclass(frozen=True, kw_only=True)
class LocalAllocationManifest(Contract):
    allocation_id: str
    race_definition: RaceRef
    payload: ArtifactRef
    statewide_simulation: ArtifactRef
    model_hash: str
    run_hash: str
    local_evaluation_report: ArtifactRef | None

    def validate(self):
        _hash(self.model_hash)
        _hash(self.run_hash)


def validate_local_allocation(race: RaceDefinition, simulation: JointSimulation,
                              payload: LocalAllocationPayload, manifest: LocalAllocationManifest,
                              run: RunManifest) -> None:
    require(race.ref == simulation.race_definition == payload.race_definition == manifest.race_definition == run.race_definition, 'Allocation definition mismatch')
    require(payload.candidate_ids == simulation.candidate_ids == tuple(c.candidate_id for c in race.candidates), 'Allocation candidate order')
    require(payload.ballot_methods == race.ballot_methods, 'Allocation ballot method axis')
    require(all(c.method_status == 'KNOWN' for c in payload.cells), 'Unknown allocation method')
    validate_partition(race, payload.cells, require_complete=True)
    require(manifest.payload.sha256 == content_id(payload) and manifest.statewide_simulation.sha256 == content_id(simulation), 'Allocation artifact binding')
    require(manifest.run_hash == content_id(run) and run.role == 'LOCAL_ALLOCATION', 'Allocation run binding')
    models = [m for m in run.fitted_models if m.role == 'LOCAL_ALLOCATION']
    require(len(models) == 1 and manifest.model_hash == content_id(models[0]), 'Allocation model binding')
    require(manifest.local_evaluation_report == models[0].evaluation_report, 'Local evaluation binding')
    require(any(i.kind == 'JOINT_DRAWS' and i.artifact.sha256 == content_id(simulation) for i in run.inputs.ordered_inputs), 'Allocation run missing statewide draws')
    require({d.statewide_draw_index for d in payload.draws} == set(range(len(simulation.draws))), 'Missing/extra statewide draw')
    for local in payload.draws:
        statewide = simulation.draws[local.statewide_draw_index]
        totals = tuple(sum(row[j] for row in local.cell_votes) for j in range(len(payload.candidate_ids)))
        require(sum(totals) == statewide.total_valid_votes, 'Statewide turnout conservation failure')
        require(all(abs(v / statewide.total_valid_votes - s) <= run.numeric_tolerance for v, s in zip(totals, statewide.shares)), 'Statewide support conservation failure')


@dataclass(frozen=True, kw_only=True)
class ElectionPrior(Contract):
    prior_id: str
    race: RaceDefinition
    evidence_cutoff: datetime
    model: ModelArtifact
    local_allocation_model: ModelArtifact
    draws: tuple[JointDraw, ...]
    local_allocation: LocalAllocationPayload
    allocation_manifest: LocalAllocationManifest
    run: RunManifest
    allocation_run: RunManifest

    @property
    def simulation(self):
        return JointSimulation(race_definition=self.race.ref,
            candidate_ids=tuple(c.candidate_id for c in self.race.candidates), draws=self.draws)

    @property
    def evidence(self):
        return dependency_evidence(self)

    def validate(self):
        require(self.model.role == 'STATEWIDE_PRIOR' and self.local_allocation_model.role == 'LOCAL_ALLOCATION', 'Wrong prior model roles')
        require(self.run.role == 'STATEWIDE_PRIOR' and self.model in self.run.fitted_models
                and self.local_allocation_model in self.allocation_run.fitted_models, 'Prior model/run binding')
        require(self.evidence_cutoff == self.run.inputs.evidence.cutoff == self.allocation_run.inputs.evidence.cutoff, 'Prior run cutoffs disagree')
        require(self.run.race_definition == self.race.ref, 'Prior definition mismatch')
        require(self.evidence_cutoff.astimezone(ZoneInfo(self.race.election_timezone)).date() < self.race.election_date, 'Prior must be pre-election')
        validate_local_allocation(self.race, self.simulation, self.local_allocation, self.allocation_manifest, self.allocation_run)
        validate_dependency_graph(self)


@dataclass(frozen=True, kw_only=True)
class MaterializedCount(Contract):
    observation_id: str
    coverage: Coverage
    candidate_ids: tuple[str, ...]
    votes: tuple[int | None, ...]
    quantities: VoteQuantities
    reporting_status: Literal['NOT_REPORTED', 'PARTIAL', 'COMPLETE', 'UNKNOWN']
    completeness: tuple[CompletenessMetric, ...]

    def validate(self):
        _nonempty_unique(self.candidate_ids, 'materialized candidates')
        require(len(self.votes) == len(self.candidate_ids), 'Materialized vector mismatch')
        _validate_votes(self.votes, self.quantities)
        require(self.reporting_status != 'COMPLETE' or all(v is not None for v in self.votes), 'Missing complete counts')


def _materialize_count_history(race: RaceDefinition, observations: tuple[CountingObservation, ...],
                               applicable_evidence_ids: frozenset[str] | None = None,
                               checked_count_ids: frozenset[str] | None = None
                               ) -> tuple[tuple[MaterializedCount, ...], dict[str, tuple[str, ...]]]:
    """Replay all chronology; optionally restrict effective count checks to numerical consumers."""
    seen, latest, totals, numerical_bases = {}, {}, {}, {}
    known_timestamps, known_values = {}, {}
    quantity_fields = tuple(f.name for f in fields(VoteQuantities) if f.name != 'schema_version')
    candidate_count = len(race.candidates)
    previous_order = None
    for obs in observations:
        _validate_count_structure(race, obs)
        require(obs.evidence.quality in ('VERIFIED_OFFICIAL', 'SYNTHETIC', 'REJECTED'),
                'Official result provenance required')
        order = (*obs.evidence.receipt_key, obs.observation_id)
        require(previous_order is None or order > previous_order, 'Backwards/duplicate observation receipt order')
        previous_order = order
        require(obs.observation_id not in seen, 'Duplicate observation')
        stream = obs.coverage, obs.evidence.source_id
        previous = latest.get(stream)
        if previous is not None:
            require(obs.source_sequence > previous.source_sequence, 'Old/repeated source sequence')
        if obs.source_as_of is not None:
            require(stream not in known_timestamps or obs.source_as_of >= known_timestamps[stream],
                    'Old source snapshot')
            known_timestamps[stream] = obs.source_as_of
        if obs.semantics != 'CUMULATIVE':
            require(previous is not None and obs.base_observation_id == previous.observation_id,
                    'Base must be latest in same coverage/source')
        if obs.semantics == 'INCREMENTAL':
            base = totals[obs.base_observation_id]
            add = lambda a, b: None if a is None or b is None else a + b
            votes = tuple(add(a, b) for a, b in zip(base.votes, obs.votes))
            quantities = VoteQuantities(**{name: add(getattr(base.quantities, name), getattr(obs.quantities, name))
                                         for name in quantity_fields})
            numerical_bases[obs.observation_id] = (obs.base_observation_id,)
        else:
            votes, quantities = obs.votes, obs.quantities
            # A correction's base is chronology, not a numerical input to its replacement totals.
            numerical_bases[obs.observation_id] = ()
        values = (*votes, *(getattr(quantities, name) for name in quantity_fields))
        witness = known_values.get(stream, (None,) * len(values))
        if obs.semantics == 'CUMULATIVE' and (checked_count_ids is None or obs.observation_id in checked_count_ids):
            require(all(a is None or b is None or b >= a for a, b in zip(witness[:candidate_count], votes)),
                    'Decrease requires explicit CORRECTION')
            require(all(a is None or b is None or b >= a for a, b in
                        zip(witness[candidate_count:], values[candidate_count:])),
                    'Quantity decrease requires CORRECTION')
        if obs.semantics == 'CORRECTION':
            # Inactive/rejected corrections cannot authorize a later cumulative decrease.
            if obs.evidence.quality != 'REJECTED' and (applicable_evidence_ids is None or
                                                      obs.evidence.evidence_id in applicable_evidence_ids):
                known_values[stream] = tuple(old if new is None else new for old, new in zip(witness, values))
        else:
            known_values[stream] = tuple(old if new is None else new if old is None else max(old, new)
                                         for old, new in zip(witness, values))
        totals[obs.observation_id] = MaterializedCount(observation_id=obs.observation_id, coverage=obs.coverage,
            candidate_ids=obs.candidate_ids, votes=votes, quantities=quantities,
            reporting_status=obs.reporting_status, completeness=obs.completeness)
        latest[stream], seen[obs.observation_id] = obs, obs
    return tuple(totals[o.observation_id] for o in observations), numerical_bases


def materialize_counts(race: RaceDefinition, observations: tuple[CountingObservation, ...]) -> tuple[MaterializedCount, ...]:
    """Historical bookkeeping, NOT active eligibility. CountingState validates selection/ancestry."""
    return _materialize_count_history(race, observations)[0]


@dataclass(frozen=True, kw_only=True)
class ExcludedObservation(Contract):
    observation_id: str
    reason: Literal['SUPERSEDED', 'OVERLAPPING_REDUNDANT', 'SOURCE_CONFLICT']
    explanation: str


@dataclass(frozen=True, kw_only=True)
class ActivePartition(Contract):
    race_definition: RaceRef
    selected_observation_ids: tuple[str, ...]
    excluded: tuple[ExcludedObservation, ...]
    precedence: Literal['STATE_TOTAL', 'COUNTY_TOTALS', 'METHOD_DETAIL', 'EXPLICIT_RECONCILIATION']
    reconciliation: str
    reconciliation_evidence_ids: tuple[str, ...]
    require_complete_coverage: bool
    require_complete_counts: bool

    def validate(self):
        _nonempty_unique(self.selected_observation_ids, 'active observations')
        excluded = tuple(e.observation_id for e in self.excluded)
        require(len(set(excluded)) == len(excluded) and not set(excluded) & set(self.selected_observation_ids), 'Invalid exclusions')
        require(self.precedence != 'EXPLICIT_RECONCILIATION' or bool(self.reconciliation_evidence_ids), 'Reconciliation requires evidence')


@dataclass(frozen=True, kw_only=True)
class SourceReconciliation(Contract):
    """Independent authority's exact decision bytes, not producer precedence prose.

    The canonical document must be the raw payload of a selected independent
    evidence envelope. Source authentication remains the ingestion/evaluator's
    responsibility; contract labels alone cannot establish official provenance.
    Existing v2 object fields/encoding are unchanged.
    """
    race_definition: RaceRef
    selected_observation_hashes: tuple[str, ...]
    excluded_observation_hashes: tuple[str, ...]
    issued_at: datetime
    basis: str

    def validate(self):
        for hashes in (self.selected_observation_hashes, self.excluded_observation_hashes):
            _nonempty_unique(hashes, 'reconciliation observation hashes')
            for digest in hashes:
                _hash(digest)
        require(bool(self.basis.strip()), 'Independent decision basis required')


def count_exclusion_reasons(race, observations, evidence, selected_ids):
    """Derive applicability and redundancy; never consume caller exclusion labels.

    Redundancy needs matched known snapshots/accounting over identical disjoint
    cells, or a newer complete encompassing snapshot from the SAME source. Mere
    overlap, receipt order, matching partial values or source preference is not
    equivalence. Unprovable overlap requires independent reconciliation.
    """
    rows, bases = _materialize_count_history(race, observations)
    by_id = {o.observation_id:o for o in observations}
    totals = {r.observation_id:r for r in rows}
    latest = {(o.coverage,o.evidence.source_id):o.observation_id for o in observations}
    selected = tuple(by_id[i] for i in selected_ids)
    validate_partition(race, tuple(o.coverage for o in selected))
    def current(o):
        return latest[o.coverage,o.evidence.source_id] == o.observation_id and o.evidence.evidence_id in evidence.selected_ids
    require(all(current(o) for o in selected), 'Stale active total')
    active = tuple(o for o in observations if current(o))
    def cells(o):
        return coverage_cells(race,o.coverage)
    def applicable(group):
        consumed, pending = set(), [o.observation_id for o in group]
        while pending:
            oid = pending.pop()
            if oid in consumed:
                continue
            consumed.add(oid)
            if by_id[oid].evidence.evidence_id not in evidence.selected_ids:
                return False
            pending.extend(bases[oid])
        try:
            for oid in consumed:
                validate_count(race,by_id[oid])
            _materialize_count_history(race, observations, frozenset(evidence.selected_ids), frozenset(consumed))
        except ValueError:
            return False
        return True
    def summary(group, allow_missing=False):
        covered = set()
        for o in group:
            if o.coverage.method_status == 'UNKNOWN' or covered & cells(o):
                return None
            covered |= cells(o)
        if not group or not applicable(group):
            return None
        vectors = [(*totals[o.observation_id].votes,
            *(getattr(totals[o.observation_id].quantities,f.name) for f in fields(VoteQuantities)
              if f.name != 'schema_version')) for o in group]
        # None never becomes zero, nor proof of numerical agreement.
        values = tuple(None if any(v[i] is None for v in vectors) else sum(v[i] for v in vectors)
                       for i in range(len(vectors[0])))
        if not allow_missing and (any(v is None for v in values[:len(race.candidates)]) or any(
                totals[o.observation_id].quantities.valid_contest_votes is None for o in group)):
            return None
        timestamps = {o.source_as_of for o in group}
        if len(timestamps) != 1 or None in timestamps:
            return None
        return covered, values, next(iter(timestamps))
    def equivalent(left,right):
        a,b = summary(left),summary(right)
        if a is None or b is None or a != b:
            return False
        if len({o.evidence.origin for o in (*left,*right)}) != 1:
            return False
        if len({(o.reporting_status,o.certified) for o in (*left,*right)}) != 1:
            return False
        # Reporting-unit percentages cannot be added across different geometries.
        if len(left) == len(right) == 1 and cells(left[0]) == cells(right[0]):
            return left[0].completeness == right[0].completeness
        return all(not o.completeness for o in (*left,*right))
    reasons = {}
    for obs in observations:
        if obs.observation_id in selected_ids:
            continue
        if not current(obs):
            reasons[obs.observation_id] = 'SUPERSEDED'
            continue
        overlapping = tuple(o for o in selected if cells(o) & cells(obs))
        require(bool(overlapping), 'Current nonoverlapping coverage cannot be excluded')
        inside = tuple(o for o in selected if cells(o) <= cells(obs))
        redundant = equivalent((obs,),inside)
        for parent in overlapping:
            # A complete current county/method partition may corroborate an
            # encompassing selected total; no totals are independently added.
            siblings = tuple(o for o in active if o.observation_id not in selected_ids and cells(o) <= cells(parent))
            if obs in siblings and equivalent(siblings,(parent,)):
                redundant = True
            # Explicitly support a publisher's newer complete snapshot replacing
            # its older partial subset. Cross-source timestamp precedence is not
            # authorized by this rule.
            old,new = summary((obs,),allow_missing=True),summary((parent,))
            if old is not None and new is not None and cells(obs) <= cells(parent) and (
                    obs.evidence.source_id,obs.evidence.origin) == (parent.evidence.source_id,parent.evidence.origin) and (
                    parent.reporting_status == 'COMPLETE' and new[2] > old[2] and
                    (not obs.certified or parent.certified)) and all(
                    x is None or y is not None and x <= y for x,y in zip(old[1],new[1])):
                redundant = True
        reasons[obs.observation_id] = 'OVERLAPPING_REDUNDANT' if redundant else 'SOURCE_CONFLICT'
    return reasons


def validate_count_exclusions(race, observations, evidence, partition):
    reasons = count_exclusion_reasons(race,observations,evidence,partition.selected_observation_ids)
    supplied = {e.observation_id:e.reason for e in partition.excluded}
    require(supplied == reasons, 'Exclusion classification differs from derived source applicability/conflict')
    conflicts = tuple(o for o in observations if reasons.get(o.observation_id) == 'SOURCE_CONFLICT')
    if not conflicts:
        # Legacy explicit coverage choices may retain context refs. They provide
        # no authority to discard a derived conflict and cannot waive equivalence.
        require(set(partition.reconciliation_evidence_ids) <= set(evidence.selected_ids),
                'Reconciliation context evidence unavailable')
        return None
    require(partition.precedence == 'EXPLICIT_RECONCILIATION' and bool(partition.reconciliation_evidence_ids),
            'Multiple current sources require evidenced reconciliation')
    # A typed raw source decision is a narrow extension using the existing
    # reconciliation string/evidence refs; no existing v2 fields are added.
    try:
        decision = from_json(partition.reconciliation)
    except (ValueError,TypeError,KeyError) as exc:
        raise ValueError('Independent source reconciliation document required') from exc
    require(type(decision) is SourceReconciliation and canonical_json(decision) == partition.reconciliation,
            'Canonical independent source reconciliation document required')
    by_id = {o.observation_id:o for o in observations}
    selected = tuple(by_id[i] for i in partition.selected_observation_ids)
    require(decision.race_definition == race.ref and decision.selected_observation_hashes ==
            tuple(content_id(o) for o in selected) and decision.excluded_observation_hashes ==
            tuple(content_id(o) for o in conflicts), 'Reconciliation does not bind exact current reports')
    registry = {e.evidence_id:e for e in evidence.catalog}
    for eid in partition.reconciliation_evidence_ids:
        require(eid in evidence.selected_ids, 'Reconciliation evidence unavailable')
        e = registry[eid]
        require(e.raw_payload.sha256 == content_id(decision) and
                e.source_kind in ('OFFICIAL_RESULTS','OFFICIAL_RULES') and
                e.quality in ('VERIFIED_OFFICIAL','SYNTHETIC') and
                all(e.origin == o.evidence.origin for o in (*selected,*conflicts)) and
                all((e.source_id,e.source_object_id) != (o.evidence.source_id,o.evidence.source_object_id)
                    and e.raw_payload != o.evidence.raw_payload for o in observations) and
                max(o.evidence.received_at for o in (*selected,*conflicts)) <= decision.issued_at <= e.received_at,
                'Independent applicable reconciliation evidence required')
    return decision


@dataclass(frozen=True, kw_only=True)
class MaterializedCounts(Contract):
    race_definition: RaceRef
    rows: tuple[MaterializedCount, ...]

    def validate(self):
        _nonempty_unique(tuple(r.observation_id for r in self.rows), 'materialized rows')


@dataclass(frozen=True, kw_only=True)
class CountingState(Contract):
    state_id: str
    prior: ElectionPrior
    evidence_cutoff: datetime
    evidence: EvidenceSelection
    observations: tuple[CountingObservation, ...]
    partition: ActivePartition
    materialized_counts: MaterializedCounts
    previous_state: ArtifactRef | None = None

    def validate(self):
        race = self.prior.race
        require(self.prior.evidence_cutoff <= self.evidence_cutoff == self.evidence.cutoff, 'Counting cutoff mismatch')
        require(race.ref == self.partition.race_definition == self.materialized_counts.race_definition, 'Count partition definition mismatch')
        registry = {e.evidence_id: e for e in self.evidence.catalog}
        require(all(registry.get(o.evidence.evidence_id) == o.evidence for o in self.observations), 'Counting evidence not in catalog')
        # Audit replay uses each immutable report as received, before later revisions.
        rows, numerical_bases = _materialize_count_history(race, self.observations)
        by_id = {o.observation_id: o for o in self.observations}
        selected = self.partition.selected_observation_ids
        excluded = {e.observation_id: e for e in self.partition.excluded}
        require(set(selected) | excluded.keys() == by_id.keys(), 'Every observation needs explicit selection/exclusion')
        latest = {}
        for obs in self.observations:
            latest[obs.coverage, obs.evidence.source_id] = obs.observation_id
        for oid in selected:
            obs = by_id[oid]
            require(latest[obs.coverage, obs.evidence.source_id] == oid, 'Selected stale observation')
            require(obs.evidence.evidence_id in self.evidence.selected_ids, 'Selected superseded source revision')
        pending, consumed = list(selected), set()
        while pending:
            oid = pending.pop()
            if oid in consumed:
                continue
            consumed.add(oid)
            dependency = by_id[oid]
            require(dependency.evidence.evidence_id in self.evidence.selected_ids,
                    f'Inapplicable numerical dependency: {oid}')
            validate_count(race, dependency)
            pending.extend(numerical_bases[oid])
        # Current applicability must hold for active consumers, not obsolete audit rows.
        # All reports still contribute witnesses, and the audit pass above checks every row.
        _materialize_count_history(race, self.observations,
                                   frozenset(self.evidence.selected_ids), frozenset(consumed))
        for oid, exclusion in excluded.items():
            obs = by_id[oid]
            require(exclusion.reason != 'SUPERSEDED' or latest[obs.coverage, obs.evidence.source_id] != oid
                    or obs.evidence.evidence_id not in self.evidence.selected_ids, 'False superseded exclusion')
        validate_partition(race, tuple(by_id[i].coverage for i in selected), self.partition.require_complete_coverage)
        require(self.materialized_counts.rows == tuple(next(r for r in rows if r.observation_id == i) for i in selected), 'Materialized counts differ from source semantics')
        if self.partition.require_complete_counts:
            require(all(r.reporting_status == 'COMPLETE' and all(v is not None for v in r.votes)
                        for r in self.materialized_counts.rows), 'Incomplete counts forbidden by partition')
        require(set(self.partition.reconciliation_evidence_ids) <= set(self.evidence.selected_ids), 'Unknown reconciliation evidence')
        validate_count_exclusions(race,self.observations,self.evidence,self.partition)
        selected_coverages = tuple(by_id[i].coverage for i in selected)
        if self.partition.precedence == 'STATE_TOTAL':
            require(all(c.unit_id == f'US:{race.state}' and c.ballot_method == 'ALL' for c in selected_coverages), 'State precedence mismatch')
        if self.partition.precedence == 'COUNTY_TOTALS':
            require(all(c.unit_id != f'US:{race.state}' and c.ballot_method == 'ALL' for c in selected_coverages), 'County precedence mismatch')
        if self.partition.precedence == 'METHOD_DETAIL':
            require(all(c.method_status != 'UNKNOWN' and c.ballot_method != 'ALL' for c in selected_coverages), 'Method precedence mismatch')
        validate_dependency_graph(self)


@dataclass(frozen=True, kw_only=True)
class ForecastRecord(Contract):
    forecast_id: str
    created_at: datetime
    evidence_cutoff: datetime
    prior: ElectionPrior
    counting_state: CountingState | None
    models: tuple[ModelArtifact, ...]
    data_version: str
    run: RunManifest
    output_draws: tuple[JointDraw, ...]
    origin: Literal['REAL', 'SYNTHETIC']
    quality: Literal['SYNTHETIC', 'EXPERIMENTAL', 'EVALUATED', 'DEGRADED', 'WITHHELD']
    eligibility: Literal['RESEARCH_ONLY', 'EVALUATED_FORECAST', 'WITHHELD']
    quality_reasons: tuple[str, ...]

    @property
    def code(self):
        return self.run.code

    @property
    def environment(self):
        return self.run.environment

    @property
    def configuration(self):
        return self.run.configuration

    @property
    def random_seed(self):
        return self.run.rng.seed

    @property
    def evidence_manifest(self):
        return self.run.inputs

    def validate(self):
        require(self.prior.evidence_cutoff <= self.evidence_cutoff <= self.created_at == self.run.created_at, 'Invalid forecast times')
        require(self.run.inputs.evidence.cutoff == self.evidence_cutoff and self.run.race_definition == self.prior.race.ref, 'Forecast run/input mismatch')
        require(self.data_version == self.run.inputs.data_version and self.models == self.run.fitted_models, 'Forecast model/data versions disagree')
        require(all(m in self.models for m in (self.prior.model, self.prior.local_allocation_model)), 'Missing prior models')
        require(any(i.kind == 'PRIOR' and i.artifact.sha256 == content_id(self.prior) for i in self.run.inputs.ordered_inputs), 'Missing exact prior input')
        if self.counting_state is not None:
            require(self.counting_state.prior == self.prior and self.counting_state.evidence_cutoff == self.evidence_cutoff, 'Counting handoff mismatch')
            require(self.run.role == 'COUNTING' and any(m.role == 'COUNTING' for m in self.models), 'Missing counting model/run')
            require(any(i.kind == 'COUNTS' and i.artifact.sha256 == content_id(self.counting_state) for i in self.run.inputs.ordered_inputs), 'Missing exact counting state input')
        else:
            require(self.run.role == 'STATEWIDE_PRIOR', 'Wrong pre-election run role')
        require(self.quality not in ('DEGRADED', 'WITHHELD', 'SYNTHETIC') or bool(self.quality_reasons) and all(r.strip() for r in self.quality_reasons), 'Quality reasons required')
        expected_eligibility = 'WITHHELD' if self.quality == 'WITHHELD' else 'EVALUATED_FORECAST' if self.quality == 'EVALUATED' else 'RESEARCH_ONLY'
        require(self.eligibility == expected_eligibility, 'Quality/eligibility mismatch')
        require(self.origin != 'SYNTHETIC' or self.quality in ('SYNTHETIC', 'WITHHELD'), 'Synthetic forecast quality')
        require(self.quality != 'SYNTHETIC' or self.origin == 'SYNTHETIC', 'Synthetic quality/origin mismatch')
        require((bool(self.output_draws) or self.quality == 'WITHHELD') and all(len(d.shares) == len(self.prior.race.candidates) for d in self.output_draws), 'Invalid forecast draws')
        validate_dependency_graph(self)
        if self.quality == 'EVALUATED':
            require(all(m.status == 'HISTORICALLY_EVALUATED' for m in self.models), 'Experimental model')
            require(all(e.quality in ('VERIFIED_OFFICIAL', 'VERIFIED_NONOFFICIAL') for e in _walk(self) if type(e) is EvidenceEnvelope), 'Unverified evaluation evidence')


def _walk(value):
    if isinstance(value, Contract):
        yield value
        for f in fields(value):
            yield from _walk(getattr(value, f.name))
    elif isinstance(value, tuple):
        for item in value:
            yield from _walk(item)


def dependency_evidence(root: Contract) -> tuple[EvidenceEnvelope, ...]:
    """Full deduplicated, ordered source lineage, including both prior models/runs."""
    registry = _evidence_registry(o for o in _walk(root) if type(o) is EvidenceEnvelope)
    return tuple(sorted(registry.values(), key=lambda e: e.receipt_key))


def validate_dependency_graph(root: Contract | tuple[Contract, ...]) -> None:
    """Validate identities, revision ancestry, availability, and synthetic taint end-to-end."""
    objects = tuple(_walk(root))
    registry = _evidence_registry(o for o in objects if type(o) is EvidenceEnvelope)
    identities = {}
    for o in objects:
        if type(o) is RaceDefinition:
            identity = 'RaceDefinition', o.race_id, o.revision
            require(identity not in identities or identities[identity] == content_id(o), 'Conflicting race-definition revision')
            identities[identity] = content_id(o)
        for key in ('model_id', 'run_id', 'prior_id', 'state_id', 'forecast_id', 'poll_id', 'observation_id', 'allocation_id'):
            if hasattr(o, key) and type(o) is not MaterializedCount:
                identity = type(o).__name__, getattr(o, key)
                digest = content_id(o)
                require(identity not in identities or identities[identity] == digest, 'Conflicting dependency identity')
                identities[identity] = digest
    for selected in (o for o in objects if type(o) is EvidenceSelection):
        scope = {(e.source_id, e.source_object_id) for e in selected.catalog}
        known_ids = {e.evidence_id for e in registry.values()
                     if (e.source_id, e.source_object_id) in scope and e.received_at <= selected.cutoff}
        require(known_ids <= {e.evidence_id for e in selected.catalog}, 'Selection omits known applicable revision')
    for race in (o for o in objects if type(o) is RaceDefinition):
        for o in objects:
            if type(o) is RunManifest and o.race_definition.race_id == race.race_id:
                require(o.race_definition == race.ref, 'Dependency race revision mismatch')
        for manifest in (o for o in objects if type(o) is InputManifest and o.race_definition == race.ref):
            for e in (race.rules_evidence, race.definition_evidence):
                require(e.evidence_id in manifest.evidence.selected_ids and e.received_at <= manifest.evidence.cutoff,
                        'Race rules/roster unavailable or superseded at input cutoff')
    if type(root) in (ElectionPrior, CountingState, ForecastRecord):
        cutoff = root.evidence_cutoff
        require(all(e.received_at <= cutoff for e in registry.values()), 'Dependency evidence after cutoff')
    if type(root) is ForecastRecord:
        synthetic = any(e.origin == 'SYNTHETIC' for e in registry.values()) or any(
            o.origin == 'SYNTHETIC' for o in objects if type(o) is ModelArtifact)
        require(not synthetic or root.origin == 'SYNTHETIC', 'Synthetic dependency cannot become real forecast')
        run_registry = {e.evidence_id: e for e in root.run.inputs.evidence.catalog}
        require(all(run_registry.get(k) == e for k, e in registry.items()), 'Forecast run omits dependency evidence')


def canonical_json(value: Contract) -> str:
    """Canonical v2 JSON; UTC microseconds, signed float zero normalized to +0.0."""
    require(isinstance(value, Contract), 'Canonical root must be a contract')
    def encode(v):
        if is_dataclass(v):
            return {'type': type(v).__name__, **{f.name: encode(getattr(v, f.name)) for f in fields(v)}}
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).isoformat(timespec='microseconds')
        if isinstance(v, date):
            return v.isoformat()
        if isinstance(v, tuple):
            return [encode(x) for x in v]
        if type(v) is float and v == 0:
            return 0.0
        return v
    return json.dumps(encode(value), sort_keys=True, separators=(',', ':'), allow_nan=False, ensure_ascii=True)


def content_id(value: Contract) -> str:
    return sha256(canonical_json(value).encode('utf-8')).hexdigest()


def artifact_ref(value: Contract) -> ArtifactRef:
    digest = content_id(value)
    return ArtifactRef(uri=f'sha256:{digest}', sha256=digest)


def verify_artifact(reference: ArtifactRef, raw: bytes) -> None:
    """Verify retrieved bytes before parsing; this function performs no retrieval."""
    require(type(raw) is bytes and sha256(raw).hexdigest() == reference.sha256, 'Artifact hash mismatch')


def from_json(payload: str) -> Contract:
    """Strict v2 reader; no implicit defaults, duplicate keys, root coercion, or migration."""
    registry = {c.__name__: c for c in Contract.__subclasses__()}
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    def decode(v):
        if isinstance(v, list):
            return tuple(decode(x) for x in v)
        if not isinstance(v, dict):
            return v
        data = dict(v)
        name = data.pop('type', None)
        require(type(name) is str and name in registry, 'Unknown/missing contract type')
        cls = registry[name]
        require(type(data.get('schema_version')) is int and data['schema_version'] == SCHEMA_VERSION, 'Explicit v2 schema required')
        hints = get_type_hints(cls)
        require(data.keys() == hints.keys(), 'Missing/unknown contract fields')
        for key, item in data.items():
            annotation = hints[key]
            if annotation is datetime or get_origin(annotation) is UnionType and datetime in get_args(annotation):
                data[key] = None if item is None else datetime.fromisoformat(item)
            elif annotation is date:
                data[key] = date.fromisoformat(item)
            else:
                data[key] = decode(item)
        return cls(**data)
    def bad_constant(value):
        raise ValueError(f'Nonfinite JSON constant: {value}')
    value = decode(json.loads(payload, object_pairs_hook=pairs, parse_constant=bad_constant))
    require(isinstance(value, Contract), 'JSON root must be a contract')
    return value
