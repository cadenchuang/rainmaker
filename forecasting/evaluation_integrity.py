"""Fail-closed evaluation boundaries for source labels and supported fitted inputs.

CAS hashes establish integrity, not official provenance. An official adapter is
trusted evaluator configuration, never supplied by a model/report. No production
official complete-result adapter is currently available in this repository.
"""
from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
from typing import Protocol

from .contracts import (
    CountingObservation, CountingState, RaceDefinition, canonical_json, content_id,
    from_json, require, validate_count,
)
from .history import HistoricalResult, TrainingExample, historical_from_payload, historical_payload, historical_features, party_axis
from .baseline import fit_fundamentals


@dataclass(frozen=True)
class OfficialResult:
    observation: CountingObservation
    final_contest_coverage: bool
    release: str


class OfficialOutcomeAdapter(Protocol):
    """Independent source decoder/authentication policy, installed by evaluator.

    It must authenticate the official source identity, reviewed race definition,
    exhaustive geography/method axis, original receipt and source chronology,
    ballot universe, and final release. Returning an observation is required;
    a boolean authenticity assertion cannot substitute for raw normalization.
    """
    source_urls: dict[str, tuple[str, ...]]

    def definition(self, race: RaceDefinition, rules: bytes, roster: bytes) -> RaceDefinition: ...

    def result(self, race: RaceDefinition, observation: CountingObservation, raw: bytes) -> OfficialResult: ...


def verify_sources(race, observations, store, official_adapter=None):
    """Compare normalized records with retained source bytes at the trust boundary."""
    synthetic = any(e.origin == 'SYNTHETIC' for e in
                    (race.rules_evidence, race.definition_evidence, *(o.evidence for o in observations)))
    if synthetic:
        require(all(e.origin == 'SYNTHETIC' for e in
                    (race.rules_evidence, race.definition_evidence, *(o.evidence for o in observations))),
                'Mixed source provenance cannot establish a complete outcome')
    if not synthetic:
        require(official_adapter is not None, 'Official outcome adapter unavailable')
        for e in (race.rules_evidence, race.definition_evidence):
            require(e.quality == 'VERIFIED_OFFICIAL' and e.source_url in
                    official_adapter.source_urls.get(e.source_id, ()), 'Unverified official definition identity')
        require(official_adapter.definition(race, store.get(race.rules_evidence.raw_payload),
                    store.get(race.definition_evidence.raw_payload)) == race, 'Official race definition mismatch')
    for obs in observations:
        validate_count(race, obs)
        raw = store.get(obs.evidence.raw_payload)
        require(obs.evidence.source_kind == 'OFFICIAL_RESULTS', 'Wrong result source identity')
        if obs.evidence.origin == 'SYNTHETIC':
            # The sole supported synthetic normalization format is the saved slice
            # producer. Arbitrary invented enum fixtures cannot assert raw binding.
            doc = json.loads(raw)
            require(doc.get('origin') == 'SYNTHETIC' and
                    obs.evidence.source_id == 'synthetic:vertical-slice' and
                    obs.evidence.source_url == f"fixture://{doc['fixture']}" and
                    datetime.fromisoformat(doc['received_at']) == obs.evidence.received_at,
                    'Synthetic source identity/receipt mismatch')
            p = doc['payload']
            require(p['race_ref'] == content_id(race) and tuple(p['votes']) == obs.votes and
                    from_json(json.dumps(p['coverage'])) == obs.coverage and
                    p['status'] == obs.reporting_status and p['sequence'] == obs.source_sequence and
                    p['semantics'] == obs.semantics and p['base'] == obs.base_observation_id and
                    tuple(p['candidate_ids']) == obs.candidate_ids and
                    datetime.fromisoformat(p['source_as_of']) == obs.source_as_of,
                    'Source normalization mismatch')
            require(obs.quantities.valid_contest_votes == (sum(obs.votes) if None not in obs.votes else None),
                    'Source contest total mismatch')
        else:
            # Mixed real/synthetic labels still require the REAL rows' verifier.
            require(official_adapter is not None, 'Official outcome adapter unavailable')
            require(obs.evidence.quality == 'VERIFIED_OFFICIAL' and obs.evidence.source_url in
                    official_adapter.source_urls.get(obs.evidence.source_id, ()), 'Unverified official source identity')
            require(not any(marker in raw.lower() for marker in
                            (b'test results', b'test release', b'"origin":"synthetic"')),
                    'Test-release provenance is not a final official result')
            verified = official_adapter.result(race, obs, raw)
            require(type(verified) is OfficialResult and verified.observation == obs and
                    verified.release == 'FINAL' and verified.final_contest_coverage,
                    'Unverified complete official result')
    return 'SYNTHETIC' if synthetic else 'REAL'


