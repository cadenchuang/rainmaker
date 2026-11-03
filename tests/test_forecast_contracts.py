"""Contract regressions: synthetic A -> B -> C -> D exchanges, no forecast algorithms."""
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest
from forecasting.contracts import (
    EvidenceSelection, VoteQuantities, CompletenessMetric, Coverage,
    ExcludedObservation, MaterializedCounts, LocalDraw,
    RNGStream, JointDraw,
    canonical_json, content_id, from_json, materialize_counts, validate_partition,
    validate_poll, validate_count, validate_race_revision, validate_local_allocation,
    validate_dependency_graph, verify_artifact, _walk,
)
from forecast_fixtures import (
    ASSET_BYTES, T0, asset, store, evidence, fixture, make_race, revised_race,
    selection, inputs, known_quantities,
)


def advance(obs, *, semantics='CUMULATIVE', votes=(5, None, 0), revision=False, suffix='next'):
    at = obs.evidence.received_at + timedelta(seconds=1)
    e = replace(obs.evidence, evidence_id=f'{obs.evidence.evidence_id}:{suffix}',
        source_object_id=obs.evidence.source_object_id if revision else f'{obs.evidence.source_object_id}:{suffix}',
        published_at=at, received_at=at, receipt_order=obs.evidence.receipt_order + 1,
        raw_payload=asset(f'raw-{suffix}'), revision=obs.evidence.revision + 1 if revision else 0,
        supersedes_id=obs.evidence.evidence_id if revision else None)
    return replace(obs, observation_id=f'{obs.observation_id}:{suffix}', evidence=e,
        votes=votes, source_sequence=obs.source_sequence + 1, source_as_of=at, semantics=semantics,
        quantities=VoteQuantities(), base_observation_id=obs.observation_id if semantics != 'CUMULATIVE' else None)


def state_for(case, observations, selected=None, **partition_changes):
    race, _, _, _, prior, state, _ = case
    selected = selected or (observations[-1].observation_id,)
    cutoff = max(o.evidence.received_at for o in observations)
    sel = selection((*prior.evidence, *(o.evidence for o in observations)), cutoff)
    excluded = tuple(ExcludedObservation(observation_id=o.observation_id, reason='SUPERSEDED',
        explanation='Replaced by later source total for this coverage.')
        for o in observations if o.observation_id not in selected)
    partition = replace(state.partition, selected_observation_ids=selected, excluded=excluded, **partition_changes)
    all_rows = materialize_counts(race, observations)
    rows = tuple(next(r for r in all_rows if r.observation_id == i) for i in selected)
    return replace(state, evidence_cutoff=cutoff, evidence=sel, observations=observations,
        partition=partition, materialized_counts=MaterializedCounts(race_definition=race.ref, rows=rows))


@pytest.mark.parametrize('office,special', [('US_SENATE', False), ('GOVERNOR', False), ('US_SENATE', True), ('GOVERNOR', True)])
def test_cross_worker_roundtrip(office, special):
    case = fixture(office, special)
    race, _, poll, obs, prior, state, forecast = case
    validate_poll(race, poll, prior.evidence_cutoff)
    validate_count(race, obs)
    validate_local_allocation(race, prior.simulation, prior.local_allocation, prior.allocation_manifest, prior.allocation_run)
    for obj in case:
        assert from_json(canonical_json(obj)) == obj
        assert content_id(from_json(canonical_json(obj))) == content_id(obj)
        with pytest.raises(FrozenInstanceError):
            obj.schema_version = 99
    assert state.materialized_counts.rows[0].votes == (4, None, 0)
    assert forecast.origin == 'SYNTHETIC' and forecast.eligibility == 'RESEARCH_ONLY'
    assert len({c.party for c in race.candidates}) == 3


