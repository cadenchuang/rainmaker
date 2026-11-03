# Forecast contract v2 handoff

Public imports are from `forecasting.contracts`. These are frozen interchange
values, validators, hashing/serialization helpers, and deterministic source-count
bookkeeping. They implement no forecasts, adapters, network access, persistence,
trading policy, approval gates or execution. No existing ledger is changed.

## Version boundary and identity

Every object, including nested objects, is schema_version=2. Names such as
RaceDefinition, PollObservation, ElectionPrior and ForecastRecord remain public;
constructor and payload changes are deliberately breaking. ForecastRecord's code,
environment, configuration, random_seed and evidence_manifest remain read-only
accessors backed by its single RunManifest; prior.evidence exposes full deduplicated
source lineage through dependency_evidence(prior). `from_json` rejects v1,
unknown types/fields, omitted fields, duplicate keys at any depth, non-contract
roots, nonfinite numbers, and implicit defaults. There is no automatic migration.

Keep original v1 bytes and their hashes. A reviewed migration must obtain missing
receipt order, race/rules revision, source lineage, missingness and run metadata
from trustworthy retained sources; it cannot infer or fabricate them. Re-emit new
v2 artifacts with new hashes and a migration audit retaining original-byte and
conversion-code references. Unrecoverable v1 records remain archived/unusable for
v2 replay. Neither storage.py's user_version nor the approval ledger changes.

Only 50-state U.S. Senate and governor, statewide single-seat candidate plurality
contests are eligible. No primary, majority/runoff, ranked-choice, multi-seat,
territorial or YES/NO market inference is supported. A verifies the actual official
rules; the enum is not a legal determination. Ties remain unresolved and write-in
aggregates cannot be interpreted as individual winners. Future rule families need
an explicit schema change and independent tests.

Stable race IDs are `US:{state}:{election_date}:{office}:{seat}:{contest}`. Dates
are ISO civil dates. Senate seat is CLASS1/CLASS2/CLASS3; governor seat is GOVERNOR.
SPECIAL adds `:TERM_END:{term_end}` to identify the remaining term. These identity
fields cannot change within `validate_race_revision(previous, current)`. A genuinely
different election event gets a different ID. Fixture events are fictional even
when their state/date/seat strings look plausible.

A definition has an integer revision, previous_definition_hash and content hash.
`race.ref` is a RaceRef(race_id, revision, definition_hash). Polls, counts, runs,
input manifests, joint draws, allocation payloads/manifests and partitions bind
this exact reference. A validates sequential revisions with validate_race_revision
and retains preceding definition artifacts in its immutable registry. The hash
covers winning rules, rule-source evidence, roster, geography and methods. Rules
changes therefore require a new definition revision and new downstream artifacts.

Candidate IDs identify people, not party, name, market outcome or ballot position.
A consolidates fusion ballot lines with raw lineage. Named third parties retain
separate columns. Once introduced, candidate columns cannot be deleted or reordered
in a definition revision: withdrawal changes status; replacement marks the original
REPLACED/replaced_by and APPENDS the replacement. Retained withdrawn/replaced
columns preserve reported votes; they do not authorize an outcome interpretation.
One WRITE_IN_AGGREGATE bucket is allowed. C withholds winner interpretation when
individual write-in eligibility or a tie is unresolved. A explicitly supplies
winner_eligibility (ELIGIBLE, INELIGIBLE or UNRESOLVED)
under the versioned official rules; withdrawal status alone never infers it.
WRITE_IN_AGGREGATE must be UNRESOLVED because it is not one person. No forecast
algorithm is implemented here.

## Units, geography, missingness and ballot methods

JointDraw.shares and LocalDraw.support_shares are fractions [0,1] of ALL valid
candidate contest votes, including third parties and write-ins. Pairwise margin
is share[i]-share[j], in [-1,1], on the same denominator; multiply by 100 only for
percentage-point presentation. No two-party renormalization is implicit. A local
cell with zero valid votes has support_shares=None, not a uniform support vector.
Poll shares use ALL respondents of the stated LV/RV/ADULTS sample, with undecided
and unmapped other separate. Polls may use an ordered subset of the roster. Field dates use the explicitly named
field_timezone, with field_end no later than publication/receipt's local calendar
date. Same-date publication is allowed because day-level fields do not establish
an intraday closing time; A checks any finer source timing without inventing it.

