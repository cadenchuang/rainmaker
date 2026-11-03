"""Synthetic reports test logic; retained official TEST releases test quarantine."""
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from forecast_fixtures import fixture, make_race, evidence, selection, T0
from forecasting.contracts import (
    ArtifactRef, Coverage, ReportingUnit, VoteQuantities, content_id, SourceReconciliation, canonical_json,
)
from forecasting.sources.audit import EVIDENCE, import_retained
from forecasting.sources.minnesota import (
    IdentityMap, INDEX_URL, RESULT_URLS, county_crosswalk, edition_sequence, normalize,
)
from forecasting.sources.store import SourceStore
from forecasting.sources.totals import replay, active_totals, counting_state


def raw_evidence(raw, name='result', order=4, url=RESULT_URLS[0], **kwargs):
    e = evidence(name, 'OFFICIAL_RESULTS', order, at=T0 + timedelta(seconds=order))
    return replace(e, source_id='mn-sos-2026', source_url=url, origin='REAL',
                   quality='VERIFIED_OFFICIAL', published_at=None,
                   raw_payload=ArtifactRef(uri='sha256:'+sha256(raw).hexdigest(), sha256=sha256(raw).hexdigest()), **kwargs)


def mn_fixture():
    r = make_race()
    r = replace(r, state='MN', race_id=r.race_id.replace(':IN:', ':MN:'), reporting_units=(
        ReportingUnit(unit_id='US:MN', state='MN', level='STATE'),
        ReportingUnit(unit_id='US:MN:27001', state='MN', level='COUNTY', parent_id='US:MN'),
        ReportingUnit(unit_id='US:MN:27003', state='MN', level='COUNTY', parent_id='US:MN')))
    mapping = IdentityMap(r, '0102', tuple(((str(i), 'Example '+str(i), 'P'), c.candidate_id)
                          for i, c in enumerate(r.candidates)), (('01','US:MN:27001'),('02','US:MN:27003')))
    raw = b'\n'.join(f'MN;01;;0102;U.S. Senator;;{i};Example {i};;;P;1;2;{v};0;9'.encode()
                     for i,v in enumerate((4,3,2)))
    context = b'Official results November 3, 2026'
    return r, mapping, raw, context


def normalized(raw=None, **kwargs):
    r, mapping, original, context = mn_fixture()
    raw = original if raw is None else raw
    headers = {'Last-Modified': 'Thu, 01 Jan 2026 00:00:00 GMT'}
    obs = normalize(raw, raw_evidence(raw), mapping, context_raw=context,
        context_evidence=raw_evidence(context, name='context', order=3, url=INDEX_URL),
        source_sequence=edition_sequence(headers), edition_headers=headers, **kwargs)
    return r, obs


def test_verified_documented_layout_partial_and_missing():
    r, obs = normalized()
    assert obs[0].votes == (4,3,2)
    assert obs[0].quantities == VoteQuantities(valid_contest_votes=9)
    assert obs[0].reporting_status == 'PARTIAL'
    assert obs[0].source_as_of is None and obs[0].evidence.published_at is None
    assert obs[0].completeness[0].numerator == 1
    # An absent county produces no zero row.
    assert len(obs) == 1
    _, _, raw, _ = mn_fixture()
    _, missing = normalized(b'\n'.join(raw.splitlines()[:2]))
    assert missing[0].votes == (4,3,None)


def test_all_precincts_is_still_partial():
    _, _, raw, _ = mn_fixture()
    _, obs = normalized(raw.replace(b';P;1;2;', b';P;2;2;'))
    assert obs[0].reporting_status == 'PARTIAL'
    assert obs[0].certified is False


def test_zero_reporting_is_unavailable_not_observed_zero():
    _, _, raw, _ = mn_fixture()
    raw = raw.replace(b';P;1;2;', b';P;0;2;')
    for v in (4,3,2):
        raw = raw.replace(f';{v};0;9'.encode(), b';0;0;0')
    _, obs = normalized(raw)
    assert obs[0].votes == (None,None,None)
    assert obs[0].reporting_status == 'NOT_REPORTED'
    assert obs[0].quantities.valid_contest_votes is None