def test_distinct_valid_artifact_hashes_and_end_to_end_fixture_replay():
    forecast = fixture()[-1]
    refs = [o for o in _walk(forecast) if type(o).__name__ == 'ArtifactRef']
    assert len({r.sha256 for r in refs}) > 15
    for ref in refs:
        verify_artifact(ref, ASSET_BYTES[ref.sha256])
    restored = from_json(canonical_json(forecast))
    state = restored.counting_state
    assert materialize_counts(restored.prior.race, state.observations) == state.materialized_counts.rows
    assert canonical_json(fixture()[-1]) == canonical_json(forecast)
    assert restored.run.rng.streams[1].spawn_key == (1,)


@pytest.mark.parametrize('changes', [dict(office='PRESIDENT'), dict(rules='YES_NO'), dict(rules='RANKED_CHOICE'),
    dict(state='DC'), dict(candidates=()), dict(ballot_methods=['MAIL']), dict(seat='CLASS2'),
    dict(race_id='arbitrary'), dict(election_date=date(2026, 11, 4))])
def test_ineligible_or_unstable_races_rejected(changes):
    with pytest.raises(ValueError):
        replace(make_race(), **changes)


def test_roster_revisions_keep_identity_and_columns_but_reject_old_observations():
    race, _, poll, obs, prior, _, _ = fixture()
    revised = revised_race(race)
    validate_race_revision(race, revised)
    assert race.race_id == revised.race_id and race.ref != revised.ref
    assert revised.candidates[0].status == 'REPLACED'
    for fn, o in ((validate_count, obs), (validate_poll, poll)):
        with pytest.raises(ValueError, match='revision'):
            fn(revised, o)
    with pytest.raises(ValueError, match='historical candidate columns'):
        validate_race_revision(race, replace(revised, candidates=revised.candidates[::-1]))
    changed_rules = replace(race, rules_evidence=replace(race.rules_evidence, raw_payload=asset('new-rules')))
    assert changed_rules.ref != race.ref
    with pytest.raises(ValueError):
        validate_count(changed_rules, obs)
    with pytest.raises(ValueError):
        replace(prior, race=revised)
    with pytest.raises(ValueError):
        validate_race_revision(race, replace(revised, previous_definition_hash='0' * 64))


def test_withdrawals_and_special_seats():
    race = make_race(special=True)
    withdrawn = replace(race, revision=1, previous_definition_hash=content_id(race),
        candidates=(replace(race.candidates[0], status='WITHDRAWN'), *race.candidates[1:]))
    validate_race_revision(race, withdrawn)
    assert 'TERM_END:2031-01-03' in race.race_id
    with pytest.raises(ValueError):
        replace(race, term_end=date(2033, 1, 3))


def test_counties_and_geographic_method_partitions():
    race = make_race()
    county = race.reporting_units[1].unit_id
    state_all = Coverage(unit_id='US:IN', ballot_method='ALL')
    county_all = Coverage(unit_id=county, ballot_method='ALL')
    mail = Coverage(unit_id=county, ballot_method='MAIL')
    for coverage in ((state_all, county_all), (county_all, mail), (state_all, mail)):
        with pytest.raises(ValueError, match='Overlapping'):
            validate_partition(race, coverage)
    validate_partition(race, (state_all,), True)
    validate_partition(race, tuple(Coverage(unit_id=u.unit_id, ballot_method=m)
        for u in race.reporting_units if u.level == 'COUNTY' for m in race.ballot_methods), True)
    with pytest.raises(ValueError, match='Incomplete'):
        validate_partition(race, (county_all,), True)
    with pytest.raises(ValueError):
        replace(race.reporting_units[1], parent_id='US:OH')
    with pytest.raises(ValueError):
        replace(race.reporting_units[1], unit_id='US:OH:39001')
    with pytest.raises(ValueError):
        validate_partition(race, (Coverage(unit_id='US:IN:18999', ballot_method='ALL'),))
    with pytest.raises(ValueError):
        validate_partition(replace(race, county_partition_complete=False),
            tuple(Coverage(unit_id=u.unit_id, ballot_method='ALL') for u in race.reporting_units[1:]), True)


