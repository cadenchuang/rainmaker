"""P1 regressions through saved sources, fitted B prior, C state and D scoring.

All values, including REAL-enum adapter exercises, are invented test inputs.
They assert no authentic official results or historical accuracy.
"""
from dataclasses import replace
import json

import pytest

from forecasting.contracts import Coverage, canonical_json, content_id
from forecasting.evaluation import evaluate, audit_forecast, score, report_artifact
from forecasting.evaluation_integrity import (
    materialize_outcome, materialize_historical_outcome, fitted_membership, OfficialResult,
)
from forecasting.history import historical_payload
from forecasting.integration import evaluate_record, evaluation_run, prior_record
from forecasting.prior import build_prior
from forecasting.synthetic_slice import prepare, instant


@pytest.fixture
def case(tmp_path):
    case = prepare(tmp_path, 'US_SENATE')
    try:
        yield case
    finally:
        case.sources.close()


def declarations(case):
    return tuple(materialize_historical_outcome(h, case.sources.selection(case.prior.evidence_cutoff),
                 case.artifacts) for h in case.history)


def final_outcome(case):
    final = case.report('integrity-final', (900,650,160), instant(2026,11,4), status='COMPLETE')
    state = case.state((final,), (final.observation_id,), final.evidence.received_at, complete=True)
    return materialize_outcome(state, case.artifacts)


def rewrite_fit(case, role, transform):
    """Produce a contract-valid mutation; hashes/refs are recomputed, not corrupted."""
    prior = case.prior
    model = prior.model if role == 'STATEWIDE_PRIOR' else prior.local_allocation_model
    item = next(i for i in model.training_manifest.ordered_inputs if i.kind == 'FEATURES')
    doc = json.loads(case.artifacts.get(item.artifact))
    new_doc = transform(doc)
    updated = replace(item, artifact=case.artifacts.save(new_doc))
    def manifest(m):
        return replace(m, ordered_inputs=tuple(updated if i == item else i for i in m.ordered_inputs))
    model = replace(model, model_id=f'mutated:{content_id(model)}:{updated.artifact.sha256}',
                    training_manifest=manifest(model.training_manifest))
    if role == 'STATEWIDE_PRIOR':
        run = replace(prior.run, inputs=manifest(prior.run.inputs), fitted_models=(model,))
        prior = replace(prior, model=model, run=run)
    else:
        run = replace(prior.allocation_run, inputs=manifest(prior.allocation_run.inputs), fitted_models=(model,))
        allocation = replace(prior.allocation_manifest, model_hash=content_id(model), run_hash=content_id(run))
        prior = replace(prior, local_allocation_model=model, allocation_run=run, allocation_manifest=allocation)
    return prior_record(prior, case.artifacts, created_at=prior.run.created_at)


def test_complete_synthetic_label_and_actual_membership(case):
    label = final_outcome(case)
    training = declarations(case)
    row = evaluate_record(case.pre_record, label, case.artifacts, training=training)
    assert row['origin'] == 'SYNTHETIC'
    assert row['outcome_sha256'] == label.materialization.sha256
    assert len(case.examples) == 5 and len(row['training_ids']) == 6  # local profiles/features include 2002
    binding = json.loads(case.artifacts.get(label.materialization))
    assert binding['votes'] == [900,650,160]
    assert binding['partition']['require_complete_counts']
    assert binding['partition']['require_complete_coverage']
    assert binding['selected_evidence'][0]['raw'] == label.evidence.raw_payload.sha256
    assert case.pre_record.quality == 'SYNTHETIC' and case.prior.model.status == 'EXPERIMENTAL'
    run = evaluation_run(case.pre_record, label, case.artifacts,
        case.sources.selection(label.evidence.received_at), created_at=label.evidence.received_at)
    report_artifact({'rows':[row]}, run)
    omitted = replace(run, inputs=replace(run.inputs, ordered_inputs=tuple(
        i for i in run.inputs.ordered_inputs if i.artifact != label.materialization)))
    with pytest.raises(ValueError, match='omits materialized outcome'):
        report_artifact({'rows':[row]}, omitted)


def test_reviewer_vote_replacement_and_rehashed_arbitrary_votes_rejected(case):
    label = final_outcome(case)
    training = declarations(case)
    before = evaluate_record(case.pre_record, label, case.artifacts, training=training)
    assert before['brier'] == 0
    changed = replace(label, votes=(1,2000,0))
    assert changed.evidence == label.evidence and changed.materialization == label.materialization
    with pytest.raises(ValueError, match='votes differ'):
        evaluate_record(case.pre_record, changed, case.artifacts, training=training)
    binding = json.loads(case.artifacts.get(label.materialization))
    binding['votes'] = [1,2000,0]
    changed = replace(changed, materialization=case.artifacts.save(binding))
    with pytest.raises(ValueError, match='binding mismatch'):
        evaluate_record(case.pre_record, changed, case.artifacts, training=training)


