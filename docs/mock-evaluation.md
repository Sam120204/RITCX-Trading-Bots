# Local volatility evaluation — November 19, 2026

**Scope correction after rereading the official PDF:** the following historical
standard/stress results are sensitivity tests. Their maker-pricing and forecast
assumptions differ from the published description and cannot establish that the
bot is unprofitable in the official case. Immediate full fills without impact are
explicitly part of the published case. See the [subsequent case audit and strategy
comparison](volatility-research-2026-11-19.md) for the new case-based scenarios.

The original local demo made profitable opportunities too predictable: forecasts
were centered on true future volatility, quotes adjusted over a fixed 15 ticks,
and fills had no delay. The demo remains available for regression checks. Its
profits are not evidence of competition profitability.

Version 2 adds standard/stress scenarios with forecast misses, variable quote
reaction, noisy quote volatility, and simulated time consumed by requests and
orders. The market maker responds to public forecasts and mean-reverts unknown
future volatility; it cannot see unreleased future volatility. Seed streams are
separated so policy choices do not change the external market path.

These assumptions are **uncalibrated sensitivity tests**, not a recovered model
of Rotman's matching engine. They retain Black–Scholes, one-second price steps,
full fills, and no impact/queue/partial-fill/outage simulation. The official
practice connection was STOPPED at tick 0 when checked during this evaluation.
No official trading orders were submitted for it.

## Fixed-policy comparison

Command: `python3 -m ritcx.mock_evaluate --seeds 30 --output reports/volatility-stress-evaluation-2026-11-19.json`

480 heats total: standard and stress, four fixed policies, 30 exploratory seeds
(0–29) and 30 separate held-out seeds (10000–10029). The candidates were specified
before looking at the held-out results. All outcomes, including losses and
failures, are retained. Each policy uses the same market seeds. No strategy
setting is automatically applied to the practice/contest launcher.

| Policy | Held-out standard mean / loss rate | Held-out stress mean / loss rate |
|---|---:|---:|
| Baseline: 0.025 edge, 500 delta, 10-contract entries / 100 cap | −$1,717 / 90% | −$1,893 / 93% |
| Strict edge: require >0.10 | −$221 / 67% | −$1,672 / 90% |
| Wide hedge: 1,000 delta | −$1,903 / 97% | −$1,819 / 90% |
| Half size: 5-contract entries / 50 cap | −$624 / 73% | −$1,192 / 87% |

Stricter entries improve average P&L versus baseline on both held-out scenarios,
but still lose money on average and have worse worst-case P&L (−$3,479 standard,
−$4,931 stress) than baseline (−$3,310 and −$4,108). Smaller positions reduce both
average losses and worst losses in these samples (worst −$2,102 and −$2,684).
Widening the hedge band is inconsistent across scenarios. None of these results
establishes a profitable competition strategy. No combined/tuned policy was
selected on these same held-out seeds.

All 480 runs ended with the strategy flat, no delta fines, and no execution
errors. Error retention and expiry inventory accounting are separately tested
with an injected request rejection; failures are not excluded by the evaluator.

Full per-heat outcomes, paired statistics, scenario parameters, policy settings,
and source fingerprints are in
[`reports/volatility-stress-evaluation-2026-11-19.json`](../reports/volatility-stress-evaluation-2026-11-19.json).

## Interpretation

The strongest finding is sensitivity to market assumptions, not a validated
parameter improvement. The bot treats announcements as reliable valuation
information and can pay substantial hedging/round-trip fees when forecasts are
wrong or the market prices future volatility differently. More permissive entry
rules or larger size are not supported here. Keep the live defaults until actual
practice observations can calibrate forecast errors, quote behavior, fees, and
latency. Evaluate any further changes on new seeds rather than repeatedly tuning
against this held-out set.