def test_unknown_and_mixed_methods_do_not_invent_partitions():
    race = make_race()
    county = race.reporting_units[1].unit_id
    unknown = Coverage(unit_id=county, ballot_method='UNKNOWN', method_status='UNKNOWN')
    mixed = Coverage(unit_id=county, ballot_method='MIXED', method_status='MIXED', mixed_methods=('IN_PERSON', 'MAIL'))
    validate_partition(race, (unknown,))
    validate_partition(race, (mixed,))
    with pytest.raises(ValueError, match='Ambiguous'):
        validate_partition(race, (unknown,), True)
    with pytest.raises(ValueError, match='Overlapping'):
        validate_partition(race, (unknown, Coverage(unit_id=county, ballot_method='MAIL')))
    with pytest.raises(ValueError):
        validate_partition(race, (replace(mixed, mixed_methods=('MAIL', 'UNRECOGNIZED')),))


def test_missing_zero_and_reporting_status_preserved():
    case = fixture()
    obs, state = case[3], case[5]
    assert state.materialized_counts.rows[0].votes == (4, None, 0)
    with pytest.raises(ValueError, match='missing'):
        replace(obs, reporting_status='COMPLETE')
    with pytest.raises(ValueError):
        replace(state, partition=replace(state.partition, require_complete_counts=True))
    with pytest.raises(ValueError):
        replace(state, materialized_counts=replace(state.materialized_counts,
            rows=(replace(state.materialized_counts.rows[0], votes=(4, 0, 0)),)))
    unreported = replace(obs, votes=(None, None, None), reporting_status='NOT_REPORTED', quantities=VoteQuantities())
    assert state_for(case, (unreported,)).materialized_counts.rows[0].votes == (None, None, None)


@pytest.mark.parametrize('votes', [(-1, None, 0), (True, None, 0), (1.0, None, 0)])
def test_invalid_counts(votes):
    with pytest.raises(ValueError):
        replace(fixture()[3], votes=votes)


def test_ballot_accounting_and_named_completeness():
    q = VoteQuantities(ballots_cast=110, valid_contest_votes=100, undervotes=7, overvotes=2, invalid_contest_ballots=1)
    assert q.valid_contest_votes != q.ballots_cast
    for changes in (dict(ballots_cast=109), dict(undervotes=-1), dict(ballots_cast=True)):
        with pytest.raises(ValueError):
            replace(q, **changes)
    obs = fixture()[3]
    with pytest.raises(ValueError):
        replace(obs, votes=(4, 2, 0), quantities=VoteQuantities(valid_contest_votes=5))
    with pytest.raises(ValueError):
        replace(obs, completeness=(CompletenessMetric(denominator_name='REPORTING_UNITS', numerator=4, denominator=3, basis='EXACT'),))
    assert CompletenessMetric(denominator_name='EXPECTED_BALLOTS', numerator=None, denominator=None, basis='ESTIMATED').numerator is None


def test_cumulative_incremental_and_correction_replay():
    case = fixture()
    first = replace(case[3], votes=(4, 3, 0), quantities=known_quantities((4, 3, 0)))
    inc1 = advance(first, semantics='INCREMENTAL', votes=(1, 2, 0), suffix='inc1')
    inc1 = replace(inc1, quantities=VoteQuantities())
    inc2 = advance(inc1, semantics='INCREMENTAL', votes=(2, 0, 1), suffix='inc2')
    rows = materialize_counts(case[0], (first, inc1, inc2))
    assert rows[-1].votes == (7, 5, 1)
    assert rows[-1].quantities.ballots_cast is None
    correction = advance(inc2, semantics='CORRECTION', votes=(3, 2, 0), suffix='correction', revision=True)
    state = state_for(case, (first, inc1, inc2, correction))
    assert state.materialized_counts.rows[0].votes == (3, 2, 0)
    assert from_json(canonical_json(state)).materialized_counts == state.materialized_counts
    assert state.evidence.selected_ids[-1] == correction.evidence.evidence_id
    assert inc2.evidence.evidence_id not in state.evidence.selected_ids
    missing_inc = advance(case[3], semantics='INCREMENTAL', votes=(1, 2, 0))
    assert materialize_counts(case[0], (case[3], missing_inc))[-1].votes == (5, None, 0)