def _outcome_document(state, store, official_adapter=None):
    require(type(state) is CountingState, 'Canonical CountingState required')
    state = from_json(canonical_json(state))
    original_state = state
    # Re-run complete partition/count validation, numerical dependencies and both
    # chronological passes. Caller flags cannot waive any of these requirements.
    state = replace(state, partition=replace(state.partition,
                    require_complete_coverage=True, require_complete_counts=True))
    rows = state.materialized_counts.rows
    require(all(r.quantities.valid_contest_votes is not None and
                sum(r.votes) == r.quantities.valid_contest_votes for r in rows),
            'Complete contest-vote accounting required')
    by_id = {o.observation_id: o for o in state.observations}
    # Authenticate all consumed numerical ancestors as well, not just selected rows.
    consumed = set(state.partition.selected_observation_ids)
    pending = list(consumed)
    while pending:
        obs = by_id[pending.pop()]
        if obs.semantics == 'INCREMENTAL' and obs.base_observation_id not in consumed:
            consumed.add(obs.base_observation_id)
            pending.append(obs.base_observation_id)
    origin = verify_sources(state.prior.race, tuple(by_id[i] for i in sorted(consumed)), store, official_adapter)
    for e in (state.prior.race.rules_evidence, state.prior.race.definition_evidence):
        require(e.evidence_id in state.evidence.selected_ids, 'Unavailable outcome rules/roster')
        store.get(e.raw_payload)
    votes = tuple(sum(row.votes[j] for row in rows) for j in range(len(state.prior.race.candidates)))
    require(sum(votes) > 0, 'Empty final contest')
    evidence = tuple(sorted({by_id[i].evidence.evidence_id: by_id[i].evidence for i in consumed}.values(),
                            key=lambda e: e.receipt_key))
    # Validation strengthens requirements without republishing a changed count
    # state under its original logical ID. The outcome owns the complete partition.
    return original_state, dict(schema='materialized-outcome-v1', state=store.save(original_state).sha256,
        race_definition=content_id(state.prior.race), candidate_ids=[c.candidate_id for c in state.prior.race.candidates],
        votes=list(votes), partition=json.loads(canonical_json(state.partition)),
        materialized_counts=content_id(state.materialized_counts), origin=origin,
        selected_evidence=[dict(id=e.evidence_id, envelope=content_id(e), raw=e.raw_payload.sha256) for e in evidence])


def materialize_outcome(state, store, *, official_adapter=None):
    """Only constructor of scorable labels: derives totals from complete source state."""
    from .evaluation import Outcome
    state, document = _outcome_document(state, store, official_adapter)
    latest = max((o.evidence for o in state.observations if o.observation_id in
                  state.partition.selected_observation_ids), key=lambda e: e.receipt_key)
    return Outcome(state.prior.race, state.prior.race.election_date.year,
        tuple(document['candidate_ids']), tuple(document['votes']), latest, store.save(document))


