"""Independent source conflict policy; ALL election/official-adapter data invented."""
from dataclasses import fields, replace
from datetime import timedelta
import json

import pytest

from test_evaluation_integrity_integration import official_enum_state
from forecasting.contracts import (
    CountingState, ExcludedObservation, MaterializedCounts, SourceReconciliation,
    VoteQuantities, Coverage, canonical_json, content_id, from_json, materialize_counts,
)
from forecasting.sources.totals import counting_state
from forecasting.evaluation_integrity import materialize_outcome, OfficialResult
from forecasting.evaluation import Outcome, score
from forecasting.integration import prior_record, evaluate_record, evaluation_run
from forecasting.counting import forecast, replay, CountingConfig
from forecasting.synthetic_slice import prepare, instant
from forecast_fixtures import selection


def contender(state, store, *, votes=(1,2000,0), snapshot=None):
    a = state.observations[0]
    raw = json.dumps(dict(fixture_only=True,release='FINAL',votes=votes)).encode()
    e = replace(a.evidence,evidence_id='fixture:source-B',source_id='fixture:source-B',
        source_object_id='fixture:source-B-count',source_url='https://fixture.invalid/source-B',
        received_at=a.evidence.received_at+timedelta(seconds=1),published_at=None,receipt_order=200,raw_payload=store.put(raw))
    return replace(a,observation_id='fixture:source-B-report',evidence=e,source_sequence=1,
        source_as_of=a.source_as_of if snapshot is None else snapshot,votes=votes,
        quantities=VoteQuantities(valid_contest_votes=sum(votes)))


def candidate_state(base, observations, *, reason='OVERLAPPING_REDUNDANT', evidence=None, decision=None):
    a = observations[0]
    at = max(o.evidence.received_at for o in observations)
    ev = evidence or selection((*base.evidence.catalog,*(o.evidence for o in observations)),at)
    p = replace(base.partition,selected_observation_ids=(a.observation_id,),
        excluded=tuple(ExcludedObservation(observation_id=o.observation_id,reason=reason,
            explanation='Caller assertion, never proof') for o in observations[1:]),
        precedence='EXPLICIT_RECONCILIATION' if decision else 'STATE_TOTAL',
        reconciliation=canonical_json(decision[0]) if decision else 'Prefer A',
        reconciliation_evidence_ids=(decision[1].evidence_id,) if decision else ())
    return dict(evidence=ev,evidence_cutoff=ev.cutoff,observations=observations,partition=p,
        materialized_counts=MaterializedCounts(race_definition=base.prior.race.ref,
            rows=(materialize_counts(base.prior.race,observations)[0],)))


def decision_for(base, store, observations, *, origin='REAL'):
    issued = max(o.evidence.received_at for o in observations)+timedelta(seconds=1)
    doc = SourceReconciliation(race_definition=base.prior.race.ref,
        selected_observation_hashes=(content_id(observations[0]),),
        excluded_observation_hashes=tuple(content_id(o) for o in observations[1:]),
        issued_at=issued,basis='INVENTED independent authority protocol: replace inconsistent secondary edition with A')
    template = observations[0].evidence
    e = replace(template,evidence_id='fixture:decision',source_id='fixture:independent-authority',
        source_object_id='fixture:independent-decision',source_url='https://fixture.invalid/decision',
        published_at=issued,received_at=issued,receipt_order=300,origin=origin,
        quality='SYNTHETIC' if origin=='SYNTHETIC' else 'VERIFIED_OFFICIAL',
        raw_payload=store.put(canonical_json(doc).encode()))
    return doc,e


