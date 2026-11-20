# Follow-up: reduce trading around the risk limit

> Historical report. Buffered is now the only supported volatility strategy; commands or alternatives described below refer to earlier revisions. Use the current README for runnable instructions.

**Deployment selection:** after this evaluation, the user selected `buffered` as
the default. The launcher and standalone volatility bot now select it automatically;
the local dashboard defaults to `case` plus `buffered`. The historical baseline
remains selectable explicitly. The evaluation below records the development stage
before that default selection; its numerical policies are unchanged.
The default-selection checks passed 121 tests, including the real launcher/child
workflow, standalone closeout under an advancing clock, local dashboard defaults,
and explicit selection of the original baseline.

The previous `scaled` policy ran successfully, but it was not a consistent
improvement. On seed 1459556188 in the `case` scenario it earned $2,910.36,
compared with baseline's $6,428.45. This follow-up addresses an observed cause
of its high trading costs without increasing its exposure limits.

## Diagnosed mechanism

The saved event log contained 15 risk-reduction decisions closing 600 option
contracts. Those decisions and their hedges cost $1,699.40 in commissions.
Several were followed by a new entry in the same option one or two seconds
later. Examples included closing 40 RTM52C at tick 124 and buying 30 at 126,
and buying back 40 short RTM52P at 195 and selling 37 again at 196.

Entry and holding checks used the same stressed-risk thresholds. A position
could be opened at the limit, cross it after a small market move, be cut, and
then be rebuilt as soon as the cut restored capacity. Fees and repeated hedge
adjustments consumed the apparent valuation edge.

## Narrow change

`buffered` uses the same valuation, position limits, hedge band, polling,
loss-trigger threshold, reconciliation, and closing logic as `scaled`. It
requires new entries to fit within 75% of the old stressed-risk thresholds:
4,875 before the stock hedge and 4,125 after it. Existing positions still use
the original 6,500 and 5,500 thresholds. No limit is relaxed.

The resulting space between opening and closing thresholds reduces repeated
trading near a boundary. `buffered_small` applies the same rule with baseline's
100-contract gross cap. Historical policies retain their original settings.
During evaluation the live default remained `baseline`; the user subsequently
selected `buffered` as described above.

## Reproduction path

All rows below use `case`, seed 1459556188, 300 simulated seconds, and include
all commissions and fines. The follow-up was replayed at accelerated speed;
the default dashboard command below paces the same simulation to 300 real seconds.

| Policy | Net P&L | Fees | Max drawdown | Orders | Risk-reduction decisions |
|---|---:|---:|---:|---:|---:|
| Baseline | +$6,428.45 | $1,565.96 | $1,805.11 | 104 | 0 |
| Previous `scaled` | +$2,910.36 | $7,145.88 | $5,344.25 | 147 | 15 |
| New `buffered` | +$9,132.63 | $4,011.32 | $2,489.05 | 94 | 3 |
| `buffered_small` | +$4,039.01 | $1,234.04 | $3,857.99 | 84 | 0 |

The large-policy change reduced fees by 43.9%, reduced maximum drawdown by
53.4%, and used at most 370 gross options compared with the previous 458.
All four runs completed flat with zero fines. This single known path is a
diagnostic replay, not independent performance validation. The same-cap result
also shows that higher dollar P&L relative to baseline still depends on the
larger position allowance.

```sh
cd "/Users/samzhong/Documents/UofT/RITCxUofT"
caffeinate -i python3 -u -m ritcx.mock --scenario case --policy buffered --seed 1459556188
```

For a new random path omit `--seed 1459556188`. A local simulation does not
connect to the official practice account. Maker lag and forecast distributions
remain assumptions; see the [case audit](volatility-research-2026-11-19.md).

## Fresh validation

400 runs compared four fixed policies on five scenarios, using exploratory seeds 80000–80009 and fresh held-out seeds 90000–90009. Parameters were fixed before starting. The table shows mean net P&L on the ten held-out seeds per scenario; it does not combine those with the exploratory results.

| Scenario | Baseline | Previous scaled | Buffered small | Buffered |
|---|---:|---:|---:|---:|
| case | $+7,865.85 | $+10,713.48 | $+6,216.14 | $+14,897.35 |
| case_fast | $+7,207.04 | $+15,764.92 | $+6,016.82 | $+17,826.07 |
| case_latency | $+7,326.98 | $+20,451.46 | $+5,025.78 | $+17,563.90 |
| standard | $-2,231.04 | $-6,122.62 | $-739.27 | $-3,778.55 |
| stress | $-3,212.56 | $-8,343.02 | $-1,684.22 | $-7,164.76 |

Buffered improved held-out mean P&L versus scaled in the main and fast-maker case scenarios, but reduced mean P&L in the higher-latency scenario. Average adverse-scenario losses were smaller, but worst individual losses were not uniformly improved: buffered lost $13,503 in the worst held-out standard run and $18,738 in stress. The larger policy is still sensitive to market assumptions. The same-cap version did not beat baseline in the case scenarios.

All 400 runs completed with the strategy flat, zero fines, and no execution errors. Current source hashes matched both reports. All 119 unit tests passed, Ruff/format/compile checks passed, and independent review found no material defect in the narrow change. A regression mutation removing the entry reserve made the new test fail as intended.

Read-only official-practice preflight passed for buffered; the server remained STOPPED at tick 0 with flat inventory and no open orders. These results are simulated, not official exchange performance.

Evidence:

- [240 case-based runs](../reports/volatility-buffered-case-evaluation-2026-11-19.json)
- [160 sensitivity runs](../reports/volatility-buffered-sensitivity-evaluation-2026-11-19.json)
- [Original-seed replay and fee attribution](../reports/volatility-buffered-replay-2026-11-19.json)