Counts are exact nonnegative integers. In candidate vectors, None is unavailable,
0 is an explicit observed zero. An omitted geographic unit is unavailable, not a
zero-valued unit. VoteQuantities separately records ballots_cast,
valid_contest_votes, undervotes, overvotes, invalid_contest_ballots; every quantity
may be None. For these accounting fields, ballots_cast means ballots in the
source's contest accounting universe, NOT registrations, turnout percentage or
ballots rejected before that universe. A must quarantine incompatible source
universes instead of relabeling them. If all components are known,
ballots=valid+under+over+invalid. Known subtotals cannot exceed a known total.
Complete candidate vectors must sum to a supplied valid_contest_votes total.

NOT_REPORTED, PARTIAL, COMPLETE and UNKNOWN are geographic reporting assertions.
COMPLETE requires all candidate values known; NOT_REPORTED requires all unknown.
CompletenessMetric names REPORTING_UNITS, EXPECTED_BALLOTS or
EXPECTED_VALID_CONTEST_VOTES, records numerator/denominator and EXACT/ESTIMATED
basis, and keeps unknown quantities unknown. A percentage of reporting precincts
is never interpreted as a percentage of counted votes or a completion guarantee.
Source certification is a separate assertion, not proof of spatial coverage.

ReportingUnit IDs are US:{state} and US:{state}:{five-digit-county-FIPS}, with the
explicit state parent. COUNTY includes official county equivalents. A verifies
the actual FIPS registry and the state/county mapping against its definition
source; string validation does not establish that a FIPS code exists.
`county_partition_complete` explicitly asserts an exhaustive county partition.
Otherwise a complete statewide partition cannot be inferred from listed counties.
No precinct inference or below-county identity is included in v2.

Race.ballot_methods is a disjoint source-verified method axis. Coverage can be:

- KNOWN: one axis method, or ALL for their union.
- MIXED: the explicitly sorted mixed_methods subset, each counted once; individual
  component counts are unavailable even though the covered method union is known.
- UNKNOWN: composition and extent are unresolved; it conservatively overlaps every
  method in its unit and cannot establish complete statewide coverage.

`validate_partition(race, coverages, require_complete)` expands geometry/method
cells and rejects overlaps. State ALL overlaps every county; county ALL overlaps
that county's MAIL. Mixed subsets overlap constituent methods. Unknown coverage
can enter a partial state but cannot be summed with method subsets of that unit.

## Evidence availability, selection and source chronology

All instants require offsets and normalize to UTC at construction, including DST
folds. Canonical timestamps use microsecond precision. Original textual offsets
and finer source timestamps remain in raw bytes. Receipt time is the actual local
ingestion sample, never publication time or replay time. A assigns receipt_order
from one durable canonical ingestion namespace; (received_at, receipt_order) is a
unique total ordering for evidence in that dataset, including equal wall times.
Multiple normalized rows from one payload share the same envelope and sort by
observation_id within that receipt key. Re-ingestion retains original identities
and times; it does not synthesize a fresh historical receipt.

published_at is the source's stated publication instant or None if unknown. It
cannot postdate receipt. Raw payload bytes are retained first; source clock errors
or invalid envelopes are quarantined with diagnostics outside accepted model
inputs, never repaired by rewriting timestamps. Evidence is eligible inclusively
at received_at <= cutoff, regardless of an earlier publication date.

(source_id, source_object_id) identifies one revision chain. Each root is revision
0; successors point to the preceding envelope, increment revision exactly once,
and have later receipt keys. Missing ancestors, forks, multiple roots, conflicting
IDs/hashes and reused receipt keys are rejected. EvidenceSelection retains the
ordered ancestor catalog and explicitly selects the latest non-rejected revision
of each object as of its cutoff. Rejected latest revisions do not revive old data.
A rejected receipt can be retained as raw lineage but cannot become a selected
model input. A stale choice must be quarantined rather than used as a forecast.

CountingObservation.source_sequence is a verified source-edition order within
(coverage, source_id), NOT the local receipt ordinal. A must establish it from
source sequence/edition/timestamp and retained provenance; if source chronology
cannot be established, quarantine the report. A delayed old report cannot be given
a new source sequence just because it arrived later. source_as_of, when available,
is the edition's snapshot time, not the time an individual ballot was cast.

`materialize_counts(race, observations)` deterministically checks receipt order,
strictly advancing source sequence, nonregressing known source_as_of, exact race
revision and candidate ordering. Semantics are:

- CUMULATIVE: replace a coverage/source total. A known decrease requires CORRECTION;
  a new missing value stays unknown, rather than carrying forward an old count.