class ProtocolAdapter:
    """Fixture decoder uses independently retained expected rows, never caller votes."""
    def __init__(self, base, observations, decision=None):
        self.base=base
        self.rows={o.evidence.evidence_id:o for o in observations}
        self.decision=decision
        self.source_urls=dict(base.source_urls)
        for o in observations:
            self.source_urls[o.evidence.source_id]=(*self.source_urls.get(o.evidence.source_id,()),o.evidence.source_url)
        if decision:
            self.source_urls[decision[1].source_id]=(decision[1].source_url,)
    def definition(self,race,rules,roster):
        return self.base.definition(race,rules,roster)
    def result(self,race,submitted,raw):
        expected=self.rows[submitted.evidence.evidence_id]
        doc=json.loads(raw)
        return OfficialResult(replace(expected,votes=tuple(doc['votes'])),True,doc['release'])
    def reconciliation(self,submitted,evidence,raw):
        assert self.decision and evidence==self.decision[1]
        return from_json(raw.decode())


@pytest.mark.parametrize('reason',['OVERLAPPING_REDUNDANT','SOURCE_CONFLICT','SUPERSEDED'])
def test_exact_attack_and_every_relabel_rejected_at_canonical_boundary(official_enum_state,reason):
    base,store,_=official_enum_state
    a=base.observations[0]
    b=contender(base,store)
    kwargs=candidate_state(base,(a,b),reason=reason)
    with pytest.raises(ValueError,match='classification|reconciliation|superseded'):
        replace(base,**kwargs)
    with pytest.raises(ValueError,match='reconciliation'):
        counting_state(base.prior,(a,b),kwargs['evidence'],(a.observation_id,),
            state_id='normal-producer',precedence='STATE_TOTAL')


def test_materialization_and_scoring_reject_forged_state_and_serialized_attack(official_enum_state):
    base,store,adapter=official_enum_state
    a=base.observations[0]; b=contender(base,store)
    kwargs=candidate_state(base,(a,b))
    # Simulate an unchecked caller bypassing the frozen dataclass constructor.
    forged=object.__new__(CountingState)
    for f in fields(base):
        object.__setattr__(forged,f.name,kwargs.get(f.name,getattr(base,f.name)))
    record=prior_record(base.prior,store,created_at=base.prior.run.created_at)
    binding=store.save(dict(schema='materialized-outcome-v2',state=store.save(forged).sha256))
    label=Outcome(base.prior.race,2026,a.candidate_ids,a.votes,a.evidence,binding)
    for action in (
        lambda: from_json(canonical_json(forged)),
        lambda: materialize_outcome(forged,store,official_adapter=adapter),
        lambda: score(record,label,a.candidate_ids[:2],store=store,official_adapter=adapter),
        lambda: evaluate_record(record,label,store,training=(),official_adapter=adapter),
    ):
        with pytest.raises(ValueError,match='classification'):
            action()


def test_equivalent_independent_sources_roundtrip_and_authenticated_scoring(official_enum_state):
    base,store,adapter=official_enum_state
    a=base.observations[0]; b=contender(base,store,votes=a.votes)
    state=replace(base,**candidate_state(base,(a,b)))
    restored=from_json(canonical_json(state))
    assert content_id(restored)==content_id(state) and len(restored.observations)==2
    label=materialize_outcome(restored,store,official_adapter=ProtocolAdapter(adapter,(a,b)))
    record=prior_record(base.prior,store,created_at=base.prior.run.created_at)
    assert score(record,label,a.candidate_ids[:2],store=store,official_adapter=ProtocolAdapter(adapter,(a,b)))['brier']==0
    doc=json.loads(store.get(label.materialization))
    assert {e['id'] for e in doc['comparison_evidence']}=={a.evidence.evidence_id,b.evidence.evidence_id}
    # Equivalent normalized values cannot mask different retained source bytes.
    dishonest=replace(b,votes=(1,2000,0),quantities=VoteQuantities(valid_contest_votes=2001))
    with pytest.raises(ValueError,match='official result|denominator mismatch'):
        materialize_outcome(state,store,official_adapter=ProtocolAdapter(adapter,(a,dishonest)))


