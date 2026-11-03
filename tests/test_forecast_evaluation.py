"""All electoral values here are invented; no empirical accuracy results."""
from dataclasses import replace
from datetime import timedelta
import math
import pytest
from forecasting.evaluation import (Outcome, cutoff_for, chronological_training,
    score, summarize, evaluate, historical_replay, audit_forecast)
from forecast_fixtures import fixture, evidence, T0
from forecasting.synthetic_slice import prepare, instant
from forecasting.evaluation_integrity import materialize_outcome


def outcome(race, votes=(40, 35, 25)):
    return Outcome(race, race.election_date.year,
        tuple(c.candidate_id for c in race.candidates), votes,
        evidence('final', 'OFFICIAL_RESULTS', 100, T0 + timedelta(days=310)))


@pytest.mark.parametrize('office', ['US_SENATE', 'GOVERNOR'])
def test_metrics(office, tmp_path):
    case = prepare(tmp_path, office)
    try:
        final = case.report('metric-final', (40,35,25), instant(2026,11,4), status='COMPLETE')
        state = case.state((final,), (final.observation_id,), final.evidence.received_at, complete=True)
        o = materialize_outcome(state, case.artifacts)
        record = case.pre_record
        s = score(record, o, o.candidate_ids[:2], store=case.artifacts)
        expected_margin = sum(d.shares[0]-d.shares[1] for d in record.output_draws)/len(record.output_draws)
        assert s['margin_absolute_error'] == pytest.approx(abs(expected_margin-.05))
        assert s['brier'] == 0
        assert math.isfinite(s['log_loss'])
        assert s['covered'] == 0
        assert summarize([s])['n'] == 1
        assert sum(b['n'] for b in summarize([s])['calibration']) == 1
        assert summarize([])['metrics'] is None
    finally:
        case.sources.close()


def test_cutoffs_and_cycle_holdout():
    race = fixture()[0]
    target = outcome(race)
    for days in (30, 7, 1):
        c = cutoff_for(race, days)
        assert (race.election_date - c.astimezone(__import__('zoneinfo').ZoneInfo(race.election_timezone)).date()).days == days
        assert chronological_training((target,), target, c) == ()
    earlier_race = replace(race, election_date=race.election_date.replace(year=2024),
        race_id=race.race_id.replace('2026-11-03', '2024-11-03'))
    old = replace(outcome(earlier_race), evidence=evidence('old-final', 'OFFICIAL_RESULTS', 99))
    assert chronological_training((old, target), target, cutoff_for(race, 30)) == (old,)
    late = replace(old, evidence=replace(old.evidence, received_at=T0+timedelta(days=400)))
    assert chronological_training((late,), target, cutoff_for(race, 1)) == ()


def test_mapping_missing_and_duplicates():
    race, *_, record = fixture()
    o = outcome(race)
    with pytest.raises(ValueError, match='mapping'):
        replace(o, candidate_ids=tuple(reversed(o.candidate_ids)))
    with pytest.raises(ValueError, match='Missing'):
        score(record, replace(o, votes=(40, None, 0)), o.candidate_ids[:2])
    with pytest.raises(ValueError, match='Duplicate'):
        evaluate((o, o), None)
    other = outcome(fixture('GOVERNOR')[0])
    with pytest.raises(ValueError, match='mapping'):
        score(record, other, other.candidate_ids[:2])
    with pytest.raises(ValueError, match='trusted outcome materialization'):
        evaluate((o,), None)


def test_no_synthetic_replay_or_early_count_extrapolation():
    race, _, _, obs, _, _, record = fixture()
    with pytest.raises(ValueError, match='Authentic'):
        historical_replay(race, (obs,), (T0,), verify_raw=lambda e: True)
    # Early counties cannot masquerade as a pre-election forecast.
    with pytest.raises(ValueError):
        audit_forecast(record, outcome(race), record.evidence_cutoff, ())
    assert historical_replay(race, (), (T0,), verify_raw=lambda e: False) == []