- INCREMENTAL: add ONCE to the latest materialized total in that coverage/source.
  base_observation_id is mandatory and points to the preceding observation, even
  when that preceding observation was itself an increment. None + anything=None.
  Quantities in the increment are deltas. A later cumulative total can resolve
  unknowns. An increment cannot assert COMPLETE.
- CORRECTION: replace the latest total, with an explicit base; decreases are legal.
  Values are replacement totals, not signed deltas. Evidence revision lineage is
  independently checked: a correction may be a revision of a prior source object
  or a new official correction bulletin.

Duplicate observations must be deduplicated idempotently by A before constructing
the tuple; repeating an ID inside a CountingState is rejected. Same logical ID
with different content anywhere in the dependency graph is rejected. Cross-source
coverage totals stay separate until an evidenced reconciliation explicitly chooses.

## Active aggregation and typed count state

CountingState retains ALL raw observations, its EvidenceSelection, ActivePartition
and typed MaterializedCounts. Materialized rows are checked against the deterministic
reducer and preserve every missing value. No opaque count blob can bypass this.
`artifact_ref(state.materialized_counts)` gives a persistable content identity.

ActivePartition names selected observation IDs and an ExcludedObservation for
EVERY other raw observation, with SUPERSEDED, OVERLAPPING_REDUNDANT or SOURCE_CONFLICT
and a concrete explanation. Selected observations must be the latest in their
coverage/source stream AND use the selected latest source-object revision.
SUPERSEDED cannot be used to hide a current source total.

STATE_TOTAL selects only statewide ALL; COUNTY_TOTALS selects county ALL;
METHOD_DETAIL selects known methods/mixed subsets; EXPLICIT_RECONCILIATION requires
selected reconciliation evidence. SOURCE_CONFLICT exclusions require the latter.
Precedence is an explicit producer choice recorded in the artifact, never a guessed
ordering between official state and county feeds. Selecting overlapping totals is
always rejected. Redundant totals remain available for independent checks.

require_complete_coverage requires the entire statewide geographic/method universe
with no unknown method extent; require_complete_counts independently requires
COMPLETE selected rows with no missing candidate counts. A partial forecast can
use an explicitly partial partition, but cannot label it a complete final count.
previous_state is optional storage lineage, not an implicit numerical input; the
new state must retain the full observation history needed by its explicit bases.

## Statewide and conditional local simulation exchange

JointSimulation binds a RaceRef, candidate axis and ordered, equally weighted
JointDraws. Within each draw, statewide shares and total_valid_votes are paired.
Never independently resample their columns or turnout. These are contest-vote
turnout draws, not ballots-cast draws. The prior cutoff must precede election day
in election_timezone. Exact poll and source eligibility is checked before fitting.

LocalAllocationPayload fixes unit_ids, ballot_methods and candidate_ids. Cells
are ordered unit-major, then ballot-method-major. In v2 allocation methods equal
the full race method axis, and unit/method cells must form a complete disjoint
statewide partition. A county allocation therefore needs an exhaustive county
registry; a state-only allocation is representable but makes no county claim.

Each LocalDraw has statewide_draw_index, residual_draw_index, conditional_weight,
cell_votes[cell][candidate], and cell_quantities[cell]. The paired cell count and
quantity vectors are a weighted empirical JOINT local turnout/support distribution
conditional on the parent's statewide candidate counts. Residual indices start at
0 and are contiguous within each parent; weights sum to 1 per parent. There must
be at least one residual draw for EVERY statewide draw. A one-row group explicitly
represents degenerate conditional uncertainty, never an unstated error model.

For every residual draw, integer cell totals sum EXACTLY to parent valid turnout;
candidate totals divided by parent turnout equal parent shares within the recorded
run tolerance. Per-cell valid_contest_votes equals that row's integer candidate
sum. Ballot quantities, if simulated, have their own accounting constraints; they
may vary residually because the statewide prior fixes contest votes, not ballots.
Local support shares and pairwise margins derive from these same paired counts.
No independent local marginal distributions can lose turnout/support correlation.

LocalAllocationManifest binds payload hash, JointSimulation hash, RaceRef,
local-model hash, allocation-run hash and a separate local evaluation report
reference (None for an experimental, unevaluated model). There is no self-referential
prior hash: the statewide draw artifact exists BEFORE local allocation. Validate
with validate_local_allocation; then ElectionPrior embeds the payload, manifest,
statewide model/run and local model/run. B's statewide and local evaluations are
separate; statewide performance cannot establish county allocation accuracy.