@pytest.mark.parametrize('omit', ['all', 'local-only', 'contradict'])
def test_omitted_or_contradictory_training_cannot_self_certify(case, omit):
    label = final_outcome(case)
    training = declarations(case)
    supplied = () if omit == 'all' else training[1:] if omit == 'local-only' else (
        replace(training[0], votes=(1,2000,0)), *training[1:])
    with pytest.raises(ValueError, match='Training membership|Training declaration'):
        evaluate_record(case.pre_record, label, case.artifacts, training=supplied)


@pytest.mark.parametrize('role', ['STATEWIDE_PRIOR','LOCAL_ALLOCATION'])
def test_target_in_direct_or_local_fitting_payload_rejected(case, role):
    target = historical_payload(case.history[-1])
    target['race'] = json.loads(canonical_json(case.race))
    for row in target['observations']:
        row['race_id'] = case.race.race_id
        row['race_definition'] = json.loads(canonical_json(case.race.ref))
    def inject(doc):
        if role == 'STATEWIDE_PRIOR':
            doc['examples'][0]['target'] = target
        else:
            doc['history'][0] = target
        return doc
    record = rewrite_fit(case, role, inject)
    with pytest.raises(ValueError):  # target labels are also unavailable before target election day
        evaluate_record(record, final_outcome(case), case.artifacts, training=declarations(case))


def test_local_calibration_payload_is_not_silently_ignored(case):
    record = rewrite_fit(case, 'LOCAL_ALLOCATION', lambda doc: dict(doc, calibration_labels=[case.race.race_id]))
    with pytest.raises(ValueError, match='local fitting lineage'):
        evaluate_record(record, final_outcome(case), case.artifacts, training=declarations(case))


@pytest.mark.parametrize('failure', ['missing-bytes','tampered-bytes','missing-manifest','old-opaque-profile'])
def test_missing_tampered_or_insufficient_fitting_lineage_fails_closed(case, failure):
    record = case.pre_record
    if failure in ('missing-bytes','tampered-bytes'):
        item = next(i for i in case.prior.model.training_manifest.ordered_inputs if i.kind == 'FEATURES')
        path = case.artifacts.root/item.artifact.sha256
        path.unlink() if failure == 'missing-bytes' else path.write_bytes(b'[]')
    elif failure == 'missing-manifest':
        m = case.prior.model
        missing = replace(m.training_manifest,
            ordered_inputs=tuple(i for i in m.training_manifest.ordered_inputs if i.kind != 'FEATURES'))
        model = replace(m, training_manifest=missing)
        prior = replace(case.prior, model=model, run=replace(case.prior.run, fitted_models=(model,)))
        record = prior_record(prior, case.artifacts, created_at=prior.run.created_at)
    else:
        record = rewrite_fit(case, 'LOCAL_ALLOCATION', lambda doc: doc['profiles'])
    with pytest.raises((ValueError, FileNotFoundError), match='hash mismatch|No such file|fitting manifest|local fitting lineage'):
        evaluate_record(record, final_outcome(case), case.artifacts, training=declarations(case))


def test_actual_earlier_fit_and_forward_holdout_harness(case):
    label = final_outcome(case)
    training = declarations(case)
    # Supply labels from one earlier cycle to D's splitter, with all fitted
    # labels retained as well. For older targets the lack of an eligible fitted
    # predictor is explicit; only the later target is selected for this run.
    target_record = case.pre_record
    actual = fitted_membership(target_record, case.artifacts)
    assert {h.race.race_id for h in actual} == {o.race.race_id for o in training}
    audit_forecast(target_record, label, target_record.evidence_cutoff, training, store=case.artifacts)
    # Exercise D.evaluate using a wrapper that admits only the fixed later target;
    # earlier outcomes cannot silently receive today's fitted model.
    from forecasting.evaluation import EvaluationUnavailable
    def predict(eligible, race, cutoff):
        if race.ref != case.race.ref:
            raise EvaluationUnavailable('No independently fitted vintage for this earlier target')
        prior = build_prior(race=race, history=case.history, examples=case.examples,
            selection=case.sources.selection(cutoff), store=case.artifacts, fitted_at=cutoff, created_at=cutoff,
            seed=42, penalty=2.0, draw_count=8, residual_count=2)
        record = prior_record(prior, case.artifacts, created_at=cutoff)
        return record, tuple(o.race.race_id for o in training), 0
    report = evaluate((*training, label), predict, store=case.artifacts)
    assert len(report['rows']) == 3
    assert report['unique_races'] == 1
    assert all(row['origin'] == 'SYNTHETIC' and len(row['training_ids']) == 6 for row in report['rows'])
    assert all('No earlier eligible cycle' in e['reason'] or 'No independently fitted vintage' in e['reason']
               for e in report['exclusions'])
    def omitted(eligible, race, cutoff):
        record, _, polls = predict(eligible, race, cutoff)
        return record, (), polls
    with pytest.raises(ValueError, match='Training membership'):
        evaluate((*training, label), omitted, store=case.artifacts)


