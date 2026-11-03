from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
import json
import pytest
from forecasting.contracts import canonical_json, content_id, from_json, validate_count
from forecast_fixtures import fixture


def test_all_contracts_round_trip_and_immutable():
    for value in fixture():
        assert from_json(canonical_json(value)) == value
        assert content_id(from_json(canonical_json(value))) == content_id(value)
        with pytest.raises(FrozenInstanceError):
            value.schema_version = 2


def test_timezone_canonicalization_and_receipt_cutoff():
    _, evidence, _, _, prior, _, forecast = fixture()
    same = replace(evidence, received_at=evidence.received_at.astimezone(timezone(timedelta(hours=-5))))
    assert content_id(evidence) == content_id(same)
    with pytest.raises(ValueError):
        replace(evidence, received_at=evidence.received_at.replace(tzinfo=None))
    with pytest.raises(ValueError):
        replace(prior, evidence=(replace(evidence, received_at=evidence.received_at + timedelta(seconds=1)),))
    with pytest.raises(ValueError):
        replace(forecast, evidence_cutoff=forecast.created_at + timedelta(seconds=1))


@pytest.mark.parametrize('changes', [dict(office='PRESIDENT'), dict(rules='YES_NO'),
    dict(rules='RANKED_CHOICE'), dict(state='DC'), dict(candidates=()), dict(ballot_methods=['MAIL'])])
def test_ineligible_or_mutable_races_rejected(changes):
    with pytest.raises(ValueError):
        replace(fixture()[0], **changes)


def test_vectors_quality_and_handoff():
    race, evidence, poll, obs, prior, state, forecast = fixture()
    for changes in [dict(votes=(-1, 2, 3)), dict(votes=(True, 2, 3)), dict(semantics='INCREMENTAL'),
                    dict(semantics='CORRECTION'), dict(base_observation_id='unexpected')]:
        with pytest.raises(ValueError):
            replace(obs, **changes)
    with pytest.raises(ValueError):
        replace(poll, shares=(0.5, 0.5, 0.5))
    with pytest.raises(ValueError):
        replace(prior, draws=(replace(prior.draws[0], shares=(0.5, 0.5)),))
    for changes in [dict(candidate_ids=obs.candidate_ids[::-1]), dict(race_id='other'),
                    dict(evidence=replace(evidence, quality='VERIFIED_NONOFFICIAL'))]:
        with pytest.raises(ValueError):
            validate_count(race, replace(obs, **changes))
    with pytest.raises(ValueError):
        replace(forecast, quality='EVALUATED')
    with pytest.raises(ValueError):
        replace(prior.model, status='HISTORICALLY_EVALUATED')
    with pytest.raises(ValueError):
        replace(forecast, prior=replace(prior, prior_id='different'))
    with pytest.raises(ValueError):
        replace(state, observations=(obs, obs))


def test_revision_and_correction_contract():
    _, evidence, _, obs, _, state, _ = fixture()
    revision = replace(evidence, evidence_id='revision', revision=1, supersedes_id=evidence.evidence_id)
    correction = replace(obs, observation_id='correction', evidence=revision,
        semantics='CORRECTION', base_observation_id=obs.observation_id, votes=(1, 2, 1))
    assert replace(state, observations=(obs, correction)).observations[-1].votes == (1, 2, 1)
    with pytest.raises(ValueError):
        replace(state, observations=(correction, obs))
    with pytest.raises(ValueError):
        replace(evidence, revision=1)


def test_strict_schema_reader():
    data = json.loads(canonical_json(fixture()[0]))
    for key, value in [('schema_version', 2), ('unknown_field', 1), ('type', 'Unknown')]:
        bad = {**data, key: value}
        with pytest.raises((ValueError, KeyError, TypeError)):
            from_json(json.dumps(bad))


def test_latest_base_and_shared_raw_evidence():
    _, _, _, obs, _, state, _ = fixture()
    newer = replace(obs, observation_id='newer')
    stale = replace(obs, observation_id='stale', semantics='INCREMENTAL', base_observation_id=obs.observation_id)
    with pytest.raises(ValueError):
        replace(state, observations=(obs, newer, stale))
    update = replace(stale, base_observation_id='newer')
    assert len(replace(state, observations=(obs, newer, update)).observations) == 3


def test_schema_version_must_be_present_in_wire_data():
    data = json.loads(canonical_json(fixture()[0]))
    del data['schema_version']
    with pytest.raises(ValueError):
        from_json(json.dumps(data))