def test_old_results_cannot_overwrite_newer_or_decrease_implicitly():
    case = fixture()
    first = case[3]
    newer = advance(first)
    for observations in ((newer, first), (first, replace(newer, source_sequence=first.source_sequence)),
                         (first, replace(newer, source_as_of=first.source_as_of - timedelta(seconds=1))),
                         (first, replace(newer, votes=(3, None, 0)))):
        with pytest.raises(ValueError):
            materialize_counts(case[0], observations)
    with pytest.raises(ValueError, match='Base must be latest'):
        inc = advance(newer, semantics='INCREMENTAL')
        materialize_counts(case[0], (first, newer, replace(inc, base_observation_id=first.observation_id)))
    with pytest.raises(ValueError, match='Selected stale'):
        state_for(case, (first, newer), selected=(first.observation_id,))


def test_same_timestamp_receipt_order_and_dst_instants():
    case = fixture()
    first = case[3]
    next_obs = advance(first)
    next_obs = replace(next_obs, evidence=replace(next_obs.evidence, received_at=first.evidence.received_at,
        published_at=first.evidence.published_at), source_as_of=first.source_as_of)
    state_for(case, (first, next_obs))
    with pytest.raises(ValueError):
        materialize_counts(case[0], (next_obs, first))
    zone = ZoneInfo('America/New_York')
    early = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    late = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    e = replace(first.evidence, received_at=late, published_at=late)
    with pytest.raises(ValueError, match='after cutoff'):
        selection((e,), early)
    assert e.received_at.tzinfo is timezone.utc


def test_revision_ancestry_selection_forks_and_cross_graph_conflicts():
    race, e, _, obs, prior, state, _ = fixture()
    revision = replace(e, evidence_id='revision', revision=1, supersedes_id=e.evidence_id,
        receipt_order=e.receipt_order + 1, raw_payload=asset('revised-raw'))
    selected = selection((e, revision), e.received_at)
    assert selected.selected_ids == ('revision',)
    for catalog in ((revision,), (e, replace(revision, revision=2)),
                    (e, revision, replace(revision, evidence_id='fork', receipt_order=99))):
        with pytest.raises(ValueError):
            selection(catalog, e.received_at)
    with pytest.raises(ValueError, match='latest'):
        replace(selected, selected_ids=(e.evidence_id,))
    with pytest.raises(ValueError, match='Conflicting evidence'):
        EvidenceSelection(cutoff=e.received_at, catalog=(e, replace(e, raw_payload=asset('conflict'))), selected_ids=(e.evidence_id,))
    corrupted_rules = replace(race.rules_evidence, raw_payload=asset('different-rule-bytes'))
    with pytest.raises(ValueError):
        replace(state, evidence=selection((corrupted_rules, race.definition_evidence, e), e.received_at))
    with pytest.raises(ValueError):
        replace(state, observations=(replace(obs, evidence=replace(e, raw_payload=asset('different-count-bytes'))),))
    with pytest.raises(ValueError):
        replace(prior, evidence_cutoff=T0 - timedelta(seconds=1))