@pytest.fixture
def official_enum_state(tmp_path, monkeypatch):
    """Mock official decoder protocol ONLY. These are not authenticated results."""
    import forecast_fixtures as f
    from forecasting.prior import ArtifactStore
    from forecasting.contracts import VoteQuantities
    from forecasting.sources.totals import counting_state
    store = ArtifactStore(tmp_path/'mock-official-artifacts')
    race = f.make_race()
    def enum_evidence(name, kind, at, raw):
        return replace(f.evidence(name, kind, 100+len(name), at), origin='REAL', quality='VERIFIED_OFFICIAL',
            source_id='mock-official-only', source_url=f'https://fixture.invalid/{name}', raw_payload=store.put(raw))
    race = replace(race,
        rules_evidence=enum_evidence('rules','OFFICIAL_RULES',f.T0,b'invented rules protocol fixture'),
        definition_evidence=enum_evidence('roster','OFFICIAL_ROSTER',f.T0,b'invented roster protocol fixture'))
    monkeypatch.setattr(f, 'make_race', lambda *args: race)
    prior = f.fixture()[4]
    for raw in f.ASSET_BYTES.values():
        store.put(raw)
    store.save(race)
    at = instant(2026,11,4)
    raw = b'{"fixture_only":true,"release":"FINAL","votes":[900,650,160]}'
    e = enum_evidence('final','OFFICIAL_RESULTS',at,raw)
    template = f.fixture()[3]
    obs = replace(template, evidence=e, coverage=Coverage(unit_id='US:IN',ballot_method='ALL'),
        votes=(900,650,160), quantities=VoteQuantities(valid_contest_votes=1710), reporting_status='COMPLETE',
        completeness=(), source_as_of=at)
    selection = f.selection((*prior.evidence,e), at)
    state = counting_state(prior,(obs,),selection,(obs.observation_id,),state_id='mock-official-only',precedence='STATE_TOTAL')
    class MockAdapter:
        source_urls = {'mock-official-only': tuple(x.source_url for x in (race.rules_evidence,race.definition_evidence,e))}
        def definition(self, submitted, rules, roster):
            assert rules == b'invented rules protocol fixture' and roster == b'invented roster protocol fixture'
            return race
        def result(self, definition, submitted, data):
            doc = json.loads(data)
            normalized = replace(obs, votes=tuple(doc['votes']), evidence=submitted.evidence)
            return OfficialResult(normalized, True, doc['release'])
    return state, store, MockAdapter()


def test_complete_official_adapter_protocol_remains_supported(official_enum_state):
    state, store, adapter = official_enum_state
    with pytest.raises(ValueError, match='adapter unavailable'):
        materialize_outcome(state, store)
    label = materialize_outcome(state, store, official_adapter=adapter)
    assert label.votes == (900,650,160)
    # These labels exercise REAL-enum branches only; the enclosing prior remains
    # SYNTHETIC, and no actual official feed/forecast is claimed by this test.
    assert json.loads(store.get(label.materialization))['origin'] == 'REAL'
    assert json.loads(store.get(label.materialization))['state'] == content_id(state)
    record = prior_record(state.prior, store, created_at=state.prior.run.created_at)
    assert score(record, label, label.candidate_ids[:2], store=store, official_adapter=adapter)['brier'] == 0
    assert record.origin == 'SYNTHETIC' and record.quality == 'SYNTHETIC'


@pytest.mark.parametrize('failure', ['incomplete-counties','missing-counts','overlap','test-release'])
def test_real_outcome_eligibility_rejects_incomplete_ambiguous_and_test_results(official_enum_state, failure):
    state, store, adapter = official_enum_state
    from forecasting.contracts import MaterializedCounts, materialize_counts
    obs = state.observations[0]
    rows = (obs,)
    if failure == 'incomplete-counties':
        obs = replace(obs, coverage=Coverage(unit_id=state.prior.local_allocation.unit_ids[0],ballot_method='ALL'))
        rows = (obs,)
    elif failure == 'missing-counts':
        obs = replace(obs,votes=(900,None,160),reporting_status='PARTIAL')
        rows = (obs,)
    elif failure == 'overlap':
        county = replace(obs, observation_id='overlapping-county',
            coverage=Coverage(unit_id=state.prior.local_allocation.unit_ids[0],ballot_method='ALL'))
        rows = (obs,county)
    else:
        evidence = replace(obs.evidence, raw_payload=store.put(b'Test Results: fixture only'))
        obs = replace(obs,evidence=evidence)
        rows = (obs,)
    with pytest.raises(ValueError):
        catalog = tuple(obs.evidence if e.evidence_id == obs.evidence.evidence_id else e for e in state.evidence.catalog)
        selection = replace(state.evidence,catalog=catalog)
        partition = replace(state.partition,selected_observation_ids=tuple(o.observation_id for o in rows),
                            precedence='COUNTY_TOTALS' if failure=='incomplete-counties' else 'STATE_TOTAL')
        state = replace(state,observations=rows,evidence=selection,partition=partition,
            materialized_counts=MaterializedCounts(race_definition=state.prior.race.ref,
                                                    rows=materialize_counts(state.prior.race,rows)))
        materialize_outcome(state,store,official_adapter=adapter)
