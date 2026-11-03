"""Offline import of authentic retained receipts. No network or credential access.

python -m forecasting.sources.audit --database /tmp/results-review.sqlite
"""
import argparse
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from .store import SourceStore

EVIDENCE = Path(__file__).with_name('evidence')


def import_retained(store):
    manifest = json.loads((EVIDENCE / 'manifest.json').read_text())
    for entry in sorted(manifest, key=lambda x: x['received_at']):
        raw = (EVIDENCE / entry['file']).read_bytes()
        if sha256(raw).hexdigest() != entry['sha256']:
            raise ValueError('Retained evidence hash mismatch')
        receipt_raw = (EVIDENCE / entry['receipt_file']).read_bytes()
        if sha256(receipt_raw).hexdigest() != entry['receipt_sha256']:
            raise ValueError('Retained receipt hash mismatch')
        receipt = json.loads(receipt_raw)
        if receipt['received_at'] != entry['received_at'] or receipt['url'] != entry['url']:
            raise ValueError('Conflicting receipt manifest')
        rid = store.retain(entry['source_id'], receipt['url'], receipt['url'], raw,
            received_at=datetime.fromisoformat(receipt['received_at']),
            receipt_id=entry['sha256'] + ':' + entry['received_at'], metadata=receipt)
        store.envelope(rid, quality='REJECTED' if entry['results_file'] else 'UNVERIFIED',
                       source_kind='OFFICIAL_RESULTS' if entry['results_file'] else 'OTHER')
        if entry['results_file']:
            store.diagnostic(rid, '2026 Minnesota index labels release Test Results; no live result accepted')
    return len(manifest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True)
    args = parser.parse_args()
    store = SourceStore(args.database)
    try:
        print(f'Retained {import_retained(store)} authentic public payloads; accepted live totals: 0')
    finally:
        store.close()
