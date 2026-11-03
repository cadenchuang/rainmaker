# Forecast contract v1 handoff

Import public types and helpers from `forecasting.contracts`. This module has no
network, database, approval, market, or execution dependencies. All fixtures in
`tests/forecast_fixtures.py` are synthetic examples, not historical evidence.

## Eligibility and identity

Only U.S. Senate and governor, 50 states, single-seat statewide candidate plurality
contests are eligible in v1. A tie is an unresolved outcome, never an implicit
50/50 winner or a candidate-order tiebreak. Ranked choice, majority/runoff,
primaries, multi-seat contests, territories, and ambiguous rules are rejected.
A runoff can only be added through an explicit future rules contract. Workers
must verify official rules against `rules_evidence`; an enum alone does not prove
eligibility. No market YES/NO labels are accepted as election definitions.

A owns immutable race IDs: `US:{state}:{date}:{office}:{seat}:{contest}`; the date
uses ISO format. Senate seat is class plus explicit special-contest identity when
needed; governor seat is GOVERNOR. Candidate IDs are registry-assigned persistent
person identities, never names, party labels, ballot positions or market tickers.
Distinct ballot lines for one person are consolidated by A with retained raw lineage.
A race's candidate tuple establishes every vector's order. All named third-party
candidates remain separate. Known aggregate write-ins use WRITE_IN_AGGREGATE;
this bucket represents vote mass, not an individually electable candidate. If
unresolved write-ins could win, withhold winner probabilities pending resolution.
Roster/identity changes require a new versioned race artifact and new forecasts.

Reporting units use `US:{state}` and `US:{state}:{county-FIPS}` identities, with
explicit state parent. Counties are an exhaustive partition only when verified by A;
a partial roster must never imply full coverage. No precinct inference in v1.
`ballot_methods` enumerates source-verified disjoint methods (including an explicit
OTHER if necessary); Coverage uses one method or ALL. ALL is a total, never an
additional method. County and statewide totals, and ALL and method subtotals,
are overlapping representations: never sum them together. Unknown or unmappable
coverage is quarantined. Coverage of a poll is the statewide race and its stated
LV/RV/ADULTS population, not a county or method allocation.

## Units and uncertainty

Shares are finite fractions [0,1], not percentages. Final vote shares divide by
valid votes credited to the entire candidate vector, including write-ins; exclude
undervotes, overvotes, invalid ballots. Pairwise margin is share[i]-share[j] in
[-1,1] of that same all-candidate denominator, not two-party margin. Poll shares
divide by all sampled respondents in the specified population; undecided and
unmapped other responses remain explicit. Do not silently redistribute them.
Poll candidate IDs must be a nonempty ordered subset of the race roster; A validates
that mapping, rather than converting generic party preference into a candidate poll.

`JointDraw` contains statewide candidate shares AND total valid votes in the SAME
draw. `ElectionPrior.draws` is an equally weighted empirical joint distribution;
rows are paired, candidate columns follow the race roster. Never independently
resample columns or turnout. These are simulations, not fitted accuracy claims.
`local_allocation` references B's immutable conditional allocation artifact. Its
manifest must bind the statewide joint-draw artifact hash, race/roster hash, ordered draw index,
unit/method partition, conditional support/turnout distribution, normalization
rules and separately evaluated local-model report. Each draw must aggregate to
that statewide draw. It must not claim local validity from statewide evaluation.
The blob's internal numerical representation belongs to B and C; version it in
its manifest. Consumers reject missing/unknown manifests rather than guessing.

## Time, evidence, corrections and counting handoff

All datetimes must carry offsets; canonical JSON normalizes instants to UTC with
microsecond precision. Date fields are civil election/fieldwork dates, not UTC
instants. `published_at` is the source's stated publication instant or None when
unknown; never substitute download time. `received_at` is the actual first local
receipt of those exact raw bytes, captured at ingestion and preserved on replay.
Unknown publication does not mean timely evidence. Source clock anomalies are
quarantined, not rewritten. Receipt-time eligibility is inclusive at cutoff;
publication cannot postdate receipt. Raw references include SHA256 of exact bytes.

Evidence revision 0 has no predecessor; subsequent revisions point to the prior
immutable envelope and increment its revision. A resolves the chain against the
registry (same source/object, no forks or missing links). Receipt time of a
revision, not the original publication time, determines replay availability.
VERIFIED_OFFICIAL means authenticated official state/county election-source
provenance and validated parsing, not certified finality. VERIFIED_NONOFFICIAL
includes verified poll provenance; UNVERIFIED is exploratory only and must yield
EXPERIMENTAL/DEGRADED quality. REJECTED evidence cannot enter a forecast.

