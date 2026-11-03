"""Synthetic cross-worker artifacts. Bytes/hashes are real; election data are invented fixtures."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from forecasting.contracts import (
    ArtifactRef, EvidenceEnvelope, EvidenceSelection, Candidate, ReportingUnit, RaceDefinition,
    PollObservation, CountingObservation, Coverage, VoteQuantities, CompletenessMetric,
    InputArtifact, InputManifest, ModelArtifact, RNGSpec, RNGStream, RunManifest,
    JointDraw, JointSimulation, LocalDraw, LocalAllocationPayload, LocalAllocationManifest,
    ElectionPrior, ActivePartition, MaterializedCounts, CountingState, ForecastRecord,
    canonical_json, content_id, artifact_ref, materialize_counts,
)

ASSET_BYTES = {}
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def asset(name):
    raw = f'SYNTHETIC TEST ASSET: {name}'.encode()
    digest = sha256(raw).hexdigest()
    ASSET_BYTES[digest] = raw
    return ArtifactRef(uri=f'fixture://{name}', sha256=digest)


def store(value):
    raw = canonical_json(value).encode()
    ref = artifact_ref(value)
    ASSET_BYTES[ref.sha256] = raw
    return ref


def evidence(name, kind, order, at=T0, revision=0, parent=None, object_id=None):
    return EvidenceEnvelope(evidence_id=f'fixture:{name}', source_id=f'fixture:{kind}',
        source_object_id=object_id or name, source_url=f'fixture://{name}', source_kind=kind,
        origin='SYNTHETIC', quality='SYNTHETIC', published_at=at, received_at=at,
        receipt_order=order, raw_payload=asset(f'raw-{name}'), revision=revision, supersedes_id=parent)


def make_race(office='US_SENATE', special=False):
    state = 'IN' if office == 'US_SENATE' else 'OH'
    seat = 'CLASS3' if office == 'US_SENATE' else 'GOVERNOR'
    contest = 'SPECIAL' if special else 'REGULAR'
    term_end = date(2031, 1, 3) if office == 'US_SENATE' else date(2031, 1, 13)
    race_id = f'US:{state}:2026-11-03:{office}:{seat}:{contest}'
    if special:
        race_id += f':TERM_END:{term_end.isoformat()}'
    prefix = '18' if state == 'IN' else '39'
    return RaceDefinition(race_id=race_id, revision=0, previous_definition_hash=None,
        state=state, office=office, election_date=date(2026, 11, 3), election_timezone='America/New_York',
        seat=seat, term_end=term_end, contest=contest, rules='SINGLE_SEAT_PLURALITY',
        rules_evidence=evidence(f'{state}-rules', 'OFFICIAL_RULES', 0),
        definition_evidence=evidence(f'{state}-roster', 'OFFICIAL_ROSTER', 1),
        candidates=tuple(Candidate(candidate_id=f'fixture:{state}:{i}', name=f'Example {i}',
                                   party=f'EXAMPLE_PARTY_{i}', winner_eligibility='ELIGIBLE') for i in range(3)),
        reporting_units=(ReportingUnit(unit_id=f'US:{state}', state=state, level='STATE'),
            *(ReportingUnit(unit_id=f'US:{state}:{prefix}{suffix}', state=state, level='COUNTY',
                            parent_id=f'US:{state}') for suffix in ('001', '003'))),
        county_partition_complete=True, ballot_methods=('IN_PERSON', 'MAIL'))


def selection(catalog, cutoff):
    unique = {}
    for e in catalog:
        assert e.evidence_id not in unique or unique[e.evidence_id] == e
        unique[e.evidence_id] = e
    catalog = tuple(sorted(unique.values(), key=lambda e: e.receipt_key))
    latest = {(e.source_id, e.source_object_id): e for e in catalog}
    selected = tuple(e.evidence_id for e in catalog
                     if latest[e.source_id, e.source_object_id] == e and e.quality != 'REJECTED')
    return EvidenceSelection(cutoff=cutoff, catalog=catalog, selected_ids=selected)


def inputs(race, catalog, cutoff=T0, extra=(), training=False):
    selected = selection((race.rules_evidence, race.definition_evidence, *catalog), cutoff)
    raw = tuple(InputArtifact(artifact=e.raw_payload, kind='RAW' if e.evidence_id in selected.selected_ids else 'LINEAGE',
        version=f'raw-revision:{e.revision}', available_at=e.received_at,
        source_evidence_ids=(e.evidence_id,)) for e in selected.catalog)
    definition = InputArtifact(artifact=store(race), kind='RACE_DEFINITION', version=f'definition:{race.revision}',
        available_at=max(race.rules_evidence.received_at, race.definition_evidence.received_at),
        source_evidence_ids=(race.rules_evidence.evidence_id, race.definition_evidence.evidence_id))
    return InputManifest(race_definition=race.ref, evidence=selected,
        ordered_inputs=(*raw, definition, *extra), data_version='synthetic-fixture-v2',
        training_cutoff=cutoff if training else None)


def model(race, role):
    at = max(T0, race.definition_evidence.received_at, race.rules_evidence.received_at)
    return ModelArtifact(model_id=f'fixture:{race.race_id}:definition:{race.revision}:{role}', role=role, origin='SYNTHETIC',
        status='EXPERIMENTAL', implementation=asset(f'{role}-implementation'), parameters=asset(f'{role}-parameters'),
        feature_version=asset(f'{role}-features-v1'), transformation_version=asset(f'{role}-transform-v1'),
        fitted_at=at, training_manifest=inputs(race, (), cutoff=at, training=True))


def run(race, role, models, manifest, name=None):
    return RunManifest(run_id=f'fixture:{race.race_id}:definition:{race.revision}:{name or role}', role=role,
        race_definition=race.ref, inputs=manifest, fitted_models=models,
        created_at=manifest.evidence.cutoff + timedelta(seconds=1), code=asset(f'{role}-code'),
        environment=asset('python-environment-lock'),
        rng=RNGSpec(algorithm='FIXTURE_ENUMERATED_DRAWS', implementation_version='1', stream_derivation=asset('fixture-stream-derivation-v1'), seed=7,
            streams=(RNGStream(name='statewide', spawn_key=(0,)), RNGStream(name='residual', spawn_key=(1,)))),
        sampling_procedure=asset('fixture-enumeration-procedure-v1'), configuration=asset(f'{role}-config'),
        numeric_tolerance=1e-9)


def known_quantities(votes):
    total = sum(votes)
    return VoteQuantities(ballots_cast=total + 2, valid_contest_votes=total,
                          undervotes=1, overvotes=1, invalid_contest_ballots=0)


def fixture(office='US_SENATE', special=False, revised=False):
    race = make_race(office, special)
    if revised:
        race = revised_race(race)
    asof = max(T0, race.definition_evidence.received_at)
    e_poll = evidence(f'{race.state}-poll', 'POLL', 2, asof)
    ids = tuple(c.candidate_id for c in race.candidates)
    poll = PollObservation(poll_id=f'fixture:{race.state}:poll', race_id=race.race_id, race_definition=race.ref,
        evidence=e_poll, field_start=date(2025, 12, 30), field_end=date(2025, 12, 31),
        field_timezone='America/New_York', sample_size=100, population='LV', candidate_ids=ids,
        shares=(0.0, 0.3, 0.2, 0.3) if revised else (0.3, 0.3, 0.2), undecided_share=0.1, other_share=0.1)
    poll_input = InputArtifact(artifact=store(poll), kind='POLL', version='poll-v2', available_at=asof,
                               source_evidence_ids=(e_poll.evidence_id,))
    prior_model, local_model, counting_model = (model(race, role) for role in ('STATEWIDE_PRIOR', 'LOCAL_ALLOCATION', 'COUNTING'))
    prior_run = run(race, 'STATEWIDE_PRIOR', (prior_model,), inputs(race, (e_poll,), cutoff=asof, extra=(poll_input,)))
    draws = (JointDraw(shares=(0.4, 0.35, 0.25), total_valid_votes=100),
             JointDraw(shares=(0.5, 0.25, 0.25), total_valid_votes=120))
    if revised:
        draws = tuple(replace(d, shares=(0.0, d.shares[1], d.shares[2], d.shares[0])) for d in draws)
    simulation = JointSimulation(race_definition=race.ref, candidate_ids=ids, draws=draws)
    joint_input = InputArtifact(artifact=store(simulation), kind='JOINT_DRAWS', version='joint-v2',
                                available_at=asof, source_evidence_ids=(e_poll.evidence_id,))
    allocation_run = run(race, 'LOCAL_ALLOCATION', (local_model,), inputs(race, (e_poll,), cutoff=asof, extra=(joint_input,)))
    matrices = (((12, 8, 5), (8, 7, 5), (12, 12, 5), (8, 8, 10)),
                ((10, 10, 5), (10, 5, 5), (10, 10, 10), (10, 10, 5)),
                ((20, 10, 10), (10, 5, 5), (20, 10, 10), (10, 5, 5)))
    if revised:
        matrices = tuple(tuple((0, row[1], row[2], row[0]) for row in matrix) for matrix in matrices)
    local_draws = tuple(LocalDraw(statewide_draw_index=0 if i < 2 else 1,
        residual_draw_index=i if i < 2 else 0, conditional_weight=0.5 if i < 2 else 1.0,
        cell_votes=matrix, cell_quantities=tuple(known_quantities(row) for row in matrix))
        for i, matrix in enumerate(matrices))
    payload = LocalAllocationPayload(race_definition=race.ref,
        unit_ids=tuple(u.unit_id for u in race.reporting_units if u.level == 'COUNTY'),
        ballot_methods=race.ballot_methods, candidate_ids=ids, draws=local_draws)
    allocation_manifest = LocalAllocationManifest(allocation_id=f'fixture:{race.state}:definition:{race.revision}:allocation',
        race_definition=race.ref, payload=store(payload), statewide_simulation=store(simulation),
        model_hash=content_id(local_model), run_hash=content_id(allocation_run), local_evaluation_report=None)
    prior = ElectionPrior(prior_id=f'fixture:{race.state}:definition:{race.revision}:prior', race=race, evidence_cutoff=asof,
        model=prior_model, local_allocation_model=local_model, draws=draws, local_allocation=payload,
        allocation_manifest=allocation_manifest, run=prior_run, allocation_run=allocation_run)
    at = datetime(2026, 11, 4, 1, tzinfo=timezone.utc)
    e_count = evidence(f'{race.state}-count', 'OFFICIAL_RESULTS', 3, at)
    obs = CountingObservation(observation_id=f'fixture:{race.state}:count', race_id=race.race_id,
        race_definition=race.ref, evidence=e_count, coverage=Coverage(unit_id=payload.unit_ids[0], ballot_method='ALL'),
        candidate_ids=ids, votes=(0, None, 0, 4) if revised else (4, None, 0), quantities=VoteQuantities(ballots_cast=10),
        reporting_status='PARTIAL', completeness=(CompletenessMetric(denominator_name='REPORTING_UNITS',
            numerator=1, denominator=3, basis='EXACT'),), source_sequence=1, source_as_of=at,
        semantics='CUMULATIVE')
    partition = ActivePartition(race_definition=race.ref, selected_observation_ids=(obs.observation_id,), excluded=(),
        precedence='COUNTY_TOTALS', reconciliation='Only the first partial county is available; no statewide total implied.',
        reconciliation_evidence_ids=(), require_complete_coverage=False, require_complete_counts=False)
    catalog = (*prior.evidence, e_count)
    state = CountingState(state_id=f'fixture:{race.state}:definition:{race.revision}:state', prior=prior, evidence_cutoff=at,
        evidence=selection(catalog, at), observations=(obs,), partition=partition,
        materialized_counts=MaterializedCounts(race_definition=race.ref, rows=materialize_counts(race, (obs,))))
    prior_input = InputArtifact(artifact=store(prior), kind='PRIOR', version='prior-v2', available_at=asof,
                                source_evidence_ids=(e_poll.evidence_id,))
    count_input = InputArtifact(artifact=store(state), kind='COUNTS', version='counting-state-v2', available_at=at,
                                source_evidence_ids=(e_count.evidence_id,))
    forecast_run = run(race, 'COUNTING', (prior_model, local_model, counting_model),
                       inputs(race, catalog, cutoff=at, extra=(prior_input, count_input)))
    forecast = ForecastRecord(forecast_id=f'fixture:{race.state}:definition:{race.revision}:forecast', created_at=forecast_run.created_at,
        evidence_cutoff=at, prior=prior, counting_state=state, models=forecast_run.fitted_models,
        data_version=forecast_run.inputs.data_version, run=forecast_run, output_draws=draws,
        origin='SYNTHETIC', quality='SYNTHETIC', eligibility='RESEARCH_ONLY',
        quality_reasons=('Synthetic fixture; outputs are not a fitted forecast.',))
    return race, e_count, poll, obs, prior, state, forecast


def revised_race(previous):
    replacement = Candidate(candidate_id=f'fixture:{previous.state}:replacement', name='Replacement', party='EXAMPLE_PARTY_0', winner_eligibility='ELIGIBLE')
    old = replace(previous.candidates[0], status='REPLACED', replaced_by=replacement.candidate_id, winner_eligibility='INELIGIBLE')
    return replace(previous, revision=previous.revision + 1, previous_definition_hash=content_id(previous),
        definition_evidence=evidence(f'{previous.state}-roster-revision-1', 'OFFICIAL_ROSTER', 10, T0 + timedelta(hours=1)),
        candidates=(old, *previous.candidates[1:], replacement))