def test_raw_overlap_preserved_but_active_double_count_rejected():
    case = fixture()
    county = case[3]
    state_total = advance(county, suffix='state')
    state_total = replace(state_total, coverage=Coverage(unit_id='US:IN', ballot_method='ALL'))
    method = advance(state_total, suffix='mail')
    method = replace(method, coverage=Coverage(unit_id=county.coverage.unit_id, ballot_method='MAIL'))
    observations = (county, state_total, method)
    partition = replace(case[5].partition, selected_observation_ids=(state_total.observation_id,),
        excluded=tuple(ExcludedObservation(observation_id=o.observation_id, reason='OVERLAPPING_REDUNDANT',
            explanation='Explicitly prefer statewide aggregate for this synthetic example.') for o in (county, method)),
        precedence='STATE_TOTAL', require_complete_coverage=True)
    state = replace(case[5], evidence_cutoff=method.evidence.received_at,
        evidence=selection((*case[4].evidence, *(o.evidence for o in observations)), method.evidence.received_at),
        observations=observations, partition=partition,
        materialized_counts=MaterializedCounts(race_definition=case[0].ref,
            rows=(materialize_counts(case[0], observations)[1],)))
    assert len(state.observations) == 3 and len(state.materialized_counts.rows) == 1
    with pytest.raises(ValueError):
        replace(state, partition=replace(partition,
            selected_observation_ids=tuple(o.observation_id for o in observations), excluded=()))
    with pytest.raises(ValueError):
        replace(state, partition=replace(partition, excluded=()))
    with pytest.raises(ValueError):
        replace(partition, precedence='EXPLICIT_RECONCILIATION')


def test_local_joint_uncertainty_and_conservation():
    race, _, _, _, prior, _, _ = fixture()
    payload = prior.local_allocation
    assert len(payload.draws) == 3 and payload.draws[0].statewide_draw_index == payload.draws[1].statewide_draw_index
    assert payload.draws[0].support_shares != payload.draws[1].support_shares
    assert sum(payload.draws[0].cell_votes[0]) != sum(payload.draws[2].cell_votes[0])
    bad_matrix = ((13, 7, 5), *payload.draws[0].cell_votes[1:])
    changed_draw = replace(payload.draws[0], cell_votes=bad_matrix,
        cell_quantities=tuple(known_quantities(row) for row in bad_matrix))
    changed = replace(payload, draws=(changed_draw, *payload.draws[1:]))
    manifest = replace(prior.allocation_manifest, payload=store(changed))
    with pytest.raises(ValueError, match='support conservation'):
        validate_local_allocation(race, prior.simulation, changed, manifest, prior.allocation_run)
    with pytest.raises(ValueError, match='artifact binding'):
        validate_local_allocation(race, prior.simulation, changed, prior.allocation_manifest, prior.allocation_run)
    bad_axes = replace(payload, candidate_ids=payload.candidate_ids[::-1], draws=payload.draws[:-1])
    with pytest.raises(ValueError):
        validate_local_allocation(race, prior.simulation, bad_axes, prior.allocation_manifest, prior.allocation_run)
    with pytest.raises(ValueError):
        replace(payload, draws=(replace(payload.draws[0], conditional_weight=0.7), *payload.draws[1:]))
    with pytest.raises(ValueError):
        replace(prior, draws=prior.draws[::-1])
    zero = LocalDraw(statewide_draw_index=0, residual_draw_index=0, conditional_weight=1.0,
        cell_votes=((0, 0, 0),), cell_quantities=(VoteQuantities(valid_contest_votes=0),))
    assert zero.support_shares == (None,)


def test_poll_order_revision_fieldwork_and_availability():
    race, _, poll, _, prior, _, _ = fixture()
    for changes in (dict(candidate_ids=('unrelated0', 'unrelated1', 'unrelated2')),
                    dict(candidate_ids=poll.candidate_ids[::-1]), dict(race_id='other')):
        with pytest.raises(ValueError):
            validate_poll(race, replace(poll, **changes), T0)
    with pytest.raises(ValueError):
        replace(poll, field_end=date(2026, 1, 2))
    with pytest.raises(ValueError):
        replace(poll, shares=(float('nan'), 0.3, 0.2))
    with pytest.raises(ValueError):
        validate_poll(race, poll, T0 - timedelta(microseconds=1))
    subset = replace(poll, candidate_ids=(poll.candidate_ids[0], poll.candidate_ids[2]),
                     shares=(0.3, 0.2), other_share=0.4)
    validate_poll(race, subset, T0)
    with pytest.raises(ValueError):
        replace(prior, run=replace(prior.run, inputs=replace(prior.run.inputs,
            evidence=replace(prior.run.inputs.evidence, cutoff=T0 - timedelta(seconds=1)))))


