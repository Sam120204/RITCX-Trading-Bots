# Volatility strategy audit — November 19, 2026

Subsequent revision: [buffered entries reduce risk-limit churn](volatility-buffered-followup.md).
The reports below are retained as evidence for the code version evaluated at this stage.
The user subsequently selected `buffered` as the default; references below to
unchanged baseline defaults describe the earlier evaluation stage.

## What the published case actually specifies

Source: [RITCx Volatility Trading Case](https://rotmanfrtl.github.io/RITCx-Volatility%20Trading%20Case.pdf), downloaded again November 19. Its SHA-256 matches `references/official/volatility-case.pdf`: `50889d751976da1a4f4c2b3335bdfb560d7597da8623085d0ee292211918f8df`.

- Pages 2–3: 300 seconds represents 20 trading days; a year has 240 days. There are five strikes (48–52), ten call/put contracts, one maturity. The overview's “10 strikes” conflicts with its instrument table.
- Page 2: RTM follows multiplicative arithmetic normal returns, `P[t] = P[t-1] * (1 + r[t])`, with zero mean return.
- Page 3: RTM and options have fixed $0.02 spreads. Market orders have enough depth to execute immediately without price impact. Full-size fills and unlimited depth are therefore published assumptions, not intrinsically unrealistic simulator conveniences.
- Page 3: the market maker uses Black–Scholes with zero variance risk premium, is less informed about changing volatility, and learns with a lag. The analysts are described as very effective. Current volatility is announced each week, with next-week estimates around the middle of the preceding week.
- Page 3: weeks are ticks 1–75, 76–150, 151–225, 226–300. The sample news table on page 4 is inconsistent, including ticks beyond 300. The parser therefore respects actual announcement timestamps and distinguishes confirmed current volatility from forecasts.
- Pages 3–4: delta limit ±7,000; fine $0.10 per excess unit per second. Option limits: 2,500 gross, 1,000 net, 100 per order; RTM: 50,000 shares, 10,000 per order. Fees: $2 per option contract, $0.02 per RTM share, multiplier 100.
- Page 4: positions settle at expiry. The bots voluntarily liquidate earlier; early liquidation is not a contest rule.

The PDF does not specify the market maker's learning speed or the distribution of forecast errors. A synthetic evaluation cannot identify those parameters from the prose.

## Research and the choice of algorithm

Public sources checked:

1. [LiChiLin / Rotman Trading Competition 2024 — Volatility](https://github.com/LiChiLin/Rotman-Trading-Competition-2024/tree/main/Volatility): a support/API example using a different 600-second case. It is not evidence of a winning strategy for this case.
2. [vins232 / RITC-2025-Volatility-Trading-Case](https://github.com/vins232/RITC-2025-Volatility-Trading-Case): Black–Scholes valuation, implied volatility, Greeks, hedge bands, and larger inventory limits. It uses multiple maturities and does not establish verified competition returns. Its order assumptions should not replace this repository's confirmed-fill reconciliation.
3. [laurachen-crr / RITC2022-Option-Volatility-Trading-Case](https://github.com/laurachen-crr/RITC2022-Option-Volatility-Trading-Case): spreadsheet/macro material, without enough documented results to rank it. No macros were executed.

There is no verified “best algorithm” or independently verified $100,000 average in these sources. The relevant common principles are news-based option valuation, executable fee-adjusted edges, portfolio Greeks, delta bands, and position sizing within the published limits. No external bot code was executed or copied wholesale.

The existing variance-integration model remains useful: remaining option value depends on future variance, not just this week's sigma. A current-sigma convergence target is another testable hypothesis about the maker's short-term quotes. The implementation keeps both models so they can be compared rather than assuming either one is universally correct.

## Changes

The baseline retains its default 10-contract entry limit and 100-contract gross cap. Its news parser now keeps confirmed current volatility separate from forecasts, including when a forecast's effective week begins before the next current-week announcement.

New optional policies share bounded sizing and execution:

| Policy | Valuation | Entry maximum | Gross / net options | Stock cap | Loss-trigger threshold |
|---|---|---:|---:|---:|---:|
| `baseline` | Remaining-variance low/high forecasts | 10 | 100 / 100 | 10,000 | $1,500 |
| `scaled_small` | Remaining-variance low/high forecasts | 10 | 100 / 100 | 10,000 | $1,500 |
| `scaled` | Remaining-variance low/high forecasts | 100 | 1,000 / 800 | 45,000 | $6,000 |
| `adaptive_small` | Current-sigma maker convergence | 10 | 100 / 100 | 10,000 | $1,500 |
| `adaptive` | Current-sigma maker convergence | 100 | 1,000 / 800 | 45,000 | $6,000 |

“Maximum” is not a fixed ticket size. The new policies scale desired size with edge and clip it for portfolio risk. They use a 1,200-delta hedge trigger, a 0.1-second idle, a 4,500 pre-hedge quoted-delta bound, a 6,000 current-sigma bound, and stock capacity checks. Before entry, they reprice options at three-sigma spot moves over at least three seconds (or the observed cycle duration if longer), with quoted/current sigma and ±25% volatility shocks. Stressed delta must stay within 6,500 before hedging and 5,500 after the intended hedge. These finite stress scenarios do not bound every possible loss or market move.

All actual orders still pass heat/freshness checks, receipt validation, known-order cancellation, and position reconciliation. Partial fills halt new entries and hedge confirmed inventory. Larger option tickets are explicitly enabled per client; legacy clients still cap at ten. Closeout uses at most 40 contracts per leg, budgets for stock tickets and measured cycle time, and refreshes hedges between legs.

If a market move exhausts stock-hedge capacity, the new policies clip the stock
hedge and reduce options. Emergency close selection first relieves current delta
excess, then compares the projected intermediate and stressed risk; it does not
simply close the largest contract count. A refreshed hedge snapshot cannot silently
authorize a decision using old news/time. Entries refresh an older decision and
check that its tick is still current immediately before submitting.

Loss thresholds trigger closing; they are not guaranteed maximum losses. Larger positions can lose much more than baseline. At this evaluation stage live defaults stayed unchanged and selecting a new policy required an explicit CLI flag; the subsequent user-selected default is `buffered`.

## Simulation methods

The original `standard` and `stress` scenarios are sensitivity tests, not reconstructions of the official market. They assume larger forecast errors, a maker that prices future variance and mean-reverts unknown volatility, additional order delays, and geometric returns. Their losses cannot establish that the strategy is bad under the published case. The older results are retained in `docs/mock-evaluation.md` and their immutable reports.

New scenarios `case`, `case_fast`, and `case_latency` use arithmetic normal returns, announced weekly sigma, forecasts at ticks 37/112/187, current-week updates at 76/151/226 (initial news available at tick 0), fixed two-cent spreads, immediate full fills at arrival, fees, limits, and delta fines. The maker's quote sigma converges toward the latest announced sigma. Unreleased future truth does not affect quotes.

| Unspecified parameter modeled as an assumption | `case` | `case_fast` | `case_latency` |
|---|---:|---:|---:|
| Maker response time constant, sampled per week | 5–15 s | 1–3 s | 5–15 s |
| Time per request | 40–80 ms | 40–80 ms | 100–180 ms |
| Additional order execution delay | 0 | 0 | 0 |
| Forecast error standard deviation | 1 percentage point, plus shared bias at 0.5 points | same | same |
| Forecast range half-width | 3 percentage points | same | same |

All scenarios have one-second market updates and separate random streams for prices, forecasts, maker quotes, and transport. Policies see the same external market path for a seed. The case scenarios include the recurring production validation, order-state, news, account, and heat reads when charging simulated time. CPU computation time is not charged to this virtual clock. Wall-clock speed changes only presentation pacing.

The `baseline_fast` local policy changes only the baseline idle from 0.6 to 0.1 seconds. `scaled_small` keeps baseline position and loss caps but changes sizing within those caps, risk admission, hedge band, and polling. `scaled` adds higher caps. These comparisons distinguish speed, decision rules, and exposure; dollar returns from `scaled` must not be described as a pure algorithm gain.

Pilot reports are development evidence, not final held-out validation. The final comparison uses new seed ranges after policy choices are fixed, retains all failures and losses, and stores source hashes. Official practice trading is still needed to calibrate maker behavior and latency.

## Speed and verification

The original pricing/risk microbenchmark (1,000 iterations) measured 2.25 ms median
and 2.76 ms at the 95th percentile. The revised strategy's broader flat-start
decision, including local reconciliation, measured 16.44 ms median and 17.31 ms at
the 95th percentile over 100 runs. These are different workloads and exclude real
network latency; neither result indicates that a language rewrite is the main
opportunity. Serial requests, polling idle, hedge churn, and position capacity
matter more to the end-to-end decision cycle.

The full regression suite passed 118 tests, including 20 new focused tests.
Ruff checks/formatting, compileall, and diff whitespace checks passed. Independent
review reproduced and helped correct stock-cap liquidation, emergency close
selection, stale refresh, and weekly return-boundary issues, then reported no
material remaining defects. Read-only practice preflight passed with the account
flat and no open orders; the server remained stopped at tick 0. No official
exchange orders were sent for this research.

## Final comparison results

600 synthetic heats: 20 exploratory seeds (60000–60019), 20 fresh held-out seeds (70000–70019), five scenarios, and three fixed policies. Each path is replayed across policies. The table below uses only the 20 held-out seeds per scenario. All P&L is net of fees and fines.

| Scenario | Policy | Mean P&L | Worst P&L | Losing runs | Mean fees |
|---|---|---:|---:|---:|---:|
| case | `baseline` | $+8,747 | $+98 | 0% | $1,274 |
| case | `scaled_small` | $+8,469 | $-623 | 10% | $994 |
| case | `scaled` | $+18,132 | $-275 | 5% | $7,406 |
| case_fast | `baseline` | $+9,000 | $+0 | 0% | $1,245 |
| case_fast | `scaled_small` | $+8,491 | $-2,165 | 5% | $986 |
| case_fast | `scaled` | $+22,372 | $+0 | 0% | $6,572 |
| case_latency | `baseline` | $+7,881 | $+38 | 0% | $1,064 |
| case_latency | `scaled_small` | $+7,811 | $+0 | 0% | $813 |
| case_latency | `scaled` | $+21,784 | $+0 | 0% | $3,890 |
| standard | `baseline` | $-1,441 | $-4,822 | 85% | $752 |
| standard | `scaled_small` | $-921 | $-3,887 | 75% | $611 |
| standard | `scaled` | $-6,006 | $-14,749 | 85% | $4,134 |
| stress | `baseline` | $-2,077 | $-5,486 | 85% | $731 |
| stress | `scaled_small` | $-1,784 | $-4,282 | 85% | $535 |
| stress | `scaled` | $-5,255 | $-17,182 | 75% | $2,950 |

The larger `scaled` policy roughly doubles or triples the baseline mean in these case-based held-out scenarios. The same-cap `scaled_small` policy does not beat baseline mean in those three scenarios. Its mean losses are smaller in the two adverse sensitivity scenarios, while larger sizing makes the adverse losses substantially worse. These results support higher *capacity* under favorable case assumptions, not a claim that the replacement decision rules are universally better. Baseline remained the live default at this stage, before the subsequent buffered-policy evaluation and user selection.

All 600 runs completed with strategy-confirmed flat inventory, zero delta fines, and no execution errors. This validates the tested local paths, not reliability under every official server condition. Both final report source fingerprints were checked against the delivered code.

The pilot reports used earlier code and seeds; their provisional held-out labels are not fresh validation for the subsequently revised strategy. No settings were changed after the final comparison began.

Final per-heat evidence:

- [360 case-based runs](../reports/volatility-scaled-case-evaluation-2026-11-19.json) (case, faster maker, higher request latency).
- [240 sensitivity runs](../reports/volatility-scaled-sensitivity-evaluation-2026-11-19.json) (standard, stress).
- [Research and verification audit](../reports/volatility-research-audit-2026-11-19.json).

## Monitored 300-second run

A full real-time run of `--scenario case --policy scaled --seed 1459556188` completed all 300 seconds, net P&L $+2,910.36, fees $7,145.88, zero fines, 147 fills, and strategy-confirmed flat inventory. The same case/seed produced +$6,428.45 with baseline and +$4,039.01 with scaled_small. The new larger policy underperformed on this particular path; its batch mean improvement must not be presented as a universal improvement. The user's original +$6,755.06 demo used different scenario mechanics and is not a matched comparison.

Local run: `logs/mock/mock-volatility-20261119T230440Z-157nfacu.json`.
