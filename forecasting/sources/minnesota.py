"""Minnesota OSS semicolon files, per retained official MediaFileLayout.

A reviewed RaceDefinition and crosswalk are required. Names/party/order codes do
not generate person identities. Test results never become accepted observations.
"""
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from io import StringIO
import re
from urllib.request import urlopen

from forecasting.contracts import (
    CountingObservation, Coverage, VoteQuantities, CompletenessMetric,
    verify_artifact, validate_count, RaceDefinition,
)

INDEX_URL = 'https://electionresults.sos.mn.gov/Select/MediaFiles/Index?ersElectionId=201'
BASE = 'https://electionresultsfiles.sos.mn.gov/20261103/'
RESULT_URLS = (BASE + 'ussenatecty.txt', BASE + 'allracesbycounty.txt',
               BASE + 'ussenate.txt', BASE + 'Governor.txt')


def fetch(store, url):
    """Public allowlist only; retain successful bytes before interpretation.

    HTTP Last-Modified orders file editions, not ballot timing or publication.
    No current live release has yet been accepted. Caller reviews context before
    creating an accepted results envelope; failures are retained as diagnostics.
    """
    if url not in (*RESULT_URLS, INDEX_URL):
        raise ValueError('Unregistered Minnesota URL')
    with urlopen(url, timeout=30) as response:
        if response.geturl() != url:
            raise ValueError('Unreviewed redirect')
        raw = response.read()
        received = datetime.now(timezone.utc)
        metadata = {'status': response.status, 'headers': dict(response.headers)}
    rid = store.retain('mn-sos-2026', url, url, raw, metadata=metadata, received_at=received)
    return rid


def edition_sequence(headers):
    """Timestamp of HTTP resource edition, never a local ingestion counter."""
    value = next((v for k, v in headers.items() if k.lower() == 'last-modified'), None)
    if value is None:
        raise ValueError('Missing source edition chronology')
    stamp = parsedate_to_datetime(value)
    if stamp.utcoffset() is None:
        raise ValueError('Ambiguous source edition timestamp')
    return int(stamp.timestamp())


def rows(raw):
    # OSS files contain legacy Windows text in some candidate names.
    return csv.reader(StringIO(raw.decode('cp1252')), delimiter=';')


def _integer(value):
    if value == '':
        return None
    if re.fullmatch(r'[0-9]+', value) is None:
        raise ValueError('Invalid integer')
    return int(value)


def county_crosswalk(counties_raw, census_raw):
    """Join actual OSS IDs to Census county names; never infer FIPS arithmetically."""
    census = {}
    for row in csv.DictReader(StringIO(census_raw.decode('utf-8-sig')), delimiter='|'):
        if row['STATE'] == 'MN':
            name = row['COUNTYNAME'].removesuffix(' County')
            census[name.casefold()] = 'US:MN:' + row['STATEFP'] + row['COUNTYFP']
    result = {}
    for row in rows(counties_raw):
        if len(row) != 3 or row[0] in result:
            raise ValueError('Invalid/duplicate OSS county')
        result[row[0]] = census[row[1].casefold()]
    if len(result) != 87 or set(result.values()) != set(census.values()):
        raise ValueError('Incomplete/unverified Minnesota county partition')
    return result


@dataclass(frozen=True)
class IdentityMap:
    race: RaceDefinition
    office_id: str
    # (source candidate order code, exact name, exact party) -> stable person ID.
    candidate_lines: tuple[tuple[tuple[str, str, str], str], ...]
    counties: tuple[tuple[str, str], ...]

    def validate(self):
        if self.race.state != 'MN' or self.race.election_date.isoformat() != '2026-11-03':
            raise ValueError('Wrong Minnesota event')
        expected = {'US_SENATE': '0102', 'GOVERNOR': '0331'}
        if expected[self.race.office] != self.office_id:
            raise ValueError('Wrong office identity')
        lines = dict(self.candidate_lines)
        if len(lines) != len(self.candidate_lines) or set(lines.values()) != {c.candidate_id for c in self.race.candidates}:
            raise ValueError('Unmapped/duplicate candidate identity')
        counties = dict(self.counties)
        if len(counties) != len(self.counties) or len(set(counties.values())) != len(counties):
            raise ValueError('Ambiguous county identities')
        if not set(counties.values()) <= {u.unit_id for u in self.race.reporting_units if u.level == 'COUNTY'}:
            raise ValueError('County absent from reviewed definition')