def test_input_cutoffs_training_lineage_model_versions_and_rng():
    race, _, _, _, prior, _, forecast = fixture()
    manifest = prior.run.inputs
    first = manifest.ordered_inputs[0]
    for changes in (dict(available_at=T0 + timedelta(seconds=1)),
                    dict(source_evidence_ids=('unknown',))):
        with pytest.raises(ValueError):
            replace(manifest, ordered_inputs=(replace(first, **changes), *manifest.ordered_inputs[1:]))
    with pytest.raises(ValueError):
        replace(manifest, training_cutoff=T0 - timedelta(seconds=1))
    with pytest.raises(ValueError):
        replace(prior.model, training_manifest=manifest)
    with pytest.raises(ValueError):
        replace(forecast.run, fitted_models=(replace(prior.model,
            training_manifest=replace(prior.model.training_manifest, training_cutoff=T0 + timedelta(days=1))),))
    for changes in (dict(streams=(RNGStream(name='same', spawn_key=(0,)), RNGStream(name='same', spawn_key=(1,)))),
                    dict(streams=(RNGStream(name='a', spawn_key=(0,)), RNGStream(name='b', spawn_key=(0,)))),
                    dict(seed=-1)):
        with pytest.raises(ValueError):
            replace(forecast.run.rng, **changes)
    with pytest.raises(ValueError):
        replace(forecast.run, numeric_tolerance=float('inf'))
    with pytest.raises(ValueError):
        replace(forecast, run=replace(forecast.run, inputs=replace(forecast.run.inputs,
            ordered_inputs=tuple(i for i in forecast.run.inputs.ordered_inputs if i.kind != 'COUNTS'))))


def test_synthetic_provenance_cannot_be_promoted():
    _, e, _, obs, prior, _, forecast = fixture()
    for changes in (dict(origin='REAL'), dict(quality='VERIFIED_OFFICIAL')):
        with pytest.raises(ValueError):
            replace(e, **changes)
    with pytest.raises(ValueError):
        replace(prior.model, status='HISTORICALLY_EVALUATED', evaluation_report=asset('fake-evaluation'))
    for changes in (dict(origin='REAL', quality='EXPERIMENTAL'),
                    dict(quality='EVALUATED', eligibility='EVALUATED_FORECAST'),
                    dict(eligibility='EVALUATED_FORECAST')):
        with pytest.raises(ValueError):
            replace(forecast, **changes)
    withheld = replace(forecast, quality='WITHHELD', eligibility='WITHHELD', output_draws=())
    assert withheld.origin == 'SYNTHETIC'
    with pytest.raises(ValueError):
        validate_count(prior.race, replace(obs, evidence=replace(e, source_kind='OTHER')))


@pytest.mark.parametrize('root', ['42', 'null', '[]', '"string"', 'true'])
def test_noncontract_json_roots_rejected(root):
    with pytest.raises(ValueError):
        from_json(root)


def test_json_duplicates_versions_fields_nonfinite_and_signed_zero():
    value = JointDraw(shares=(0.0, 0.5, 0.5), total_valid_votes=100)
    data = json.loads(canonical_json(value))
    for changes in (dict(schema_version=1), dict(schema_version=99), dict(unknown_field=1), dict(type='Unknown')):
        with pytest.raises(ValueError):
            from_json(json.dumps({**data, **changes}))
    for key in ('schema_version', 'shares'):
        bad = dict(data)
        del bad[key]
        with pytest.raises(ValueError):
            from_json(json.dumps(bad))
    with pytest.raises(ValueError, match='Duplicate'):
        from_json(canonical_json(value)[:-1] + ',"schema_version":2}')
    with pytest.raises(ValueError):
        from_json('{"type":"JointDraw","schema_version":2,"shares":[NaN,0.5],"total_valid_votes":100}')
    assert content_id(value) == content_id(replace(value, shares=(-0.0, 0.5, 0.5)))
    assert '-0.0' not in canonical_json(replace(value, shares=(-0.0, 0.5, 0.5)))
    with pytest.raises(ValueError):
        canonical_json(42)