def test_conflicting_sources_accept_only_independent_bound_authenticated_decision(official_enum_state):
    base,store,adapter=official_enum_state
    a=base.observations[0]; b=contender(base,store)
    decision=decision_for(base,store,(a,b))
    ev=selection((*base.evidence.catalog,b.evidence,decision[1]),decision[1].received_at)
    kwargs=candidate_state(base,(a,b),reason='SOURCE_CONFLICT',evidence=ev,decision=decision)
    state=replace(base,**kwargs)
    configured=ProtocolAdapter(adapter,(a,b),decision)
    produced=counting_state(base.prior,(a,b),ev,(a.observation_id,),state_id='independently-reconciled',
        precedence='EXPLICIT_RECONCILIATION',reconciliation=canonical_json(decision[0]),
        reconciliation_evidence_ids=(decision[1].evidence_id,))
    assert produced.partition.excluded[0].reason=='SOURCE_CONFLICT'
    assert materialize_outcome(produced,store,official_adapter=configured).votes==a.votes
    label=materialize_outcome(state,store,official_adapter=configured)
    assert label.votes==a.votes
    assert content_id(from_json(canonical_json(state)))==content_id(state)
    record=prior_record(base.prior,store,created_at=base.prior.run.created_at)
    assert score(record,label,a.candidate_ids[:2],store=store,official_adapter=configured)['brier']==0
    run=evaluation_run(record,label,store,ev,created_at=ev.cutoff,official_adapter=configured)
    bound=next(i for i in run.inputs.ordered_inputs if i.artifact==label.materialization)
    assert bound.available_at==decision[1].received_at
    assert set(bound.source_evidence_ids)=={a.evidence.evidence_id,b.evidence.evidence_id,decision[1].evidence_id}
    with pytest.raises(ValueError,match='authentication unavailable'):
        materialize_outcome(state,store,official_adapter=ProtocolAdapter(adapter,(a,b)))
    # Prose plus the winning report's hash, or a stale decision, is not authority.
    with pytest.raises(ValueError,match='document'):
        replace(state,partition=replace(state.partition,reconciliation='Trust A'))
    with pytest.raises(ValueError,match='Independent applicable'):
        replace(state,partition=replace(state.partition,reconciliation_evidence_ids=(a.evidence.evidence_id,)))
    with pytest.raises(ValueError,match='bind exact'):
        changed=replace(decision[0],excluded_observation_hashes=(content_id(a),))
        replace(state,partition=replace(state.partition,reconciliation=canonical_json(changed)))


def test_complete_county_partition_is_verified_redundancy_in_either_direction(official_enum_state):
    base,store,adapter=official_enum_state
    a=base.observations[0]
    b=replace(contender(base,store,votes=(400,300,60)),
        coverage=Coverage(unit_id=base.prior.local_allocation.unit_ids[0],ballot_method='ALL'))
    c=replace(contender(base,store,votes=(500,350,100)),observation_id='fixture:source-C-report',
        coverage=Coverage(unit_id=base.prior.local_allocation.unit_ids[1],ballot_method='ALL'))
    c=replace(c,evidence=replace(c.evidence,evidence_id='fixture:source-C',source_id='fixture:source-C',
        source_object_id='fixture:source-C-count',source_url='https://fixture.invalid/source-C',receipt_order=201,
        received_at=c.evidence.received_at+timedelta(seconds=1)))
    state=replace(base,**candidate_state(base,(a,b,c)))
    configured=ProtocolAdapter(adapter,(a,b,c))
    assert materialize_outcome(state,store,official_adapter=configured).votes==a.votes
    # The other legitimate partition is the disjoint pair of complete counties.
    partition=replace(state.partition,selected_observation_ids=(b.observation_id,c.observation_id),
        excluded=(ExcludedObservation(observation_id=a.observation_id,reason='OVERLAPPING_REDUNDANT',
            explanation='Verified county conservation, not asserted precedence'),),precedence='COUNTY_TOTALS')
    rows={r.observation_id:r for r in materialize_counts(base.prior.race,state.observations)}
    state=replace(state,partition=partition,materialized_counts=MaterializedCounts(
        race_definition=base.prior.race.ref,rows=(rows[b.observation_id],rows[c.observation_id])))
    assert materialize_outcome(state,store,official_adapter=configured).votes==a.votes


