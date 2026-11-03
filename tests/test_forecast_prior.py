"""Fictional elections only: these fixtures are not a historical dataset."""
from dataclasses import replace
from datetime import date, datetime, timezone
from math import fsum
import random

import pytest
from forecast_fixtures import make_race, evidence, selection, ASSET_BYTES
from forecasting.contracts import (CountingObservation, Coverage, VoteQuantities,
    canonical_json, content_id, from_json, validate_local_allocation)
from forecasting.history import HistoricalResult, historical_features, training_example
from forecasting.baseline import fit_fundamentals, polling_benchmark, temporal_validation
from forecasting.prior import ArtifactStore, apportion, build_prior


def instant(year, month=1, day=1):
    return datetime(year,month,day,tzinfo=timezone.utc)


def dataset(office='US_SENATE'):
    template=make_race(office)
    histories=[]; catalog=[]
    for i,year in enumerate((2002,2006,2010,2014,2018,2022)):
        race=replace(template, election_date=date(year,11,3),
            race_id=template.race_id.replace('2026',str(year)),
            rules_evidence=evidence(f'{office}-{year}-rules','OFFICIAL_RULES',i*10,instant(year)),
            definition_evidence=evidence(f'{office}-{year}-roster','OFFICIAL_ROSTER',i*10+1,instant(year)))
        rows=[]
        for j,(unit,method) in enumerate((u.unit_id,m) for u in race.reporting_units if u.level=='COUNTY' for m in race.ballot_methods):
            votes=(100+i*13+j*7,80+i*8+j*4,20+i*2+j)
            rows.append(CountingObservation(observation_id=f'{office}-{year}-{j}',race_id=race.race_id,
                race_definition=race.ref,evidence=evidence(f'{office}-{year}-{j}','OFFICIAL_RESULTS',i*10+2+j,instant(year,11,5)),
                coverage=Coverage(unit_id=unit,ballot_method=method),candidate_ids=tuple(c.candidate_id for c in race.candidates),
                votes=votes,quantities=VoteQuantities(valid_contest_votes=sum(votes)),reporting_status='COMPLETE',
                completeness=(),source_sequence=1,source_as_of=instant(year,11,5),semantics='CUMULATIVE'))
        result=HistoricalResult(race,tuple(rows)); histories.append(result); catalog.extend(result.evidence)
    target=template; catalog.extend((target.rules_evidence,target.definition_evidence))
    info=selection(tuple(catalog),instant(2026))
    examples=[]
    for outcome in histories[1:]:
        cutoff=instant(outcome.race.election_date.year)
        past=selection(tuple(e for e in catalog if e.received_at <= cutoff),cutoff)
        examples.append(training_example(histories,outcome,past))
    return target,tuple(histories),tuple(examples),info


@pytest.mark.parametrize('office',['US_SENATE','GOVERNOR'])
def test_prior_conservation_replay_and_cas(tmp_path,office):
    race,history,examples,info=dataset(office)
    store=ArtifactStore(tmp_path)
    for e in info.catalog:
        store.put(ASSET_BYTES[e.raw_payload.sha256])
    kwargs=dict(race=race,history=history,examples=examples,selection=info,store=store,
        fitted_at=instant(2026),created_at=instant(2026),seed=42,penalty=2.0,draw_count=25,residual_count=4)
    prior=build_prior(**kwargs)
    assert canonical_json(prior)==canonical_json(build_prior(**kwargs))
    assert from_json(canonical_json(prior))==prior
    validate_local_allocation(race,prior.simulation,prior.local_allocation,prior.allocation_manifest,prior.allocation_run)
    assert prior.model.origin=='SYNTHETIC' and prior.model.status=='EXPERIMENTAL'
    assert prior.local_allocation_model.evaluation_report is None
    assert len({d.cell_votes for d in prior.local_allocation.draws})>1
    for local in prior.local_allocation.draws:
        parent=prior.draws[local.statewide_draw_index]
        totals=tuple(sum(r[j] for r in local.cell_votes) for j in range(3))
        assert sum(totals)==parent.total_valid_votes
        assert tuple(v/parent.total_valid_votes for v in totals)==parent.shares
    assert store.get(prior.model.parameters)
    assert store.get(prior.allocation_manifest.payload)==canonical_json(prior.local_allocation).encode()
    assert content_id(build_prior(**{**kwargs,'seed':43}))!=content_id(prior)
    assert all(sum(d.shares)==pytest.approx(1) for d in prior.draws)


