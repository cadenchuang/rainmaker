# Rainmaker: value-based autonomous trading implementation handoff

Date: November 3, 2026.

## Instruction to the implementing agent

Inspect the existing repository, its instructions, forecasting interfaces, execution service, risk ledger and tests. Implement this policy using existing infrastructure where possible. Do not rewrite working modules unnecessarily. Report implementation, statistical validation and live activation as separate states. Do not submit an order merely to test integration.

This document supersedes conflicting earlier strategy proposals: use up to four distinct Senate/governor races, $50 per race and $200 overall. Remove the earlier requirement that the candidate or selected outcome must have a 90% winning probability. The objective is evidence of a pricing advantage; low-probability outcomes can qualify. The earlier $10 race cap, $100/$100 autonomous/manual split and fixed 5-cent entry requirement no longer apply to this policy.

The $200 budget and autonomous operating direction come from the user. Statistical settings below are proposed research defaults, not measured optima or proof of profitability. This handoff does not establish that the existing system has passed validation or been enabled.

## 1. Initial scope

- Standard $1-or-$0 winner contracts for individual U.S. Senate and governor races, evaluating both YES and NO.
- Initial entries during counting, supported by an applicable validated counting state and pre-election prior.
- Only explicitly mapped, allowlisted races, election formats and settlement rules covered by validation.
- Margins, Senate control/seat counts, House control and combos remain disabled extensions. Each requires its own forecast and payout evaluator, validation and execution support.
- Entry assumes willingness and ability to hold through settlement. An early sale may be considered under a separately implemented exit policy; price convergence is never assumed.

## 2. Entry rule and units

For a standard binary contract paying $1 for the selected outcome:

`p_conservative * $1 > average_purchase_price(q) + entry_fee(q)/q + required_edge`

Equivalent implementation form:

`q * p_conservative * $1 - purchase_cost(q) - entry_fee(q) > q * required_edge`

Initial candidate setting: `required_edge = $0.03 per contract`, strictly greater than. Equality fails. Compare using appropriate exact monetary precision; do not rely on floating-point rounding around the boundary. Probabilities are in [0,1]; monetary amounts are dollars or explicitly typed exchange units. Never confuse 30%, 30 cents and 30 dollars.

There is no outcome-probability floor. A 30% outcome can offer value at 21 cents; a 95% outcome can be overpriced at 98 cents.

The buffer is not a fee, profit guarantee or sell target. Evaluate alternative buffers on earlier validation folds and freeze the selected policy before final evaluation. Do not tune until a small sample of historical trades looks profitable.

## 3. Conservative probability: required statistical contract

The forecasting service must provide a nominal probability and a defensible conservative estimate for the exact contract payout, with supporting artifact and validation references.

Research default: evaluate a one-sided 95% lower probability bound where the statistical model supports such a bound. This is not an assertion that a valid bound currently exists. Distinguish frequentist confidence bounds, Bayesian credible bounds and scenario stress estimates. Do not label any of them interchangeably or promise conditional frequentist coverage without evidence.

Required forecast fields:

| Field | Requirement |
|---|---|
| forecast_id, model_version | Durable prediction and fitted model identities |
| counting_state_id, evidence_manifest | Exact applicable inputs and revisions |
| information_cutoff, generated_at | Information timing and computation timing |
| contract_rules_hash, payout_mapping_version | Exact event being priced |
| p_nominal, p_conservative | Valid probabilities for the selected payout |
| uncertainty_method, confidence_level | Method identity and interpretation; level may be null for scenario methods |
| calibration_artifact_id, validation_report_id | Empirical support and eligibility |
| stress_results, quality_flags | Plausible alternatives and source problems |
| supported_counting_regime | Evidence that this reporting situation falls within supported coverage |

Do not create a lower bound by arbitrarily subtracting percentage points. Do not substitute a quantile of final vote margin or Monte Carlo sampling error for uncertainty in the probability estimate. Running more simulations reduces numerical error; it does not validate model assumptions.

For complementary binary YES/NO outcomes, a conservative YES probability can use the YES lower bound. A conservative NO probability uses `1 - YES upper bound` from an appropriate corresponding method. It must NOT use `1 - YES lower bound`. Alternatively estimate each side directly with correct uncertainty handling.

Fit and evaluate on chronological whole-election-cycle splits, with historical information cutoffs. Assess calibration, tail errors, nominal bound behavior where measurable, missing-data sensitivity, shared errors and counting-stage coverage. The true probability for one election is not directly observed; one realized result cannot validate its probability bound. Thousands of repeated snapshots of a race are not independent elections. Report sparse evidence and selection/multiple-testing limitations. Per-trade 95% confidence does not imply 95% confidence that all selected trades have positive edges or that the portfolio profits.