def normalize(raw, evidence, identities, *, context_raw, context_evidence,
              source_sequence, edition_headers, corrections=None):
    """Only independently verified editions are accepted; missing stays missing.

    corrections maps unit_id -> immediately previous observation ID. The caller
    must retain official correction authority; a decrease alone never creates it.
    Source files provide no publication instant: published_at stays None. Caller
    obtains source_sequence from retained HTTP edition headers, never receipt order.
    """
    identities.validate()
    race = identities.race
    if evidence.source_url not in RESULT_URLS or evidence.source_id != 'mn-sos-2026':
        raise ValueError('Wrong results source')
    verify_artifact(evidence.raw_payload, raw)
    verify_artifact(context_evidence.raw_payload, context_raw)
    if (context_evidence.source_url != INDEX_URL or context_evidence.source_id != 'mn-sos-2026'
            or context_evidence.quality != 'VERIFIED_OFFICIAL'):
        raise ValueError('Unverified election context')
    context = context_raw.decode('utf-8')
    if re.search(r'test\s+results', context, re.I):
        raise ValueError('Official site labels this release TEST RESULTS; quarantined')
    if 'November 3, 2026' not in context:
        raise ValueError('Wrong election context')
    if context_evidence.received_at > evidence.received_at or (evidence.received_at - context_evidence.received_at).total_seconds() > 900:
        raise ValueError('Election context must precede results within 15 minutes')
    if source_sequence != edition_sequence(edition_headers) or source_sequence > int(evidence.received_at.timestamp()):
        raise ValueError('Unverified/future file edition chronology')
    if evidence.quality != 'VERIFIED_OFFICIAL' or source_sequence < 0:
        raise ValueError('Unverified results/chronology')
    lines, counties, groups = dict(identities.candidate_lines), dict(identities.counties), {}
    office_name = 'U.S. Senator' if race.office == 'US_SENATE' else 'Governor & Lt Governor'
    for row in rows(raw):
        if len(row) != 16:
            raise ValueError('Unexpected OSS result layout')
        if row[3] != identities.office_id:
            continue
        if row[0] != 'MN' or row[2] or row[5] or row[4] != office_name:
            raise ValueError('Wrong event/office/geographic scope')
        unit = counties[row[1]] if row[1] else 'US:MN'
        cid = lines[(row[6], row[7], row[10])]
        candidate = next(c for c in race.candidates if c.candidate_id == cid)
        if row[6] == '9901' and candidate.kind != 'WRITE_IN_AGGREGATE':
            raise ValueError('Official write-in aggregate must retain aggregate identity')
        reporting, expected, votes, total = map(_integer, (row[11], row[12], row[13], row[15]))
        group = groups.setdefault(unit, {'votes': {}, 'meta': (reporting, expected, total)})
        if group['meta'] != (reporting, expected, total):
            raise ValueError('Inconsistent office accounting')
        # Fusion lines may map to the same person; duplicate raw lines are rejected.
        key = (row[6], row[7], row[10])
        if key in group['votes']:
            raise ValueError('Duplicate ballot line')
        group['votes'][key] = (cid, votes)
    if not groups:
        raise ValueError('Requested office absent')
    observations = []
    for unit, group in sorted(groups.items()):
        reporting, expected, total = group['meta']
        candidate_votes = []
        for candidate in race.candidates:
            entries = [v for cid, v in group['votes'].values() if cid == candidate.candidate_id]
            mapped_lines = sum(cid == candidate.candidate_id for cid in lines.values())
            candidate_votes.append(sum(entries) if len(entries) == mapped_lines and all(v is not None for v in entries) else None)
        # With zero reporting units these are pre-report placeholders, not observed zeros.
        if reporting == 0:
            if any(v not in (0, None) for v in candidate_votes) or total not in (0, None):
                raise ValueError('Votes with zero reporting precincts')
            candidate_votes, total, status = [None] * len(candidate_votes), None, 'NOT_REPORTED'
        else:
            # All precincts reporting is not final ballot completeness/certification.
            status = 'PARTIAL' if reporting is not None else 'UNKNOWN'
        base = (corrections or {}).get(unit)
        oid = sha256(f'{evidence.evidence_id}:{race.ref.definition_hash}:{unit}'.encode()).hexdigest()
        obs = CountingObservation(observation_id=oid, race_id=race.race_id, race_definition=race.ref,
            evidence=evidence, coverage=Coverage(unit_id=unit, ballot_method='ALL'),
            candidate_ids=tuple(c.candidate_id for c in race.candidates), votes=tuple(candidate_votes),
            quantities=VoteQuantities(valid_contest_votes=total), reporting_status=status,
            completeness=(CompletenessMetric(denominator_name='REPORTING_UNITS', numerator=reporting,
                denominator=expected, basis='EXACT'),), source_sequence=source_sequence,
            source_as_of=None, semantics='CORRECTION' if base else 'CUMULATIVE', base_observation_id=base)
        validate_count(race, obs)
        observations.append(obs)
    return tuple(observations)