def test_synthetic_contender_cannot_be_reclassified_into_real_equivalence(official_enum_state):
    base,store,_=official_enum_state
    a=base.observations[0]
    b=contender(base,store,votes=a.votes)
    b=replace(b,evidence=replace(b.evidence,origin='SYNTHETIC',quality='SYNTHETIC'))
    with pytest.raises(ValueError,match='classification'):
        replace(base,**candidate_state(base,(a,b)))
    decision=decision_for(base,store,(a,b),origin='SYNTHETIC')
    ev=selection((*base.evidence.catalog,b.evidence,decision[1]),decision[1].received_at)
    with pytest.raises(ValueError,match='Independent applicable'):
        replace(base,**candidate_state(base,(a,b),reason='SOURCE_CONFLICT',evidence=ev,decision=decision))


def test_unknown_or_different_snapshot_does_not_prove_equivalence(official_enum_state):
    base,store,_=official_enum_state
    a=base.observations[0]
    for snapshot in (a.source_as_of+timedelta(seconds=1),None):
        b=contender(base,store,votes=a.votes)
        b=replace(b,source_as_of=snapshot)
        with pytest.raises(ValueError,match='classification'):
            replace(base,**candidate_state(base,(a,b)))


def test_synthetic_reconciliation_replay_corrections_and_no_provenance_promotion(tmp_path):
    case=prepare(tmp_path,'US_SENATE')
    try:
        a=case.report('source-A',(900,650,160),instant(2026,11,4),status='COMPLETE')
        raw=json.loads(case.sources.raw(a.evidence.raw_payload))
        raw.update(fixture='source-B',source_id='synthetic:vertical-slice:secondary',
                   received_at=(a.evidence.received_at+timedelta(seconds=1)).isoformat())
        raw['payload']['votes']=[1,2000,0]
        b=replace(a,observation_id='synthetic:source-B',votes=(1,2000,0),quantities=VoteQuantities(valid_contest_votes=2001),
            evidence=replace(a.evidence,evidence_id='synthetic:source-B',source_id=raw['source_id'],
                source_object_id='source-B',source_url='fixture://source-B',receipt_order=200,
                received_at=a.evidence.received_at+timedelta(seconds=1),
                raw_payload=case.artifacts.put(json.dumps(raw,sort_keys=True,separators=(',',':')).encode())))
        base=case.state((a,),(a.observation_id,),a.evidence.received_at,complete=True)
        decision=decision_for(base,case.artifacts,(a,b),origin='SYNTHETIC')
        ev=selection((*base.evidence.catalog,b.evidence,decision[1]),decision[1].received_at)
        state=replace(base,**candidate_state(base,(a,b),reason='SOURCE_CONFLICT',evidence=ev,decision=decision))
        label=materialize_outcome(state,case.artifacts)
        assert label.origin=='SYNTHETIC'
        result=forecast(case.prior,state,created_at=state.evidence_cutoff,config=CountingConfig(draws=8))
        assert result.record.origin=='SYNTHETIC' and result.record.quality=='SYNTHETIC'
        assert canonical_json(replay(result.record,result.artifacts).record)==canonical_json(result.record)
        with pytest.raises(ValueError):
            replace(result.record,origin='REAL',quality='EVALUATED',eligibility='EVALUATED_FORECAST')
        # Corrected same-source snapshots are not compared to their obsolete base.
        c=case.report('source-A-correction',(800,650,160),a.evidence.received_at+timedelta(minutes=1),
            status='COMPLETE',sequence=2,semantics='CORRECTION',base=a.observation_id)
        corrected=case.state((a,c),(c.observation_id,),c.evidence.received_at,complete=True)
        assert corrected.partition.excluded[0].reason=='SUPERSEDED'
        assert materialize_outcome(corrected,case.artifacts).votes==c.votes
    finally:
        case.sources.close()