Missing uncertainty support, invalid revisions, conflicts or unsupported counting conditions => BLOCKED. Ordinary uncertainty with valid support can lead to SKIP if the edge is insufficient.

## 4. Executable cost and quantity

Use actual synchronized offers and available quantity. For each feasible quantity q, calculate purchase cost across price levels, actual applicable maker/taker fee treatment, multiplier and rounding. Use a versioned fee adapter; never assume a flat 1-cent fee. Unknown applicable fee policy blocks entry.

Avoid double-counting spread or order-book depth effects already included in purchase cost. Separately account for potential price changes between decision and execution through a limit price and revalidation. Do not claim a displayed quote is guaranteed liquidity.

Generate only quantities whose permitted partial fills and worst allowed execution prices satisfy the edge condition after applicable fee rounding. Do not authorize overpriced tail fills merely because earlier cheap fills improve the average. If fee behavior cannot be bounded for possible partial fills, restrict execution to a supported configuration or block the proposal.

Use whole contracts initially; fractional support is optional after exact unit and rounding tests. A $50 allocation is a maximum debit allowance, not 50 contracts or a target to spend exactly $50.

Hypothetical arithmetic, using an assumed fee solely to explain the rule:

| Item | Value |
|---|---:|
| Conservative YES probability | 27% |
| Purchase price | $0.21 |
| Assumed average entry fee | $0.01 |
| Conservative expected profit | $0.05 per contract |
| Required edge | $0.03 per contract |
| Result | Eligible on value alone; still requires all other gates |

At a conservative probability of 25%, that example has exactly 3 cents of edge and fails the strict inequality. No example constitutes measured performance or a market quote.

## 5. Budget and race slots

Use a campaign-scoped durable ledger. Hard invariant:

`filled purchase principal + all paid trading fees + pending debit reservations + reserved exit fees <= $200`

Do not credit sales proceeds, settlement payouts or profits back into this cumulative spending allowance. This is a deliberately conservative gross-spending policy, distinct from current cash, equity, exposure or net loss. Broker buying power must independently suffice. Reserve enough for the configured possible exit path so entering four positions cannot silently put future trading fees outside the ceiling.

Each race has a $50 envelope covering its cumulative purchase principal, paid fees and applicable pending/exit reservations. Equivalent party/candidate tickers and opposite sides share race identity. Selling does not replenish a race envelope.

At most four distinct races may have entries filled during the campaign. Pending new-race submissions reserve slots; unknown submissions retain them. A conclusively canceled/rejected zero-fill intent may release a provisional slot. A race with any entry fill keeps its campaign slot even after closure. This prevents race churn and ambiguity about partial fills.

Quantity must also satisfy a documented portfolio correlation/stress-risk policy. Four distinct races are not necessarily diversified. Missing required risk-policy settings block automatic entries. The user chose $50 ceilings; that does not establish that $50 is appropriate for every qualifying trade. Apply the configured, validated sizing policy beneath the caps; the budget cap alone is not a sizing optimizer.

Evaluate available proposals together and re-evaluate after each atomic reservation. A transparent initial ranking is conservative expected profit per dollar reserved, subject to portfolio risk checks. Label it as a heuristic. There is no evidence yet for an optimal future-opportunity reserve schedule. Do not force four entries or deploy the whole budget.

## 6. Runtime decision and execution sequence

1. Validate allowlist, contract rules, counting evidence, forecast eligibility and uncertainty support.
2. Validate synchronized book, market status, timestamps, fees and broker reconciliation.
3. Build feasible quantity/price proposals satisfying the strict entry inequality.
4. Apply sizing, race/campaign budgets, slots and correlated-risk constraints.
5. Atomically reserve debit and race slot under serialized risk authority.
6. Persist decision and stable order intent before dispatch; persistence failure prevents submission.
7. Recheck current veto/control version, forecast applicability, rules, fees, price ceiling and budget immediately before dispatch. Recompute and re-reserve if changes require it.
8. Submit through one broker using a limit-priced immediate-or-cancel order. No automatic price chasing. Partial fills are acceptable.
9. Reconcile order status, fills, positions, fees and reservations. Deduplicate exchange events.