def test_reject_duplicate_line_unmapped_candidate_and_invalid_integer():
    _, _, raw, _ = mn_fixture()
    for bad in (raw+b'\n'+raw.splitlines()[0], raw.replace(b'Example 0',b'Unknown'), raw.replace(b';4;0;9',b';-1;0;9')):
        with pytest.raises((ValueError, KeyError)):
            normalized(bad)


def test_real_test_release_never_becomes_live_result():
    r, mapping, _, _ = mn_fixture()
    raw = (EVIDENCE/'mn-senate-county.raw').read_bytes()
    context = (EVIDENCE/'mn-index.html').read_bytes()
    with pytest.raises(ValueError, match='TEST RESULTS'):
        normalize(raw,raw_evidence(raw),mapping,context_raw=context,
            context_evidence=raw_evidence(context,name='context',order=3,url=INDEX_URL),
            source_sequence=0,edition_headers={})


def test_county_ids_verified_by_names_not_fips_guess():
    mapping = county_crosswalk((EVIDENCE/'mn-counties.raw').read_bytes(), (EVIDENCE/'census-counties.raw').read_bytes())
    assert len(mapping) == 87
    assert mapping['01'] == 'US:MN:27001'
    assert mapping['87'] == 'US:MN:27173'
    with pytest.raises((ValueError,KeyError)):
        county_crosswalk(b'01;Unknown;1', (EVIDENCE/'census-counties.raw').read_bytes())


def test_unknown_and_future_edition_quarantined():
    with pytest.raises(ValueError):
        edition_sequence({})
    r, mapping, raw, context = mn_fixture()
    with pytest.raises(ValueError,match='chronology'):
        normalize(raw,raw_evidence(raw),mapping,context_raw=context,
            context_evidence=raw_evidence(context,name='context',order=3,url=INDEX_URL),
            source_sequence=99,edition_headers={'Last-Modified':'Thu, 01 Jan 2026 00:00:00 GMT'})


def test_retained_receipts_replay_without_new_times(tmp_path):
    store = SourceStore(tmp_path/'sources.sqlite')
    try:
        assert import_retained(store) == 7
        before = store.artifacts('evidence')
        assert import_retained(store) == 7
        assert store.artifacts('evidence') == before
        assert len(store.db.execute('SELECT * FROM source_receipts').fetchall()) == 7
        assert len([e for e in before if e.quality == 'REJECTED']) == 2
        for e in before:
            assert sha256(store.raw(e.raw_payload)).hexdigest() == e.raw_payload.sha256
            assert e.published_at is None
        with pytest.raises(Exception, match='immutable'):
            store.db.execute("DELETE FROM source_receipts")
    finally:
        store.close()


def test_invalid_revisions_preserve_raw_and_diagnostics(tmp_path):
    s=SourceStore(tmp_path/'sources.sqlite')
    try:
        def receipt(n):
            return s.retain('official','snapshot','https://example.invalid',str(n).encode(), received_at=T0+timedelta(seconds=n))
        root=s.envelope(receipt(0))
        with pytest.raises(ValueError, match='roots'):
            s.envelope(receipt(1))
        with pytest.raises(ValueError, match='sequence'):
            s.envelope(receipt(2),revision=2,supersedes_id=root.evidence_id)
        child=s.envelope(receipt(3),revision=1,supersedes_id=root.evidence_id)
        with pytest.raises(ValueError, match='Forked'):
            s.envelope(receipt(4),revision=1,supersedes_id=root.evidence_id)
        with pytest.raises(ValueError, match='Future publication'):
            s.envelope(receipt(5),published_at=T0+timedelta(days=1))
        assert len(s.db.execute('SELECT * FROM source_receipts').fetchall()) == 6
        assert len(s.artifacts('evidence')) == 2
        assert len(s.db.execute('SELECT * FROM source_diagnostics').fetchall()) == 4
        selected=s.selection(T0+timedelta(seconds=8))
        assert selected.selected_ids == (child.evidence_id,)
    finally: s.close()


