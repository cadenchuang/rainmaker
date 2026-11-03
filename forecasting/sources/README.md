# Official result ingestion (Agent A)

This package accepts official U.S. Senate/governor cumulative results and hands
immutable, contract-v2 observations to C. It contains no forecasting, approval,
strategy, credentials, or execution changes. Shared contracts remain unchanged.

## Actual coverage at collection

Seven original public payloads are retained in `evidence/`, alongside actual UTC
receipt times, response headers, and SHA-256 hashes of both bytes and receipts.
Scoped Git attributes preserve exact evidence bytes, including source CRLFs.
The result files were retrieved November 3, 2026 at approximately 12:22 UTC.
`registry.json` explicitly enumerates all 50 states; unresearched states are
unknown coverage, not assertions that feeds or elections do not exist.
`coverage.json` records the narrower observed coverage.

Minnesota's official index explicitly says **Test Results**. Its all-county file
contains 435 Senate test rows and 348 governor test rows, each covering 87 counties,
with zero precincts reporting. These are authentic official-site bytes, but they
are **not live election results**. They are quarantined, never model inputs.
There are **zero accepted live observations** and no forecast-ready real count
state. Texas's official results page was discoverable, but direct byte retrieval
returned HTTP 403; no Texas payload or adapter is claimed.

Official sources:

- [Minnesota downloads](https://electionresults.sos.mn.gov/Select/MediaFiles/Index?ersElectionId=201)
- [Minnesota file layout](https://electionresults.sos.mn.gov/Results/MediaFileLayout/Index?erselectionId=201)
- [All races by county](https://electionresultsfiles.sos.mn.gov/20261103/allracesbycounty.txt)
- [Senate by county](https://electionresultsfiles.sos.mn.gov/20261103/ussenatecty.txt)
- [Census county FIPS registry](https://www2.census.gov/geo/docs/reference/codes2020/national_county2020.txt)
- [Texas results page](https://electionresults.sos.state.tx.us/results.html)

## Storage and replay

`SourceStore(path)` adds separate `source_*` tables to a caller-owned local SQLite
file using WAL/FULL. It does not alter the journal/approval tables or user_version,
start a worker, or introduce a service. SQLite triggers forbid updates/deletes to
raw blobs, receipts, artifacts, and diagnostics. A globally increasing receipt
ordinal provides a durable namespace within that database; independently created
databases must not be concatenated without a reviewed namespace migration.

Retain original bytes before parsing. Publication time is `None` unless an actual
source publication instant is known; HTTP Date and Last-Modified are retained,
not relabeled as publication or election snapshot time. Receipt time is sampled
immediately after the complete HTTP response is read. Imports use the original
retained receipt ID/time; replay never samples a new receipt clock. Replaying the
same receipt is idempotent. A new retrieval is a new receipt, even if bytes match;
callers must preserve receipt identity when retrying an import.

A changed source object must extend its exact preceding envelope revision. Invalid
roots, forks, missing ancestors, clock regressions, and conflicting IDs fail while
the raw receipt remains durable with diagnostics. Accepted bundles commit their
evidence and observations atomically after revision and historical count replay
validation. Failed normalization/replay commits a rejected source revision and
diagnostic; a rejected latest edition cannot revive old evidence.

Offline inspection/import of the retained evidence (no network):

```sh
python -m forecasting.sources.audit --database /tmp/results-review.sqlite
```

This imports all seven payloads; both result files have REJECTED envelopes. Other
payloads remain unverified context/supporting evidence, not official race rules.
The output reports zero accepted live totals. The sidecar headers preserve original
Last-Modified values; no historical receipt times are reconstructed from them.

## Minnesota adapter and identity review

`fetch(store, url)` fetches only registered public Minnesota URLs and retains bytes
and headers. Fetch the index first, retain its official context envelope, then
fetch a result file and call `ingest_receipt(store, result_receipt_id, identities,
context_receipt_id=index_receipt_id)`. This currently returns no accepted rows
because the source is serving a test release. A context page must precede the
result receipt by at most 15 minutes. Direct `normalize` is the pure parser;
`ingest_receipt` supplies retained provenance and durable quarantine.

Before enabling live acceptance, provide a reviewed `RaceDefinition` backed by
actual official rules and roster evidence, persist it with `put_race`, and supply
an `IdentityMap`. No real 2026 definition is published here: eligibility, rules,
ballot methods, seat/term and candidate-person identity still need official review.
The adapter verifies office IDs/names, state, county/state scope, and the exact
candidate order-code/name/party crosswalk. Unknown identities fail closed. Stable
person IDs come from that reviewed map, never from party, name, ballot position,
or a market outcome. Multiple mapped fusion lines sum once; absent lines remain
unknown. Named third parties and aggregate write-ins stay separate roster columns.

`county_crosswalk` joins the actual OSS county table to Census county names and
checks the exhaustive 87-county set. `mn-county-crosswalk.json` is the verified
output; OSS numeric IDs are not treated as FIPS codes or converted arithmetically.
The retained Census registry is the 2020 code list; it establishes the Minnesota
mapping used here, not a claim about current county changes elsewhere.

The documented files have no result-edition sequence field. The adapter uses the
retained server Last-Modified timestamp to order **file editions**, rejecting
missing/future timestamps and repeated/regressing editions. It does not infer
source_as_of from that resource timestamp. This is intentionally conservative:
changed files with identical Last-Modified seconds cannot be accepted. A provider
change in edition semantics requires review, not a local counter substitution.

Candidate counts are nonnegative exact integers; blanks remain None. A zero-
reporting county with zero placeholders becomes NOT_REPORTED with unknown counts;
nonzero votes with zero reporting precincts fail. Absent counties produce no row.
Precinct completeness is an exact REPORTING_UNITS metric, never a ballot completion
estimate. Even all precincts reporting stays PARTIAL; no certification is inferred.
The file's total votes for office maps to valid_contest_votes; turnout and total
ballots are not inferred. State and county totals retain separate coverage keys.

Corrections are explicit reviewed producer assertions: pass `corrections={unit_id:
previous_observation_id}` only when the retained official edition is verified as
replacement/correction authority. A decrease never auto-generates a correction.
The shared reducer enforces immediate bases, count witnesses across missing values,
source chronology, and exact candidate/race revision bindings.

## C handoff and exclusions

`replay` deduplicates exact observation IDs and sorts deterministically, rejecting
conflicting content. `active_totals` requires an explicit active observation list
and precedence; it rejects overlaps and stale selections. Current conflicting
sources require EXPLICIT_RECONCILIATION, applicable reconciliation evidence, and a
concrete explanation. Every other observation gets an explicit exclusion.

Use `counting_state(prior, observations, evidence, selected_ids, state_id=...,
precedence=...)` for publication. This constructs the full shared CountingState,
including effective numerical dependency and evidence applicability validation.
`active_totals` alone is partition bookkeeping, not a forecast-ready handoff. No
cross-source totals are silently added and no source is implicitly ranked. Complete
coverage is opt-in and validated against the reviewed county/method partition.

The test suite uses explicitly synthetic reports for positive normalization and
correction scenarios; those reports are not claimed as authentic election results.
The retained real test release separately exercises quarantine. Tests cover raw
retention, timestamps, invalid revisions, multiple sources, overlap, correction,
missingness, idempotency, restart/replay, schema refusal, and contract handoff.
