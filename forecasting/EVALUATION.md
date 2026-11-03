# Independent evaluator

`forecasting.evaluation` scores frozen Senate/governor artifacts. Evaluation
requires retained artifact bytes (`store=ArtifactStore(...)`), a materialized
outcome, and verifiable fitting lineage. It does not promote model status.

## Trusted outcome boundary

Construct labels with `evaluation_integrity.materialize_outcome(complete_state,
store)` or `materialize_historical_outcome(history, selection, store)`. The first
revalidates the canonical CountingState with both complete geographic coverage and
complete counts required, including chronology, source revisions and numerical
bases. The second accepts only independent complete cumulative historical records.
Both verify disjoint exhaustive coverage, exact race-definition revision/candidate
axis, explicit valid-contest totals, retained source bytes and source selection.

A versioned CAS outcome artifact binds the actual derived vote vector, complete
partition, materialized counts/source history and selected evidence IDs, envelope
hashes and raw hashes. `score`, `evaluate`, and `evaluate_record` recompute that
binding before scoring. Replacing only `Outcome.votes`, or rehashing arbitrary votes
without changing the independently normalized source observations, fails. Reports
identify the outcome artifact, not just an evidence envelope; `report_artifact`
requires both scored forecast and materialized-outcome hashes in the evaluation
run inputs.

Synthetic source normalization supports the saved slice's explicitly fictional
format and preserves SYNTHETIC provenance. REAL-enum labels need an independently
configured `OfficialOutcomeAdapter`: approved source identities/URLs, decoded
rules/roster, normalized source observations, and an explicit final-contest coverage
assessment. Unknown, partial, missing, overlapping, mixed-origin and test-release
inputs cannot become verified complete real labels. Hashes and enum labels alone
are not authentication. An adapter is evaluator trust configuration, never supplied
by the fitted model. The repository has no production official complete-result
adapter; its positive protocol test uses invented bytes and a mock adapter only.

## Verified fitting membership

B persists `ridge-training-v2`, `local-profiles-v2` and `statewide-features-v2`
FEATURES documents inside existing v2 InputManifest/ModelArtifact/RunManifest
contracts. Each includes canonical race definitions, full observations and source
lineage. The local matrices retain their corresponding historical identities.
The shared contract schema is unchanged.

The verifier hash-checks fitting bytes, recognizes the exact supported B/C
implementation, reconstructs the ridge fit and checks its parameters, reconstructs
local profiles and checks their parameter matrices, and verifies outcome-bearing
feature histories and transformations. All selected result sources must be
accounted for. It unions label, local-profile and historical-feature membership,
checks source normalization/availability and earlier-cycle independence, and
reconciles declarations exactly. In the slice, five regression examples consume
six historical races because their lagged features and local profiles also matter.
Empty or incomplete training declarations cannot certify a clean split.

Unknown estimators/calibrators, extra opaque fitting inputs, missing bytes,
parameter disagreement and old anonymous local profiles fail closed. A new
estimator needs an independently reviewed lineage reader. Existing artifacts are
never rewritten or assigned inferred training membership. The supported frozen
code/environment and original source records remain necessary for replay.

## Scoring and temporal evaluation

`evaluate(outcomes, predict, store=...)` holds out whole election years. Only
older labels actually received before each target cutoff enter the eligible set.
The callback receives that set, target definition and cutoff, and returns a record,
used race IDs and deduplicated poll count. Used IDs must match verified fitting
membership. Cutoffs are 30, 7 and 1 calendar days before election-day local midnight.
An adapter with no independently fitted vintage may raise `EvaluationUnavailable`;
the report records an exclusion. Integrity failures are not swallowed as exclusions.

Pre-election auditing rejects election-night records. The integration adapter
scores frozen live records separately, with the same outcome and fitting checks.
Scores use all-candidate vote fractions: first-roster-pair margin MAE, first
candidate-versus-field Brier/log loss, central 90% interval coverage/width and
calibration bins. Ties and unresolved eligibility are rejected. Log loss clips
probabilities at 1e-15; baseline event probability is 50%. Subgroups separate
origin, horizon, office and input sparsity. Wilson intervals assume independent
races within each subgroup; repeated horizons are correlated. These calculations
do not establish historical accuracy, calibrated local performance or trading edge.

`historical_replay` remains an audit reducer over independently verified real
receipts; it does not produce complete scorable outcomes or certify final coverage.

## Validation and limitations

```sh
.venv/bin/python -m forecasting.synthetic_slice --output /tmp/rainmaker-integrity-check
.venv/bin/python -m pytest -q tests/test_evaluation_integrity_integration.py tests/test_forecast_integration.py tests/test_forecast_evaluation.py tests/test_forecast_prior.py
.venv/bin/python -m pytest -q
git diff --check
```

Use a new output directory for the revised fixture byte format. Earlier directories
remain immutable archives; they are not silently migrated. Genuine scored
elections and authentic historical replays remain zero. The full suite needs
localhost socket permission for existing dashboard and stream tests. See
INTEGRATION.md for observed fix validation and retained output locations.