def test_timestamp_normalization_and_naive_rejection():
    e = fixture()[1]
    offset = timezone(timedelta(hours=-5))
    assert content_id(e) == content_id(replace(e, received_at=e.received_at.astimezone(offset)))
    with pytest.raises(ValueError):
        replace(e, received_at=e.received_at.replace(tzinfo=None))
    with pytest.raises(ValueError):
        replace(e, published_at=e.received_at + timedelta(seconds=1))


def test_wrong_raw_bytes_and_raw_manifest_binding_rejected():
    prior = fixture()[4]
    with pytest.raises(ValueError, match='Artifact hash'):
        verify_artifact(prior.allocation_manifest.payload, b'corrupted bytes')
    manifest = prior.run.inputs
    first = manifest.ordered_inputs[0]
    with pytest.raises(ValueError, match='Raw input'):
        replace(manifest, ordered_inputs=(replace(first, artifact=asset('wrong-source')), *manifest.ordered_inputs[1:]))


def test_graph_rejects_same_race_revision_with_different_contents():
    race = make_race()
    changed = replace(race, candidates=(replace(race.candidates[0], name='Other spelling'), *race.candidates[1:]))
    with pytest.raises(ValueError, match='Conflicting race-definition'):
        validate_dependency_graph((race, changed))


def test_valid_complete_partition_and_partial_count_requirements():
    case = fixture()
    first = case[3]
    a = replace(first, votes=(4, 3, 0), reporting_status='COMPLETE', quantities=known_quantities((4, 3, 0)))
    b = advance(a, votes=(5, 4, 1), suffix='second-county')
    b = replace(b, coverage=Coverage(unit_id=case[0].reporting_units[2].unit_id, ballot_method='ALL'),
                reporting_status='COMPLETE', quantities=known_quantities((5, 4, 1)))
    state = state_for(case, (a, b), selected=(a.observation_id, b.observation_id),
                      require_complete_coverage=True, require_complete_counts=True)
    assert tuple(r.votes for r in state.materialized_counts.rows) == ((4, 3, 0), (5, 4, 1))
    with pytest.raises(ValueError, match='Incomplete'):
        state_for(case, (a,), require_complete_coverage=True)


def test_increment_cannot_repair_missing_with_a_zero_and_repeated_ids_refused():
    case = fixture()
    first = case[3]
    increment = advance(first, semantics='INCREMENTAL', votes=(0, 0, 0))
    assert materialize_counts(case[0], (first, increment))[-1].votes == (4, None, 0)
    with pytest.raises(ValueError):
        materialize_counts(case[0], (first, first))


def test_publication_receipt_lag_controls_asof_and_rules_availability():
    case = fixture()
    race, _, poll, _, _, _, _ = case
    delayed = replace(poll, evidence=replace(poll.evidence, received_at=T0 + timedelta(hours=2)))
    with pytest.raises(ValueError, match='unavailable'):
        validate_poll(race, delayed, T0 + timedelta(hours=1))
    with pytest.raises(ValueError):
        selection((delayed.evidence,), T0 + timedelta(hours=1))
    late_race = replace(race, rules_evidence=replace(race.rules_evidence, received_at=T0 + timedelta(hours=1)))
    rebound = replace(poll, race_definition=late_race.ref)
    with pytest.raises(ValueError, match='Definition unavailable'):
        validate_poll(late_race, rebound, T0)