def preelection(race, cutoff):
    from forecast_fixtures import inputs, run, store
    from forecasting.contracts import InputArtifact, ForecastRecord
    _, _, _, _, prior, _, _ = fixture(race.office)
    models = (prior.model, prior.local_allocation_model)
    item = InputArtifact(artifact=store(prior), kind='PRIOR', version='synthetic-prior',
        available_at=prior.evidence_cutoff,
        source_evidence_ids=tuple(e.evidence_id for e in prior.evidence))
    manifest = inputs(race, prior.evidence, cutoff=cutoff, extra=(item,))
    execution = run(race, 'STATEWIDE_PRIOR', models, manifest, name='evaluation-asof')
    return ForecastRecord(forecast_id='synthetic-asof', created_at=execution.created_at,
        evidence_cutoff=cutoff, prior=prior, counting_state=None, models=models,
        data_version=manifest.data_version, run=execution,
        output_draws=fixture(race.office)[-1].output_draws, origin='SYNTHETIC',
        quality='SYNTHETIC', eligibility='RESEARCH_ONLY', quality_reasons=('Invented test only',))


def dataset():
    current = [outcome(fixture(office)[0]) for office in ('US_SENATE', 'GOVERNOR')]
    previous = []
    for o in current:
        race = replace(o.race, election_date=o.race.election_date.replace(year=2024),
            race_id=o.race.race_id.replace('2026-11-03', '2024-11-03'))
        previous.append(replace(outcome(race), evidence=evidence('old-final', 'OFFICIAL_RESULTS', 99)))
    return tuple(previous + current)


def test_end_to_end_groups_and_leakage():
    def predict(train, race, cutoff):
        return preelection(race, cutoff), tuple(o.race.race_id for o in train), 1
    # These old enumerated fixtures have neither raw-normalized outcomes nor
    # authenticated fitting lineage; they may no longer certify performance.
    with pytest.raises(ValueError, match='trusted outcome materialization'):
        evaluate(dataset(), predict)
    def leak(train, race, cutoff):
        return preelection(race, cutoff), (race.race_id,), 1
    with pytest.raises(ValueError, match='trusted outcome materialization'):
        evaluate(dataset(), leak)
    def duplicate_poll(train, race, cutoff):
        return preelection(race, cutoff), (), 2
    with pytest.raises(ValueError, match='trusted outcome materialization'):
        evaluate(dataset(), duplicate_poll)


def test_replay_corrections_missing_duplicates_and_biased_county():
    # Exercise REAL enum validator paths only; bytes and dates remain invented.
    from forecast_fixtures import real_counting_observation, known_quantities, asset
    from forecasting.contracts import materialize_counts
    race, _, _, raw, *_ = fixture()
    obs = real_counting_observation(raw)
    obs = replace(obs, source_as_of=obs.evidence.received_at)
    later = obs.evidence.received_at + timedelta(seconds=1)
    correction = replace(obs, observation_id='test-correction', source_sequence=obs.source_sequence+1,
        source_as_of=later, semantics='CORRECTION', base_observation_id=obs.observation_id,
        votes=(1, 2, 0), quantities=known_quantities((1, 2, 0)),
        evidence=replace(obs.evidence, evidence_id='test-correction-evidence', source_object_id='bulletin',
            received_at=later, published_at=later, receipt_order=50, raw_payload=asset('correction')))
    result = historical_replay(race, (obs, correction), (obs.evidence.received_at, later),
                               verify_raw=lambda e: True)
    assert result[0][1][-1].votes == obs.votes
    assert result[1][1][-1].votes == (1, 2, 0)
    with pytest.raises(ValueError, match='Duplicate'):
        historical_replay(race, (obs, obs), (later,), verify_raw=lambda e: True)
    with pytest.raises(ValueError, match='Authentic'):
        historical_replay(race, (obs,), (later,), verify_raw=lambda e: False)
    with pytest.raises(ValueError, match='Decrease'):
        materialize_counts(race, (obs, replace(correction, semantics='CUMULATIVE', base_observation_id=None)))
    county = replace(obs, coverage=replace(obs.coverage, unit_id=race.reporting_units[1].unit_id))
    county_rows = historical_replay(race, (county,), (later,), verify_raw=lambda e: True)[0][1]
    assert len(county_rows) == 1
    assert county_rows[0].coverage.unit_id != 'US:IN'
    assert county_rows[0].votes == county.votes  # Never extrapolate early counties.