## Reproducibility and quality

InputManifest has a RaceRef, ordered InputArtifacts, data version, EvidenceSelection
and optional training_cutoff. Each input has content hash, kind, version,
available_at and exact source_evidence_ids. For derived replay artifacts,
available_at means the availability of their SOURCE lineage at the selected
information set, not a claim that replay computation physically occurred then.
Actual model fit/run creation instants are recorded separately. RAW/LINEAGE hashes
must match the referenced envelope's raw bytes. RAW is selected evidence;
LINEAGE is superseded/rejected raw ancestry, not permission to consume it as data.
Every catalog entry must retain its raw input. Every selected envelope must appear
in consumed input lineage. Derived inputs cannot precede receipt of their sources.
Identical bytes may legitimately occur in different source objects; ordered inputs
retain those distinct lineage contexts even when their content hashes match.
All consumed source lineage must be selected and available by the cutoff; training
inputs must additionally be available by training_cutoff.

ModelArtifact fixes fitted parameters, implementation, feature/transformation
version hashes, actual fitted_at, typed training manifest, origin, status and
optional evaluation report. v2 model application records use the same target race
revision as their run; historical feature/outcome datasets remain explicitly
versioned inputs with raw source lineage. Model/application artifacts need new
identities when their target definition changes; immutable parameter bytes may
be shared. Evaluation reports are verified by D, not manufactured by this module.

RunManifest fixes role, RaceRef, inputs, full fitted model artifacts, actual
created_at, code/environment hashes, RNGSpec, sampling_procedure hash,
configuration hash, arithmetic mode and numeric_tolerance. RNGSpec supplies exact
generator algorithm and implementation version, seed, hashed stream-derivation
procedure, and uniquely named streams with spawn keys. The procedure bytes MUST
specify the generator constructor/seed expansion and spawn-key interpretation;
consumers reject unsupported procedures. Environment artifacts must contain exact
runtime/dependency versions and relevant numerical execution settings. Sampling
procedure/configuration artifacts define draw order, draw counts and all options.
This schema records the specification; implementing a generator or model is outside
this milestone. Record binary64 share arithmetic and exact integer count arithmetic;
tolerance is absolute share tolerance, 0..1e-6, while integer conservation is exact.

ForecastRecord embeds the exact prior, optional counting state, models, full output
draws and typed run. Its run must reference the exact prior hash and, for a live
forecast, the exact CountingState hash. Full dependency evidence must appear in
the forecast run catalog, with consistent IDs/hashes. Rules and roster evidence
must be selected and available in each applicable input manifest. Training cutoffs
cannot exceed the forecast evidence cutoff; actual computation may happen later
for replay. No worker may backdate receipt or use later revisions to improve an
old forecast. A seed alone is not a replay specification.

Evidence origin is REAL or SYNTHETIC. Synthetic evidence MUST have quality SYNTHETIC,
never VERIFIED_OFFICIAL; source_kind describes the simulated type without asserting
real provenance. Source verification states for REAL evidence are UNVERIFIED,
VERIFIED_OFFICIAL, VERIFIED_NONOFFICIAL and REJECTED. Constructors check consistency,
not network authentication: A remains responsible for verifying official provenance.
Synthetic training taints ModelArtifact, which cannot claim HISTORICALLY_EVALUATED.
Any synthetic dependency taints ForecastRecord, which must remain SYNTHETIC or
WITHHELD. Tests cannot promote it by changing one display label.

REAL models are EXPERIMENTAL or HISTORICALLY_EVALUATED with a report reference.
Forecast quality is SYNTHETIC, EXPERIMENTAL, EVALUATED, DEGRADED or WITHHELD; reasons
are mandatory for SYNTHETIC/DEGRADED/WITHHELD. EVALUATED requires all models evaluated
and all dependency evidence verified. eligibility is RESEARCH_ONLY,
EVALUATED_FORECAST or WITHHELD and must match quality. These are forecasting
research eligibility states; NONE means trade-eligible or authorizes any action.

## Four-worker interfaces and concrete examples

A owns canonical identity/rule registries, official state/county ingestion, poll
mapping, raw capture, receipt order, source chronology, revisions and quarantine.
Imports: RaceDefinition, Candidate, ReportingUnit, RaceRef, EvidenceEnvelope,
EvidenceSelection, PollObservation, CountingObservation, Coverage, VoteQuantities,
CompletenessMetric, validate_race_revision, validate_poll, validate_count.
Returns the race definition plus tuples of validated polls/results and their full
EvidenceSelection. Raw bytes and definition/observation JSON are stored by hash.
No market ticker supplies an election identity.

