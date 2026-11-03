# Independent evaluator

`forecasting.evaluation` consumes immutable v2 artifacts and does not implement or
modify any forecast model. `evaluate(outcomes, predict)` uses a forward election-year
holdout: only earlier cycles whose final labels were actually received by the
forecast cutoff enter training. The earliest cycle is excluded rather than trained
on future outcomes. The callback receives the eligible training set, target race
and cutoff, never the target label. It returns a ForecastRecord, the complete
training race membership, and a selected unique poll-envelope count.

Cutoffs are 30, 7 and 1 calendar days before election-day **local midnight**, using
the definition's timezone. Availability is receipt time, inclusive at the cutoff;
publication time cannot make late evidence eligible. Exact race-definition hashes,
contract dependency graphs, model training cutoffs, declared training membership,
and deduplicated poll counts are checked. Adapters must resolve revisions using
contract EvidenceSelection; superseded corrections cannot be substituted into an
old forecast. Opaque model parameters cannot establish the absence of hidden label
leakage: independent raw lineage and complete adapter training declarations remain
necessary. These checks do not certify externally supplied provenance.

Scores use fractions of all valid candidate votes. Margin MAE compares the first
two explicitly ordered roster candidates; it does not infer party or renormalize
to two parties. Brier and log loss score the first roster candidate's win against
the entire field. Log loss alone clips probabilities at 1e-15. Ties and unresolved
eligibility are rejected. Central 90% draw intervals use linearly interpolated
quantiles, inclusive coverage and mean interval width. Calibration has ten bins,
with probability 1 in the final bin and null values for empty bins. A transparent
zero-margin / 50% event-probability baseline is reported alongside scores.
Coverage uncertainty uses a 95% Wilson interval, conditional on independent races
within a subgroup. Small samples remain small; this is not model uncertainty or
an edge estimate. No pooled inference across correlated horizons is made.

Reports split REAL and SYNTHETIC, horizon, Senate/governor and sparse/non-sparse
inputs. Sparse means fewer than three unique selected poll envelopes by default;
it is an input-availability label, not an estimate of demographic coverage. Reports
include per-row forecast hashes, declared training membership, raw outcome hashes,
source URLs, receipt times, exclusions, scored prediction counts and unique race
counts. `report_artifact(report, evaluation_run)` returns deterministic bytes and
an ArtifactRef embedding the caller's EVALUATION RunManifest, requiring scored
forecast hashes in its inputs. Caller owns artifact storage and must supply genuine
code/environment/configuration references and the complete eligible-data lineage.
No model status or trading eligibility is changed.

`historical_replay` requires real, verified official observations with original
source timestamps, original receipt times and an independent raw-byte/authenticity
verification callback. It replays receipt-ordered prefixes through the shared
count reducer, preserving corrections and missingness. It returns audit rows, not
an active CountingState or a statewide estimate from early counties. A callback
returning True is a trust boundary, not evidence of historical authenticity.

## Independent results and blockers

No authentic historical observations, final-outcome dataset, historical forecast
artifacts, or forecasting implementations were present in this checkout. Therefore:

- Genuine scored elections: **0**. Genuine election-night replays: **0**.
- No historical MAE, calibration, coverage, profitability, or empirical edge can be
  claimed. Local model evaluation is also blocked on authentic local predictions
  and final local outcomes.
- Deterministic end-to-end fixtures score **2 invented races**, one per office,
  at three horizons (**6 synthetic predictions**). Two invented earlier-cycle
  labels exercise training. The earlier-cycle targets produce six exclusions.
  Synthetic fixture forecasts have margin MAE 0.10, Brier 0, and 90% interval
  coverage 0 at every horizon. These deliberately simple values test arithmetic,
  not forecasting skill. REAL subgroups remain empty with null metrics.
- Adversarial tests exercise candidate/race mismatch, unavailable labels and
  candidate counts, duplicate outcomes/evidence, held-out label membership,
  synthetic replay rejection, corrections versus unexplained decreases,
  raw-authenticity rejection, and an unrepresentative early county that remains
  a county rather than being extrapolated statewide. REAL enum test branches use
  invented fixture bytes and are not historical observations.

Run from the repository root with a Python environment containing pytest:

```sh
python -m pytest -q tests/test_forecast_evaluation.py tests/test_forecast_contracts.py
```

Initial full-suite validation: 264 passed; 20 pre-existing dashboard/stream tests
failed because the sandbox disallows binding local sockets. The targeted forecast
suite passed after expanding end-to-end/adversarial checks. See commit delivery
for final counts. Default system Python lacks pytest; validation uses the existing
neighboring project's Python environment without modifying it.