Define expiring proposal fields: contract, side, race, quantity, limit, maximum debit, fee assumptions, entry edge, expected profit, forecast/book/rules/policy/control versions, creation/expiry times and invalidation reasons. Freshness/expiry settings must be configured for the actual feed and strategy; missing values block entry.

Known pending orders are permitted if fully reserved. Unknown submission status or material accounting discrepancies block additional risk until reconciled. Timeout does not mean rejection. A client ID is not by itself proof of exchange idempotency. Before retrying, verify whether the original economic order exists. Retain risk while cancellation is pending because fills can race cancellation.

Restart behavior: preserve campaign spending, slots, blocks and intents; reconcile outstanding orders, fills, cash and positions before entries resume. Fixture models and test environments cannot access real submission endpoints.

## 7. Hold, sell and user controls

Default: hold to settlement. Do not enter relying on a planned resale at the model's fair value. Forecasting a 30% election probability does not forecast an executable market bid of 30 cents before settlement.

Automatic exits require a separately enabled and validated exit policy comparing net executable sale proceeds against updated expected settlement value, portfolio effects and reserved fees. Use an appropriate exit uncertainty policy rather than blindly reusing the pessimistic entry bound. Original entry cost is sunk for the forward comparison. Entry's 3-cent requirement is not an automatic stop-loss or exit threshold.

Early-sale profitability claims require historical executable prices, quantities, timing and both entry/exit costs. Without those records, label evidence as forecast validation or synthetic software tests, not a trading backtest.

Persistent controls: pause entries, block race, veto proposal, cancel pending entries, stop all submissions and explicit resume. User vetoes apply at the serialized submission boundary. Requests already sent may fill; completed fills cannot be undone. Report actual states: blocked before dispatch, cancel requested, cancel confirmed, partially filled, filled or unknown. Do not report a cancellation request as a successful veto.

Data/ledger faults block entries and attempt cancellation of pending entries. Continue reconciliation and alerts. Do not blindly liquidate using bad inputs. No agent may raise budgets, lower edge thresholds, change the uncertainty method or resume a user stop without explicit operator action.

## 8. Implementation acceptance criteria

- Entry arithmetic handles probability/money units, strict equality, fee rounding, multiple book levels and partial fills. High candidate probability is never a hidden eligibility floor.
- Conservative NO uses appropriate upper-bound information; no inversion of the YES lower bound.
- Unsupported uncertainty and unvalidated forecast artifacts cannot become trade eligible through an override or fixture flag.
- Duplicate/revised/out-of-order counting data cannot inflate votes or duplicate evidence.
- Simultaneous proposals cannot overspend $200, exceed a race envelope or reserve a fifth race. Canceled zero-fill slots release correctly; filled slots persist.
- Fees for entry and authorized exits stay within reserves; unexpected fees cause containment and review. Sell proceeds never silently replenish allowances.
- Timeout, duplicate acknowledgments/fills, cancel/fill races and restart cannot create duplicate economic orders or release reservations early.
- Veto ordering is tested before reservation, before dispatch, after dispatch and during partial fills. Blocks persist after restart.
- Integration replay covers forecast → decision → broker simulation → reconciliation, without real endpoint calls.
- Statistical validation, realistic trading replay and operational tests have separate reports. Threshold tuning uses earlier folds; final evaluation remains untouched. Missing historical depth or few independent cycles are explicit limitations.

## 9. Deliverables and reporting

Implement typed interfaces and deterministic evaluators, ledger transactions, control endpoints, broker integration and tests. Reuse existing project conventions and persistence. The conversational layer explains outputs; it cannot invent probabilities or bypass the broker.

Log every TAKE/SKIP/BLOCKED/HOLD/SELL decision with reasons and input versions. Record evidence receipt, forecast completion, decision, dispatch, acknowledgment and fill times; do not invent the time information first became public. Record actual fees and fill prices against assumptions.

Provide: modified modules, test results, unresolved statistical dependencies, data/fee adapter readiness and exact live-mode status. A successful code review or synthetic test is not permission to label the strategy profitable or calibrated.

Notify the user of fills, faults and portfolio summaries through a separately configured channel. This document does not establish a notification integration or ongoing monitoring service.

## References

- Kalshi order mechanics: https://kalshi.com/pro/help/the-order-panel
- Kalshi fee schedule: https://kalshi.com/fee-schedule and https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Probability calibration concepts: https://scikit-learn.org/stable/modules/calibration.html

These sources explain mechanics and concepts; they do not establish Rainmaker's measured edge or an optimal 95%/3-cent policy. Verify current contract terms and applicable fees during integration and before execution.