Counting votes are nonnegative integer candidate counts. CUMULATIVE establishes
or replaces a total for one exact coverage. INCREMENTAL adds votes once to its
explicit base; CORRECTION replaces that base's total, and may decrease counts.
Correction values are replacement totals, not signed deltas. A base must be the
latest accepted observation in that coverage, with identical roster/race. No
implicit differencing, reset detection or duplicate addition. Same ID/same content
is idempotent; same ID/different content is an error. A new cumulative report is
not added to its predecessor. Missing counts are unknown, never zero-filled.
Certification is a source assertion separate from verification and completeness.

C receives the exact immutable ElectionPrior (including local allocation and
model identities), then verified CountingObservations. CountingState retains the
ordered observation lineage needed to reproduce materialized_counts. No fitting
or updating the prior in place. C checks latest-base ordering, overlapping
coverage, monotonic receipt availability and raw revisions before aggregation;
conflicts withhold outputs. State IDs are registry keys; content hashes bind their
bytes. Previous-state references are optional lineage, never permission to omit
required observations from the replay manifest.

## Worker interfaces and ownership

- A imports RaceDefinition, Candidate, ReportingUnit, EvidenceEnvelope, ArtifactRef,
  PollObservation, CountingObservation, Coverage, validate_count. Returns canonical
  races, immutable raw/evidence manifests, mapped polls, and verified official
  result observations. Owns direct official state/county adapters, identity registry,
  rule verification, timestamps, revisions and quarantine. Existing market journal
  is not an official election results source.
- B imports RaceDefinition, PollObservation, EvidenceEnvelope, ModelArtifact,
  JointDraw, ElectionPrior, ArtifactRef. Consumes A's point-in-time manifests and
  returns ElectionPrior plus referenced allocation/model artifacts. Owns statewide
  prior and separately evaluated conditional local turnout/support allocation.
- C imports ElectionPrior, CountingObservation, CountingState, ForecastRecord,
  ModelArtifact, JointDraw, validate_count. Returns immutable CountingState and
  ForecastRecord plus materialized-count/output artifacts. Owns counting likelihood,
  final-outcome simulation and explicit unresolved tie handling. No trade decisions.
- D imports all contracts plus canonical_json, content_id, from_json. Consumes
  immutable artifacts/manifests and returns an EVALUATION ModelArtifact and hashed
  evaluation report (split dates, data eligibility, baseline, metrics, uncertainty,
  exclusions, statewide AND local evaluation separately). Owns independent historical
  replay/evaluation; cannot rewrite receipt times or promote models without evidence.

No worker owns approval changes, broker changes, order submission or trading policy.
No workers or forecasting algorithms are started by this contract milestone.

## Forecast quality, storage and schema evolution

Model status defaults to no inference: producers must explicitly choose EXPERIMENTAL
or HISTORICALLY_EVALUATED; the latter requires a report reference, whose validity
D verifies. Forecast EXPERIMENTAL is unvalidated; EVALUATED requires every referenced
model historically evaluated plus verified eligible evidence and complete replay
inputs. DEGRADED has known limitations and mandatory reasons; WITHHELD has mandatory
reasons and may have no draws. No quality label is a performance guarantee. Complete
manifests include every used artifact/model (including all prior models), receipt
cutoffs, explicit ordered revision selection, data version, code/environment/config
hashes and RNG seed. Seed alone is insufficient for deterministic replay.

All contracts and nested sequences are frozen tuples. Mutable ingestion buffers,
registries and current-state pointers are implementation details, not forecasts.
Persist canonical UTF-8 JSON append-only by content_id (SHA256); reject conflicting
content for a logical ID. URI is a locator, hash is integrity, not proof of source.
Persist referenced bytes before publishing a record and verify their hashes on read.
No forecast database is introduced here. Existing storage.py journal and approval
ledger schemas remain untouched. A later store can reuse local SQLite conventions
(WAL/FULL, transactional writes, refuse unknown schema), without sharing their tables.

Every nested object carries schema_version=1 and a type discriminator. from_json
rejects unknown types/fields/versions and constructors validate types/invariants.
No automatic migrations or field dropping. Future migrations must be explicit pure
conversions yielding NEW hashes/IDs with original artifact/migration-code references;
keep original bytes for replay. Readers must not edit old records in place.

Run `.venv/bin/python -m pytest -q tests/test_forecast_contracts.py` then
`.venv/bin/python -m pytest -q`.