def test_race_revision_conflict_and_missing_ancestor(tmp_path):
    s=SourceStore(tmp_path/'sources.sqlite'); race=make_race()
    try:
        s.put_race(race); s.put_race(race)
        with pytest.raises(ValueError, match='Conflicting'):
            s.put_race(replace(race,ballot_methods=('IN_PERSON',)))
        with pytest.raises(ValueError):
            s.put_race(replace(race,revision=2,previous_definition_hash=content_id(race)))
        new=replace(race,revision=1,previous_definition_hash=content_id(race))
        s.put_race(new)
        assert len(s.artifacts('race')) == 2
    finally: s.close()


def test_dedup_corrections_and_deterministic_replay():
    race, _, _, first, _, _, _ = fixture()
    first=replace(first,votes=(4,3,0),quantities=VoteQuantities(valid_contest_votes=7))
    second=replace(first,observation_id='second', evidence=evidence('second','OFFICIAL_RESULTS',8,at=first.evidence.received_at+timedelta(seconds=1)),
        source_sequence=first.source_sequence+1,semantics='CORRECTION',base_observation_id=first.observation_id,
        votes=(1,2,0),quantities=VoteQuantities(valid_contest_votes=3))
    a, rows = replay(race,(second,first,first))
    assert a == (first,second) and rows[-1].votes == (1,2,0)
    assert replay(race,(first,second)) == (a,rows)
    with pytest.raises(ValueError,match='Decrease'):
        replay(race,(first,replace(second,semantics='CUMULATIVE',base_observation_id=None)))
    with pytest.raises(ValueError,match='Conflicting'):
        replay(race,(first,replace(first,votes=(5,3,0),quantities=VoteQuantities(valid_contest_votes=8))))
    with pytest.raises(ValueError,match='source sequence'):
        replay(race,(first,replace(second,source_sequence=first.source_sequence)))


def test_multiple_sources_and_overlap_require_explicit_reconciliation():
    race, _, _, a, _, _, _ = fixture()
    b=replace(a,observation_id='other-source',evidence=replace(a.evidence,evidence_id='other-evidence',source_id='other-source',
        source_object_id='other-object',receipt_order=a.evidence.receipt_order+1))
    ev=selection((a.evidence,b.evidence),a.evidence.received_at)
    with pytest.raises(ValueError,match='Overlap'):
        active_totals(race,(a,b),ev,(a.observation_id,b.observation_id),precedence='COUNTY_TOTALS')
    with pytest.raises(ValueError,match='reconciliation'):
        active_totals(race,(a,b),ev,(a.observation_id,),precedence='COUNTY_TOTALS')
    # A winning report's envelope and producer prose are not independent evidence.
    with pytest.raises(ValueError,match='reconciliation document'):
        active_totals(race,(a,b),ev,(a.observation_id,),precedence='EXPLICIT_RECONCILIATION',
            reconciliation='Prefer the first source',reconciliation_evidence_ids=(a.evidence.evidence_id,))
    decision=SourceReconciliation(race_definition=race.ref,selected_observation_hashes=(content_id(a),),
        excluded_observation_hashes=(content_id(b),),issued_at=a.evidence.received_at,basis='Invented independent protocol decision')
    raw=canonical_json(decision).encode()
    authority=replace(evidence('independent-decision','OFFICIAL_RESULTS',99,at=a.evidence.received_at),
        raw_payload=ArtifactRef(uri='fixture://decision',sha256=sha256(raw).hexdigest()))
    ev=selection((a.evidence,b.evidence,authority),a.evidence.received_at)
    _, partition, totals=active_totals(race,(a,b),ev,(a.observation_id,),precedence='EXPLICIT_RECONCILIATION',
        reconciliation=canonical_json(decision),reconciliation_evidence_ids=(authority.evidence_id,))
    assert partition.excluded[0].reason == 'SOURCE_CONFLICT'
    assert len(totals.rows) == 1


def test_full_counting_state_handoff():
    race, _, _, obs, prior, state, _ = fixture()
    result=counting_state(prior,state.observations,state.evidence,(obs.observation_id,),state_id='source-test',precedence='COUNTY_TOTALS')
    assert result.materialized_counts == state.materialized_counts


def test_registry_explicitly_marks_unresearched_states():
    registry=json.loads((EVIDENCE.parent/'registry.json').read_text())
    assert len(registry['states']) == 50
    assert all(s['accepted_live_observations'] == 0 for s in registry['states'])