def _historical_document(history, selection, store, official_adapter=None):
    """Independent complete cumulative archives need no fitted prior to be labels."""
    from .sources.totals import active_totals
    history = HistoricalResult(history.race, history.observations)
    selection = from_json(canonical_json(selection))
    for e in history.evidence:
        require(e in selection.catalog and e.evidence_id in selection.selected_ids,
                'Historical outcome evidence unavailable')
    _, partition, counts = active_totals(history.race, history.observations, selection,
        tuple(o.observation_id for o in history.observations), precedence='METHOD_DETAIL'
        if all(o.coverage.ballot_method != 'ALL' for o in history.observations) else 'COUNTY_TOTALS'
        if all(o.coverage.unit_id != f'US:{history.race.state}' for o in history.observations) else 'STATE_TOTAL',
        require_complete=True)
    partition = replace(partition, require_complete_counts=True)
    require(all(r.quantities.valid_contest_votes is not None and sum(r.votes) == r.quantities.valid_contest_votes
                for r in counts.rows), 'Complete historical contest-vote accounting required')
    origin = verify_sources(history.race, history.observations, store, official_adapter)
    return dict(schema='complete-history-outcome-v1', history=historical_payload(history),
        selection=json.loads(canonical_json(selection)), partition=json.loads(canonical_json(partition)),
        candidate_ids=[c.candidate_id for c in history.race.candidates], votes=list(history.votes), origin=origin,
        selected_evidence=[dict(id=o.evidence.evidence_id, envelope=content_id(o.evidence),
                               raw=o.evidence.raw_payload.sha256) for o in history.observations])


def materialize_historical_outcome(history, selection, store, *, official_adapter=None):
    from .evaluation import Outcome
    document = _historical_document(history, selection, store, official_adapter)
    latest = max((o.evidence for o in history.observations), key=lambda e: e.receipt_key)
    return Outcome(history.race, history.race.election_date.year, tuple(document['candidate_ids']),
                   tuple(document['votes']), latest, store.save(document))


def verify_outcome(outcome, store, *, official_adapter=None):
    require(store is not None and outcome.materialization is not None, 'Missing trusted outcome materialization')
    document = json.loads(store.get(outcome.materialization))
    if document.get('schema') == 'complete-history-outcome-v1':
        history = historical_from_payload(document['history'])
        selection = from_json(json.dumps(document['selection']))
        recomputed = _historical_document(history, selection, store, official_adapter)
        require(document == recomputed and outcome.race == history.race and outcome.votes == history.votes and
                outcome.candidate_ids == tuple(document['candidate_ids']) and outcome.evidence ==
                max((o.evidence for o in history.observations), key=lambda e: e.receipt_key),
                'Scored votes/evidence differ from historical materialization')
        return document
    require(document.get('schema') == 'materialized-outcome-v1', 'Unsupported outcome materialization')
    from .contracts import ArtifactRef
    ref = ArtifactRef(uri=f"sha256:{document['state']}", sha256=document['state'])
    state = from_json(store.get(ref).decode())
    state, recomputed = _outcome_document(state, store, official_adapter)
    require(document == recomputed, 'Outcome materialization binding mismatch')
    require(outcome.race == state.prior.race and outcome.candidate_ids == tuple(document['candidate_ids']) and
            outcome.votes == tuple(document['votes']), 'Scored votes differ from validated materialization')
    latest = max((o.evidence for o in state.observations if o.observation_id in
                  state.partition.selected_observation_ids), key=lambda e: e.receipt_key)
    require(outcome.evidence == latest, 'Outcome evidence differs from materialization')
    return document


