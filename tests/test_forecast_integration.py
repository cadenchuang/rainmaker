"""Actual A/B/C/D producer exchange; all election values are invented."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from forecasting.contracts import Coverage, canonical_json, content_id, from_json
from forecasting.counting import CountingConfig, forecast, replay
from forecasting.evaluation import Outcome, audit_forecast
from forecasting.integration import evaluate_record, verify_graph
from forecasting.synthetic_slice import instant, prepare, run_office


@pytest.fixture
def case(tmp_path):
    value = prepare(tmp_path, 'US_SENATE')
    try:
        yield value
    finally:
        value.sources.close()


@pytest.mark.parametrize('office', ['US_SENATE', 'GOVERNOR'])
def test_saved_evidence_to_fitted_prior_live_record_and_evaluation(tmp_path, office):
    summary = run_office(tmp_path, office)
    assert summary['origin'] == 'SYNTHETIC'
    assert summary['model_status'] == 'EXPERIMENTAL'
    assert summary['eligibility'] == 'RESEARCH_ONLY'
    assert summary['complete_count_converged'] and summary['replay_identical']
    assert {r['phase'] for r in summary['scores']} == {'PRE_ELECTION', 'LIVE_COUNTING'}
    assert len(summary['scores']) == 2
    report = json.loads((tmp_path/'artifacts'/summary['report_hash']).read_bytes())
    assert report['run']['role'] == 'EVALUATION'
    assert report['report']['origin'] == 'SYNTHETIC'
    assert {r['forecast_sha256'] for r in report['report']['rows']} <= {
        item['artifact']['sha256'] for item in report['run']['inputs']['ordered_inputs']}
    # Reopening the durable source store reconstructs identical artifacts.
    again = run_office(tmp_path, office)
    assert summary == again


def test_correction_missingness_and_increment_pass_actual_producers(case):
    at = instant(2026, 11, 4)
    first = case.report('initial', (40, None, 0), at)
    correction = case.report('corrected', (30, None, 0), at+timedelta(minutes=1), sequence=2,
        semantics='CORRECTION', base=first.observation_id)
    increment = case.report('increment', (2, 3, 0), at+timedelta(minutes=2), sequence=3,
        semantics='INCREMENTAL', base=correction.observation_id)
    state = case.state((first, correction, increment), (increment.observation_id,), increment.evidence.received_at)
    assert state.materialized_counts.rows[0].votes == (32, None, 0)
    assert len(state.partition.excluded) == 2
    result = forecast(case.prior, state, created_at=state.evidence_cutoff, config=CountingConfig(draws=32))
    assert all(group[0].counted_votes[0] == 32 and group[0].counted_votes[2] == 0 for group in result.group_draws)
    assert any('Unavailable candidate counts' in reason for reason in result.record.quality_reasons)
    assert canonical_json(replay(result.record, result.artifacts).record) == canonical_json(result.record)
    # An unexplained decrease is rejected upstream and never reaches C.
    with pytest.raises(ValueError, match='Decrease'):
        case.report('unexplained', (1, None, 0), at+timedelta(minutes=3), sequence=4)


def test_overlapping_counts_cannot_enter_counting(case):
    at = instant(2026, 11, 4)
    state_total = case.report('state', (50, 40, 10), at)
    county = case.report('county', (20, 10, 5), at+timedelta(seconds=1),
        coverage=Coverage(unit_id=case.prior.local_allocation.unit_ids[0], ballot_method='ALL'))
    with pytest.raises(ValueError, match='[Oo]verlap'):
        case.state((state_total, county), (state_total.observation_id, county.observation_id),
                   county.evidence.received_at)


def test_hash_revision_and_cutoff_boundaries(case):
    prior = case.prior
    assert content_id(from_json(canonical_json(prior))) == content_id(prior)
    assert all(e.origin == 'SYNTHETIC' and e.quality == 'SYNTHETIC' for e in prior.evidence)
    at = instant(2026, 11, 4)
    final = case.report('final-boundary', (900, 650, 160), at, status='COMPLETE')
    with pytest.raises(ValueError, match='Stale active total'):
        case.state((final,), (final.observation_id,), prior.evidence_cutoff)
    with pytest.raises(ValueError, match='definition|revision|Definition'):
        case.state(
            (replace(final, race_definition=replace(final.race_definition, revision=1)),),
            (final.observation_id,), at)
    ref = prior.allocation_manifest.payload
    path = case.artifacts.root/ref.sha256
    original = path.read_bytes()
    path.write_bytes(b'corrupt')
    with pytest.raises(ValueError, match='hash mismatch'):
        verify_graph(prior, case.artifacts)
    path.write_bytes(original)


def test_pre_and_live_evaluation_boundaries(case):
    at = instant(2026, 11, 4)
    partial = case.report('evaluation-partial', (4, 3, 0), at)
    state = case.state((partial,), (partial.observation_id,), at)
    live = forecast(case.prior, state, created_at=at, config=CountingConfig(draws=32))
    for raw in live.artifacts.values():
        case.artifacts.put(raw)
    final = case.report('evaluation-final', (900, 650, 160), at+timedelta(hours=1), status='COMPLETE', sequence=2)
    outcome = Outcome(case.race, 2026, final.candidate_ids, final.votes, final.evidence)
    assert evaluate_record(live.record, outcome, case.artifacts, training=())['phase'] == 'LIVE_COUNTING'
    with pytest.raises(ValueError, match='Pre-election scoring'):
        audit_forecast(live.record, outcome, at, ())
    with pytest.raises(ValueError, match='already available'):
        evaluate_record(live.record, replace(outcome, evidence=partial.evidence), case.artifacts, training=())