def test_local_turnout_conservation_missing_draws_and_model_binding():
    race, _, _, _, prior, _, _ = fixture()
    payload = prior.local_allocation
    matrix = ((13, 8, 5), *payload.draws[0].cell_votes[1:])
    draw = replace(payload.draws[0], cell_votes=matrix, cell_quantities=tuple(known_quantities(row) for row in matrix))
    bad = replace(payload, draws=(draw, *payload.draws[1:]))
    with pytest.raises(ValueError, match='turnout conservation'):
        validate_local_allocation(race, prior.simulation, bad, replace(prior.allocation_manifest, payload=store(bad)), prior.allocation_run)
    bad = replace(payload, draws=payload.draws[:2])
    with pytest.raises(ValueError, match='Missing/extra statewide'):
        validate_local_allocation(race, prior.simulation, bad, replace(prior.allocation_manifest, payload=store(bad)), prior.allocation_run)
    with pytest.raises(ValueError, match='model binding'):
        validate_local_allocation(race, prior.simulation, payload, replace(prior.allocation_manifest, model_hash='1'*64), prior.allocation_run)


def test_complete_graph_cannot_hide_a_known_applicable_revision():
    e = fixture()[1]
    successor = replace(e, evidence_id='known-successor', revision=1, supersedes_id=e.evidence_id,
                        receipt_order=e.receipt_order + 1, raw_payload=asset('successor'))
    stale = selection((e,), e.received_at)
    current = selection((e, successor), e.received_at)
    with pytest.raises(ValueError, match='omits known applicable revision'):
        validate_dependency_graph((stale, current))
    earlier = replace(stale, cutoff=e.received_at - timedelta(seconds=1), catalog=(), selected_ids=())
    validate_dependency_graph((earlier, current))


def test_original_forecast_convenience_accessors_have_one_source_of_truth():
    forecast = fixture()[-1]
    assert forecast.code == forecast.run.code
    assert forecast.environment == forecast.run.environment
    assert forecast.configuration == forecast.run.configuration
    assert forecast.random_seed == forecast.run.rng.seed
    assert forecast.evidence_manifest == forecast.run.inputs


@pytest.mark.parametrize('office', ['US_SENATE', 'GOVERNOR'])
def test_replacement_roster_flows_through_all_four_worker_artifacts(office):
    race, _, poll, obs, prior, state, forecast = fixture(office, special=True, revised=True)
    validate_race_revision(make_race(office, special=True), race)
    validate_poll(race, poll, prior.evidence_cutoff)
    validate_count(race, obs)
    assert len(race.candidates) == 4
    assert race.candidates[0].status == 'REPLACED'
    assert obs.votes == (0, None, 0, 4)
    assert all(draw.shares[0] == 0 for draw in prior.draws)
    assert all(row[0] == 0 for draw in prior.local_allocation.draws for row in draw.cell_votes)
    assert from_json(canonical_json(forecast)) == forecast
    with pytest.raises(ValueError):
        validate_poll(race, fixture(office, special=True)[2])


def test_identical_raw_bytes_from_distinct_source_objects_keep_both_contexts():
    race = make_race()
    a = evidence('object-a', 'OTHER', 20)
    b = replace(evidence('object-b', 'OTHER', 21), raw_payload=a.raw_payload)
    manifest = inputs(race, (a, b))
    duplicates = [i for i in manifest.ordered_inputs if i.artifact.sha256 == a.raw_payload.sha256]
    assert len(duplicates) == 2
    assert duplicates[0].source_evidence_ids != duplicates[1].source_evidence_ids
    with pytest.raises(ValueError, match='Duplicate input entries'):
        replace(manifest, ordered_inputs=(*manifest.ordered_inputs, manifest.ordered_inputs[0]))


def test_candidate_status_does_not_infer_winner_eligibility():
    race = make_race()
    candidate = race.candidates[0]
    assert replace(candidate, status='WITHDRAWN').winner_eligibility == 'ELIGIBLE'
    assert replace(candidate, status='WITHDRAWN', winner_eligibility='UNRESOLVED').winner_eligibility == 'UNRESOLVED'
    with pytest.raises(ValueError, match='Aggregate write-ins'):
        replace(candidate, kind='WRITE_IN_AGGREGATE')
    assert replace(candidate, kind='WRITE_IN_AGGREGATE', winner_eligibility='UNRESOLVED').kind == 'WRITE_IN_AGGREGATE'
