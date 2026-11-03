"""Append-only source tables in a caller-owned SQLite database.

Uses the journal's WAL/FULL conventions without changing its tables/user_version.
All writes are synchronous; one connection belongs to one caller thread.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4

from forecasting.contracts import (
    ArtifactRef, EvidenceEnvelope, EvidenceSelection, RaceDefinition,
    canonical_json, from_json, validate_race_revision,
)


class SourceStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        try:
            if self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise ValueError('Sources require local WAL storage')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            with self.db:
                self.db.execute('CREATE TABLE IF NOT EXISTS source_schema (version INTEGER PRIMARY KEY)')
                versions = self.db.execute('SELECT version FROM source_schema').fetchall()
                if versions and versions != [(1,)]:
                    raise ValueError('Unsupported source schema')
                self.db.execute('INSERT OR IGNORE INTO source_schema VALUES (1)')
                self.db.execute('CREATE TABLE IF NOT EXISTS source_blobs (sha256 TEXT PRIMARY KEY, raw BLOB NOT NULL)')
                self.db.execute('''CREATE TABLE IF NOT EXISTS source_receipts (
                    ordinal INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT UNIQUE NOT NULL,
                    source_id TEXT NOT NULL, object_id TEXT NOT NULL, url TEXT NOT NULL,
                    received_at TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES source_blobs(sha256),
                    metadata TEXT NOT NULL)''')
                self.db.execute('CREATE TABLE IF NOT EXISTS source_artifacts (kind TEXT, identity TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind, identity))')
                self.db.execute('CREATE TABLE IF NOT EXISTS source_diagnostics (ordinal INTEGER PRIMARY KEY AUTOINCREMENT, receipt_id TEXT NOT NULL, reason TEXT NOT NULL)')
                for table in ('source_blobs', 'source_receipts', 'source_artifacts', 'source_diagnostics'):
                    for action in ('UPDATE', 'DELETE'):
                        self.db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table}
                            BEGIN SELECT RAISE(ABORT, 'immutable source history'); END''')
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def _put(self, kind, identity, value):
        payload = canonical_json(value)
        old = self.db.execute('SELECT payload FROM source_artifacts WHERE kind=? AND identity=?', (kind, identity)).fetchone()
        if old is not None and old[0] != payload:
            raise ValueError('Conflicting immutable identity')
        self.db.execute('INSERT OR IGNORE INTO source_artifacts VALUES (?,?,?)', (kind, identity, payload))

    def artifacts(self, kind):
        return tuple(from_json(row[0]) for row in self.db.execute(
            'SELECT payload FROM source_artifacts WHERE kind=? ORDER BY identity', (kind,)))

    def retain(self, source_id, object_id, url, raw, *, metadata=None,
               received_at=None, receipt_id=None):
        """Retain BEFORE parsing. Explicit times are for importing retained receipts only.

        Network callers sample the clock immediately after response.read(), then call
        this method. Replaying must use the original receipt_id and received_at.
        """
        if type(raw) is not bytes:
            raise TypeError('Original bytes required')
        at = received_at or datetime.now(timezone.utc)
        if at.utcoffset() is None:
            raise ValueError('Naive receipt')
        digest = sha256(raw).hexdigest()
        rid = receipt_id or str(uuid4())
        values = (rid, source_id, object_id, url, at.astimezone(timezone.utc).isoformat(), digest,
                  json.dumps(metadata or {}, sort_keys=True))
        with self.db:
            previous = self.db.execute('SELECT receipt_id,source_id,object_id,url,received_at,sha256,metadata FROM source_receipts WHERE receipt_id=?', (rid,)).fetchone()
            if previous is not None and previous != values:
                raise ValueError('Conflicting receipt identity')
            self.db.execute('INSERT OR IGNORE INTO source_blobs VALUES (?,?)', (digest, raw))
            self.db.execute('INSERT OR IGNORE INTO source_receipts (receipt_id,source_id,object_id,url,received_at,sha256,metadata) VALUES (?,?,?,?,?,?,?)', values)
        return rid

    def diagnostic(self, receipt_id, reason):
        with self.db:
            self.db.execute('''INSERT INTO source_diagnostics(receipt_id,reason)
                SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM source_diagnostics WHERE receipt_id=? AND reason=?)''',
                (receipt_id, reason, receipt_id, reason))

    def candidate_envelope(self, receipt_id, *, published_at=None, revision=0, supersedes_id=None,
                           quality='VERIFIED_OFFICIAL', source_kind='OFFICIAL_RESULTS', origin='REAL'):
        """Construct without publishing; accepted bundles validate before commit."""
        row = self.db.execute('SELECT ordinal,source_id,object_id,url,received_at,sha256 FROM source_receipts WHERE receipt_id=?', (receipt_id,)).fetchone()
        if row is None:
            raise ValueError('Missing durable receipt')
        order, source, obj, url, at, digest = row
        return EvidenceEnvelope(evidence_id=f'receipt:{receipt_id}', source_id=source,
            source_object_id=obj, source_url=url, source_kind=source_kind, origin=origin,
            published_at=published_at, received_at=datetime.fromisoformat(at), receipt_order=order,
            raw_payload=ArtifactRef(uri=f'sha256:{digest}', sha256=digest), quality=quality,
            revision=revision, supersedes_id=supersedes_id)

    def envelope(self, receipt_id, **options):
        try:
            e = self.candidate_envelope(receipt_id, **options)
            with self.db:
                self._put('evidence', e.evidence_id, e)
                self.selection(max(x.received_at for x in self.artifacts('evidence')))
            return e
        except (ValueError, TypeError) as exc:
            self.diagnostic(receipt_id, str(exc))
            raise

    def accept_bundle(self, race, envelope, observations):
        """Atomic acceptance of a validated source edition and all mapped rows."""
        from .totals import replay
        from forecasting.contracts import validate_count
        receipt_id = envelope.evidence_id.removeprefix('receipt:')
        expected = self.candidate_envelope(receipt_id, published_at=envelope.published_at,
            revision=envelope.revision, supersedes_id=envelope.supersedes_id,
            quality=envelope.quality, source_kind=envelope.source_kind, origin=envelope.origin)
        if envelope != expected:
            raise ValueError('Envelope does not match retained receipt')
        with self.db:
            self._put('evidence', envelope.evidence_id, envelope)
            self.selection(max(e.received_at for e in self.artifacts('evidence')))
            for obs in observations:
                if obs.evidence != envelope:
                    raise ValueError('Bundle evidence mismatch')
                validate_count(race, obs)
                self._put('observation', obs.observation_id, obs)
            history = tuple(o for o in self.artifacts('observation') if o.race_definition == race.ref)
            replay(race, history)

    def selection(self, cutoff):
        catalog = tuple(sorted((e for e in self.artifacts('evidence') if e.received_at <= cutoff), key=lambda e: e.receipt_key))
        latest = {(e.source_id, e.source_object_id): e for e in catalog}
        return EvidenceSelection(cutoff=cutoff, catalog=catalog, selected_ids=tuple(
            e.evidence_id for e in catalog if latest[e.source_id, e.source_object_id] == e and e.quality != 'REJECTED'))

    def put_race(self, race):
        if type(race) is not RaceDefinition:
            raise TypeError('RaceDefinition required')
        with self.db:
            previous = [r for r in self.artifacts('race') if r.race_id == race.race_id]
            same = next((r for r in previous if r.revision == race.revision), None)
            if same is None:
                if previous:
                    validate_race_revision(max(previous, key=lambda r: r.revision), race)
                elif race.revision != 0:
                    raise ValueError('Missing race revision ancestor')
            self._put('race', f'{race.race_id}:{race.revision}', race)

    def put_observations(self, race, observations):
        from .totals import replay
        with self.db:
            for obs in observations:
                saved = self.db.execute('SELECT payload FROM source_artifacts WHERE kind=? AND identity=?', ('evidence', obs.evidence.evidence_id)).fetchone()
                if saved is None or saved[0] != canonical_json(obs.evidence):
                    raise ValueError('Observation evidence must be durably retained')
                self._put('observation', obs.observation_id, obs)
            history = tuple(o for o in self.artifacts('observation') if o.race_definition == race.ref)
            replay(race, history)

    def raw(self, reference):
        row = self.db.execute('SELECT raw FROM source_blobs WHERE sha256=?', (reference.sha256,)).fetchone()
        if row is None or sha256(row[0]).hexdigest() != reference.sha256:
            raise ValueError('Missing/corrupt raw artifact')
        return row[0]