def fitted_membership(record, store, *, official_adapter=None):
    """Replay the known B fit and local profiles; reject opaque/calibration inputs.

    This supports exactly the repository's experimental B/C implementations. A
    different estimator/calibrator needs an audited lineage reader, not an empty
    membership declaration. Unknown historical v1 matrices fail closed.
    """
    require(store is not None, 'Verified fitting artifact store required')
    expected_code = b'\n'.join(Path(__file__).with_name(n).read_bytes()
                    for n in ('history.py','baseline.py','prior.py','contracts.py'))
    results = {}
    all_models = {m.model_id: m for m in (*record.models, record.prior.model, record.prior.local_allocation_model)}
    require(len(record.models) == (3 if record.counting_state is not None else 2),
            'Unsupported fitted/calibration model lineage')
    for model in all_models.values():
        manifest = from_json(canonical_json(model.training_manifest))
        require(manifest.training_cutoff <= record.evidence_cutoff, 'Future fitting inputs')
        if model.role == 'COUNTING':
            require(store.get(model.implementation) == Path(__file__).with_name('counting.py').read_bytes() and
                    manifest == record.prior.local_allocation_model.training_manifest and
                    model.parameters == record.configuration and
                    model.feature_version == model.transformation_version == record.run.sampling_procedure,
                    'Unsupported counting/calibration fitting lineage')
            continue
        require(store.get(model.implementation) == expected_code, 'Unsupported fitting implementation')
        transforms = {'feature':'latest same-state/office election party log ratios, log contest turnout, years gap',
            'pseudocount':0.5, 'standardization':'training means/scales; unpenalized intercept',
            'outcomes':'party additive log ratio to last party; log valid contest votes'}
        require(json.loads(store.get(model.feature_version)) == transforms and
                json.loads(store.get(model.transformation_version)) == transforms,
                'Unsupported feature/calibration transformation')
        derived = [i for i in manifest.ordered_inputs if i.kind not in ('RAW','LINEAGE','RACE_DEFINITION')]
        require(len(derived) == 1 and derived[0].kind == 'FEATURES', 'Missing or ambiguous fitting manifest')
        item = derived[0]
        doc = json.loads(store.get(item.artifact))
        histories = []
        params = json.loads(store.get(model.parameters))
        if model.role == 'STATEWIDE_PRIOR':
            require(isinstance(doc, dict) and set(doc) == {'schema','examples'} and
                    doc['schema'] == 'ridge-training-v2', 'Insufficient fitting lineage')
            examples = []
            for x in doc['examples']:
                require(set(x) == {'target','feature_result','feature_cutoff','features'}, 'Invalid fitting example')
                target, feature = historical_from_payload(x['target']), historical_from_payload(x['feature_result'])
                histories.extend((target, feature))
                examples.append(TrainingExample(target, datetime.fromisoformat(x['feature_cutoff']), tuple(x['features']), feature))
            fit = fit_fundamentals(tuple(examples), manifest.evidence, params['penalty'])
            expected = dict(parties=fit.parties, means=fit.means, scales=fit.scales, coefficients=fit.coefficients,
                            paired_residuals=fit.residuals, penalty=fit.penalty)
            require(params == json.loads(json.dumps(expected)), 'Fitted parameters disagree with fitting inputs')
        elif model.role == 'LOCAL_ALLOCATION':
            require(isinstance(doc, dict) and set(doc) == {'schema','profiles','history'} and
                    doc['schema'] == 'local-profiles-v2', 'Insufficient local fitting lineage')
            histories = [historical_from_payload(h) for h in doc['history']]
            require(set(params) == {'profiles','cells','parties','method'}, 'Unsupported local/calibration parameters')
            cells = tuple((u,m) for u in record.prior.local_allocation.unit_ids for m in record.prior.race.ballot_methods)
            profiles = []
            for h in histories:
                rows = {(o.coverage.unit_id,o.coverage.ballot_method):o for o in h.observations}
                require(set(rows) == set(cells), 'Invalid local fitting geography')
                axis = [party_axis(h.race).index(p) for p in params['parties']]
                profiles.append([[rows[cell].votes[j] for j in axis] for cell in cells])
            require(doc['profiles'] == profiles == params['profiles'] and params['cells'] == [list(c) for c in cells],
                    'Local parameters disagree with fitting inputs')
        else:
            raise ValueError('Unsupported calibration fitting lineage')
        require(bool(histories), 'Empty fitting history')
        evidence_ids = set()
        for h in histories:
            require(h.race.office == record.prior.race.office and h.race.election_date < record.prior.race.election_date
                    and h.race.election_date.year < record.prior.race.election_date.year, 'Held-out target in fitting inputs')
            for e in h.evidence:
                require(e.evidence_id in manifest.evidence.selected_ids and e in manifest.evidence.catalog and
                        e.received_at <= item.available_at <= manifest.training_cutoff, 'Fitting availability mismatch')
                evidence_ids.add(e.evidence_id)
            verify_sources(h.race, h.observations, store, official_adapter)
            previous = results.get(h.race.race_id)
            require(previous is None or previous == h, 'Contradictory fitting histories')
            results[h.race.race_id] = h
        require(evidence_ids == set(item.source_evidence_ids), 'Fitting source lineage mismatch')
        require(all(e.evidence_id in evidence_ids for e in manifest.evidence.catalog
                    if e.source_kind == 'OFFICIAL_RESULTS' and e.evidence_id in manifest.evidence.selected_ids),
                'Undeclared outcome-bearing fitting source')
    # Application features can themselves bear historical labels. Verify and
    # include them; an extra opaque FEATURES/calibration artifact is not ignored.
    prior_inputs = record.prior.run.inputs
    expected_train = next(i.artifact for i in record.prior.model.training_manifest.ordered_inputs if i.kind == 'FEATURES')
    feature_items = [i for i in prior_inputs.ordered_inputs if i.kind == 'FEATURES' and i.artifact != expected_train]
    require(len(feature_items) == 1, 'Missing or ambiguous feature/calibration lineage')
    feature_item = feature_items[0]
    feature_doc = json.loads(store.get(feature_item.artifact))
    require(isinstance(feature_doc, dict) and set(feature_doc) == {'schema','features','history'} and
            feature_doc['schema'] == 'statewide-features-v2', 'Unsupported feature/calibration lineage')
    feature_ids = set()
    feature_histories = []
    for payload in feature_doc['history']:
        h = historical_from_payload(payload)
        feature_histories.append(h)
        require(h.race.election_date.year < record.prior.race.election_date.year, 'Held-out target in feature inputs')
        verify_sources(h.race, h.observations, store, official_adapter)
        for e in h.evidence:
            require(e.evidence_id in prior_inputs.evidence.selected_ids and e in prior_inputs.evidence.catalog and
                    e.received_at <= feature_item.available_at <= record.evidence_cutoff, 'Feature availability mismatch')
            feature_ids.add(e.evidence_id)
        require(h.race.race_id not in results or results[h.race.race_id] == h, 'Contradictory feature history')
        results[h.race.race_id] = h
    require(feature_ids == set(feature_item.source_evidence_ids), 'Feature source lineage mismatch')
    features, _ = historical_features(tuple(feature_histories), record.prior.race, prior_inputs.evidence)
    require(feature_doc['features'] == list(features), 'Feature transformation mismatch')
    allowed_allocation = {'RAW','LINEAGE','RACE_DEFINITION','FEATURES','JOINT_DRAWS'}
    allocation = record.prior.allocation_run.inputs
    require(all(i.kind in allowed_allocation for i in allocation.ordered_inputs) and
            [i for i in allocation.ordered_inputs if i.kind == 'FEATURES'] ==
            [i for i in record.prior.local_allocation_model.training_manifest.ordered_inputs if i.kind == 'FEATURES'],
            'Unsupported allocation/calibration application lineage')
    return tuple(results[k] for k in sorted(results))


def reconcile_training(record, training, store, *, official_adapter=None):
    actual = fitted_membership(record, store, official_adapter=official_adapter)
    declared = {o.race.race_id: o for o in training}
    require(len(declared) == len(training) and set(declared) == {h.race.race_id for h in actual},
            'Training membership omission or contradiction')
    for h in actual:
        o = declared[h.race.race_id]
        require(o.race == h.race and o.votes == h.votes and o.candidate_ids == tuple(c.candidate_id for c in h.race.candidates)
                and o.evidence in tuple(row.evidence for row in h.observations), 'Training declaration contradicts fitting inputs')
    return actual