def ingest_receipt(store, receipt_id, identities, *, context_receipt_id, corrections=None):
    """Normalize already durable bytes; preserve failed editions as rejected lineage.

    The election context must be fetched before the results. A correction is an
    explicit reviewed producer assertion bound to the retained official edition.
    Returns () on quarantined editions, with a durable diagnostic explaining why.
    Replaying the same receipt returns the exact saved observations and times.
    """
    from dataclasses import replace
    row = store.db.execute('SELECT source_id,object_id,metadata FROM source_receipts WHERE receipt_id=?', (receipt_id,)).fetchone()
    if row is None:
        raise ValueError('Missing durable result receipt')
    source_id, obj, metadata = row
    previous = sorted((e for e in store.artifacts('evidence') if (e.source_id,e.source_object_id) == (source_id,obj)), key=lambda e:e.receipt_key)
    existing = next((e for e in previous if e.evidence_id == f'receipt:{receipt_id}'), None)
    if existing is not None:
        return tuple(o for o in store.artifacts('observation') if o.evidence == existing and o.race_definition == identities.race.ref)
    options = {'revision':previous[-1].revision+1, 'supersedes_id':previous[-1].evidence_id} if previous else {}
    # Publication is not exposed by OSS; HTTP Last-Modified remains in metadata.
    candidate = store.candidate_envelope(receipt_id, **options)
    try:
        context = next(e for e in store.artifacts('evidence') if e.evidence_id == f'receipt:{context_receipt_id}')
        import json
        headers = json.loads(metadata).get('headers', {})
        observations = normalize(store.raw(candidate.raw_payload), candidate, identities,
            context_raw=store.raw(context.raw_payload), context_evidence=context,
            source_sequence=edition_sequence(headers), edition_headers=headers, corrections=corrections)
        store.accept_bundle(identities.race, candidate, observations)
        return observations
    except (ValueError, KeyError, StopIteration, UnicodeError) as exc:
        # Failed reports never masquerade as accepted evidence. Invalid envelope
        # chronology may itself fail; original bytes and diagnostic still survive.
        store.diagnostic(receipt_id, f'Quarantined: {type(exc).__name__}: {exc}')
        store.envelope(receipt_id, quality='REJECTED', **options)
        return ()