def test_ingestion_atomic_quarantine_correction_and_reopen(tmp_path):
    from forecasting.sources.minnesota import ingest_receipt
    r, mapping, raw, context = mn_fixture()
    path = tmp_path/'sources.sqlite'
    s = SourceStore(path)
    ctx = s.retain('mn-sos-2026',INDEX_URL,INDEX_URL,context,received_at=T0)
    s.envelope(ctx)
    def retain(raw, seconds):
        from email.utils import format_datetime
        return s.retain('mn-sos-2026',RESULT_URLS[0],RESULT_URLS[0],raw,
            received_at=T0+timedelta(seconds=seconds),metadata={'headers':{'Last-Modified':format_datetime(T0+timedelta(seconds=seconds-1),usegmt=True)}})
    first_id=retain(raw,10)
    first=ingest_receipt(s,first_id,mapping,context_receipt_id=ctx)
    assert len(first) == 1
    assert ingest_receipt(s,first_id,mapping,context_receipt_id=ctx) == first
    decreased=raw.replace(b';4;0;9',b';1;0;6').replace(b';3;0;9',b';3;0;6').replace(b';2;0;9',b';2;0;6')
    bad_id=retain(decreased,20)
    assert ingest_receipt(s,bad_id,mapping,context_receipt_id=ctx) == ()
    assert len(s.artifacts('observation')) == 1
    assert any(e.quality == 'REJECTED' for e in s.artifacts('evidence'))
    assert first[0].evidence.evidence_id not in s.selection(T0+timedelta(seconds=20)).selected_ids
    corrected_id=retain(decreased,30)
    corrected=ingest_receipt(s,corrected_id,mapping,context_receipt_id=ctx,corrections={'US:MN:27001':first[0].observation_id})
    assert corrected[0].semantics == 'CORRECTION'
    assert corrected[0].votes == (1,3,2)
    assert len(s.artifacts('observation')) == 2
    old_evidence=s.artifacts('evidence')
    s.close()
    s=SourceStore(path)
    try:
        assert s.artifacts('evidence') == old_evidence
        assert ingest_receipt(s,corrected_id,mapping,context_receipt_id=ctx) == corrected
        assert replay(r,s.artifacts('observation'))[1][-1].votes == (1,3,2)
    finally: s.close()


def test_source_schema_refusal_and_receipt_identity_conflict(tmp_path):
    import sqlite3
    path=tmp_path/'sources.sqlite'
    s=SourceStore(path)
    rid=s.retain('a','b','https://example.invalid',b'original',received_at=T0,receipt_id='known')
    assert s.retain('a','b','https://example.invalid',b'original',received_at=T0,receipt_id='known') == rid
    with pytest.raises(ValueError,match='receipt identity'):
        s.retain('a','b','https://example.invalid',b'changed',received_at=T0,receipt_id='known')
    s.close()
    db=sqlite3.connect(path)
    db.execute('UPDATE source_schema SET version=9'); db.commit(); db.close()
    with pytest.raises(ValueError,match='schema'):
        SourceStore(path)


def test_disjoint_counties_can_use_distinct_sources_but_cannot_be_hidden():
    race, _, _, a, _, _, _ = fixture()
    counties=[u for u in race.reporting_units if u.level=='COUNTY']
    other=next(u for u in counties if u.unit_id != a.coverage.unit_id)
    b=replace(a,observation_id='disjoint',coverage=Coverage(unit_id=other.unit_id,ballot_method='ALL'),
        evidence=replace(a.evidence,evidence_id='disjoint-evidence',source_id='other-source',
            source_object_id='disjoint',receipt_order=a.evidence.receipt_order+1))
    ev=selection((a.evidence,b.evidence),a.evidence.received_at)
    with pytest.raises(ValueError,match='nonoverlapping'):
        active_totals(race,(a,b),ev,(a.observation_id,),precedence='COUNTY_TOTALS')
    _, partition, totals=active_totals(race,(a,b),ev,(a.observation_id,b.observation_id),precedence='COUNTY_TOTALS',require_complete=True)
    assert not partition.excluded
    assert len(totals.rows) == 2