def test_point_in_time_and_training_leakage():
    race,history,examples,info=dataset()
    # Delayed receipts make a result unavailable, regardless of its election year.
    latest=history[-1]
    delayed=replace(latest,observations=tuple(replace(o,evidence=replace(o.evidence,
        received_at=instant(2027),published_at=instant(2027))) for o in latest.observations))
    features,source=historical_features((*history[:-1],delayed),race,info)
    assert source==history[-2]
    assert features!=historical_features(history,race,info)[0]
    poisoned=replace(examples[0],features=tuple(v+1 for v in examples[0].features))
    with pytest.raises(ValueError,match='Feature values'):
        fit_fundamentals((poisoned,*examples[1:]),info,1.0)
    future=replace(examples[0],feature_cutoff=instant(2027))
    with pytest.raises(ValueError,match='Future'):
        fit_fundamentals((future,*examples[1:]),info,1.0)
    with pytest.raises(ValueError,match='precedes|precede'):
        historical_features(history,race,replace(info,cutoff=instant(2026,11,4)))


def test_apportion_properties_and_empirical_residuals():
    rnd=random.Random(19)
    for total in (0,1,2,100,1000001):
        for _ in range(20):
            w=[rnd.random() for _ in range(9)]
            values=apportion(total,w)
            assert sum(values)==total and min(values)>=0
            assert max(abs(v-total*x/sum(w)) for v,x in zip(values,w))<1
    _,_,examples,info=dataset()
    fit=fit_fundamentals(examples,info,2.0)
    assert all(abs(fsum(r[j] for r in fit.residuals))<1e-10 for j in range(3))
    assert all(len(r)==3 for r in fit.residuals)
    report=temporal_validation(examples,info,2.0)
    assert report['held_out_contests']==2
    assert report['share_mae']>=0
    assert all(s['training_contests']>=3 for s in report['scores'])


def test_poll_benchmark_requires_archive():
    race,_,_,info=dataset()
    with pytest.raises(ValueError,match='archive'):
        polling_benchmark(race,(),info)
    from forecast_fixtures import fixture
    race,_,poll,_,prior,_,_=fixture()
    poll=replace(poll,shares=(0.4,0.3,0.2),other_share=0.0)
    info=selection((race.rules_evidence,race.definition_evidence,poll.evidence),prior.evidence_cutoff)
    assert polling_benchmark(race,(poll,),info)==pytest.approx((4/9,3/9,2/9))
    with pytest.raises(ValueError,match='Duplicate'):
        polling_benchmark(race,(poll,poll),info)
    with pytest.raises(ValueError,match='unmapped'):
        polling_benchmark(race,(replace(poll,shares=(0.3,0.3,0.2),other_share=0.1),),info)


def test_refuse_incomplete_geography_and_missing_raw(tmp_path):
    race,history,examples,info=dataset()
    with pytest.raises(ValueError):
        HistoricalResult(history[0].race,history[0].observations[:-1])
    store=ArtifactStore(tmp_path)
    args=dict(race=race,history=history,examples=examples,selection=info,store=store,
        fitted_at=instant(2026),created_at=instant(2026),seed=1,penalty=1.0,draw_count=2,residual_count=2)
    with pytest.raises(FileNotFoundError):
        build_prior(**args)
    for e in info.catalog:
        store.put(ASSET_BYTES[e.raw_payload.sha256])
    with pytest.raises(ValueError,match='Exhaustive'):
        build_prior(**{**args,'race':replace(race,county_partition_complete=False)})


def test_rejected_latest_revision_does_not_revive_history():
    race,history,_,info=dataset()
    original=history[-1].observations[0].evidence
    old=replace(original,origin='REAL',quality='VERIFIED_OFFICIAL')
    last=replace(history[-1],observations=(replace(history[-1].observations[0],evidence=old),
                                         *history[-1].observations[1:]))
    history=(*history[:-1],last)
    info=selection(tuple(old if e==original else e for e in info.catalog),info.cutoff)
    # Test enum branch only: all election values and payloads remain fictional.
    rejected=replace(old,evidence_id=old.evidence_id+':rejected',origin='REAL',quality='REJECTED',
        revision=1,supersedes_id=old.evidence_id,received_at=instant(2025),published_at=instant(2025),receipt_order=999)
    revised=selection((*info.catalog,rejected),info.cutoff)
    _,source=historical_features(history,race,revised)
    assert source==history[-2]


def test_reproduce_in_fresh_process(tmp_path):
    import subprocess
    import sys
    code='''
import sys
sys.path.insert(0, 'tests')
from test_forecast_prior import dataset, instant
from forecast_fixtures import ASSET_BYTES
from forecasting.prior import ArtifactStore, build_prior
from forecasting.contracts import content_id
race,history,examples,info=dataset()
store=ArtifactStore(sys.argv[1])
for e in info.catalog: store.put(ASSET_BYTES[e.raw_payload.sha256])
p=build_prior(race=race,history=history,examples=examples,selection=info,store=store,
    fitted_at=instant(2026),created_at=instant(2026),seed=23,penalty=1.0,draw_count=10,residual_count=3)
print(content_id(p))
'''
    outputs=[subprocess.check_output([sys.executable,'-c',code,str(tmp_path/str(i))],text=True).strip()
             for i in range(2)]
    assert outputs[0]==outputs[1]