B imports A's outputs plus InputArtifact, InputManifest, ModelArtifact, RunManifest,
RNGSpec, RNGStream, JointDraw, JointSimulation, LocalDraw, LocalAllocationPayload,
LocalAllocationManifest, ElectionPrior, validate_local_allocation. Returns one
ElectionPrior (including both typed payloads/runs and exact manifest bindings).
B first persists JointSimulation, then builds/validates the conditional allocation,
then publishes the prior. No statistical estimator is prescribed here.

C imports ElectionPrior, CountingObservation, EvidenceSelection, ActivePartition,
ExcludedObservation, materialize_counts, MaterializedCounts, CountingState,
ForecastRecord and the input/run/model types above. Returns CountingState and
ForecastRecord. C explicitly chooses coverage and exclusions, validates the
materialized state, and constructs a run whose ordered inputs include that exact
state and prior. Counting likelihood and final-outcome simulation remain C's future
implementation; bookkeeping here never predicts uncounted votes.

D imports from_json, canonical_json, content_id, verify_artifact plus all relevant
types/validators above. Consumes immutable A/B/C artifacts. Returns a report
ArtifactRef and an EVALUATION RunManifest describing code, eligible data/splits,
models and configuration used to score it. Report contents must include metrics,
baselines, uncertainty, exclusions, statewide performance and separately measured
local performance. D alone supplies historical evaluation evidence for any later
model-status change; changing status creates a new artifact, not an in-place edit.

Runnable synthetic exchange (from repository root):

```sh
PYTHONPATH=tests .venv/bin/python - <<'PY'
from forecast_fixtures import fixture, ASSET_BYTES
from forecasting.contracts import (
    validate_poll, validate_count, validate_local_allocation, materialize_counts,
    verify_artifact, canonical_json, content_id, from_json,
)
race, evidence, poll, observation, prior, state, forecast = fixture('GOVERNOR', True)
# A -> B: exact definition and mapped poll, eligible at the prior cutoff.
validate_poll(race, poll, prior.evidence_cutoff)
# B -> C: paired statewide/local draws and all binding hashes.
validate_local_allocation(race, prior.simulation, prior.local_allocation,
                          prior.allocation_manifest, prior.allocation_run)
verify_artifact(prior.allocation_manifest.payload,
                ASSET_BYTES[prior.allocation_manifest.payload.sha256])
# A -> C: an explicitly PARTIAL county with (4, None, 0), not (4, 0, 0).
validate_count(race, observation)
assert materialize_counts(race, state.observations) == state.materialized_counts.rows
# C -> D: frozen complete record, canonical hash, independent read/validation.
restored = from_json(canonical_json(forecast))
assert content_id(restored) == content_id(forecast)
assert restored.quality == 'SYNTHETIC'
assert restored.eligibility == 'RESEARCH_ONLY'
PY
```

The regression suite also constructs Senate/governor regular/special events,
withdrawal/replacement revisions, two statewide draws with multiple paired local
residuals, cumulative/incremental/corrected totals, explicit overlapping raw totals,
partial/full partitions, delayed receipts, equal timestamps, DST folds and hash
conflicts. Fixture artifacts have distinct hashes of actual retained synthetic
bytes; they assert no historical coefficients or accuracy.

## Immutable storage and verification

All sequences are tuples and contracts are frozen. Mutable source buffers and
current-state pointers are outside the interchange. `content_id(contract)` hashes
canonical UTF-8 JSON; `artifact_ref(contract)` supplies a stable content URI/hash.
`verify_artifact(ref, raw_bytes)` checks retrieved bytes before parsing. URI is a
locator, not source authentication. Normalized float -0.0 serializes as +0.0;
object keys sort, sequences retain order, Unicode uses JSON escapes, UTC timestamps
use six fractional digits. Exact original raw bytes retain their own hashes.

Persist artifacts before publishing references. Stores must enforce append-only
logical identities and reject conflicting content, also across separate writes;
validate_dependency_graph detects conflicts within one complete dependency graph.
A future store can reuse local SQLite WAL/FULL and schema-refusal conventions,
without sharing the existing journal/approval tables. No new database, service,
message bus or worker runtime is introduced.

Run `.venv/bin/python -m pytest -q tests/test_forecast_contracts.py`, then
`.venv/bin/python -m pytest -q`, then `git diff --check`.
