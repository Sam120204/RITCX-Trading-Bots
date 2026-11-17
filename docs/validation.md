# Validation record — November 17, 2026

This record separates correctness checks, historical observations, and unverified strategy performance.

## Current implementation

The baseline had 45 tests. The integrated package passes **79 tests**, including:

- Black–Scholes reference prices, put–call parity, expiry, IV inversion, news/range parsing, future-week separation, and fee-adjusted signals.
- Whole-book tender valuation, a 20% depth reserve, acceptance cash, exit time budgets, and complete-notional FX pricing.
- Published and live weighted limits, every intermediate basket subset, and projected settlement fees/cash.
- Real local HTTP authentication, read-only enforcement, unknown/changed heats, stale decisions, non-retried failed submissions, known partial fills, and cancellation races.
- Partial ETF basket recovery; bounded passive attempts and cancellation before a market replacement.
- Full launcher/child executions against local HTTP simulators: a volatility entry/hedge/exit, an ETF tender acceptance/liquidation/FX hedge, and GET-only preflight.
- Waiting for initial news at active tick 0; heat resets and propagation of the endpoint/profile/account lock.
- A delayed confirmed option fill that advances tick 20→23 before its hedge, plus a delayed stock hedge before option liquidation.
- An advancing virtual clock charging **100 ms per HTTP request** while the actual volatility bot builds **100 option contracts** and subsequently closes all options and RTM before tick 300. The old fixed tick-285 liquidation failed this regression; the inventory/latency budget passes.
- Summary reports retain incomplete runs, losses and fines; truncated logs cannot count as successful heats.

The independent code review identified delayed-hedge freshness and insufficient liquidation time as material issues. Both were reproduced in tests and fixed. The closing path also defers a subsequent option close after a delayed initial stock hedge.

Commands:

```sh
python3 -m unittest discover -s tests -q
python3 -m compileall -q ritcx tests
python3 -m ritcx --help
python3 -m ritcx.volatility.bot --help
python3 -m ritcx.etf.bot --help
python3 -m ritcx.analytics --help
```

Ruff lint and format checks pass using the repository’s `pyproject.toml`. Ruff is a developer-only tool, not a runtime dependency. All eight files listed in `references/manifest.json` passed SHA-256 verification; the supplied CSV is byte-identical to the user’s file. Both PDFs and the four DMA/REST scripts match fresh official downloads.

## Real practice connectivity

A GET-only preflight using the existing private practice profile passed after the integration. The live server reported:

```text
Case: RITCx Volatility Trading Case
Status: STOPPED
Period/tick: P1, 0/300
Positions: none
Open orders: 0
NLV: 0
Fines: 0
```

This verifies current connectivity and the stopped-heat preflight path. **No real practice or competition orders were submitted during this integration.** The formal competition profile still requires the organizer’s assigned endpoint and credentials. Client REST authentication was tested locally; native Windows execution is outside this launcher’s supported runtime.

## Python performance

Reproduce the CPU-only benchmark with:

```sh
python3 -m ritcx.benchmark --iterations 1000
```

The saved [benchmark report](../reports/model-benchmark-2026-11-17.json) measured on Python 3.12/macOS ARM64:

| Workload | Median | 95th percentile |
|---|---:|---:|
| Ten-option scenario valuation, quoted-IV hedge, and one proposed risk check | 1.61 ms | 2.07 ms |

The benchmark excludes network, pacing, logging, reconciliation, and order execution. It supports keeping a small Python model; it does not measure end-to-end latency or establish superiority over C++. Prior practice notes reported roughly 43 ms per order response and 124–202 ms for snapshots before the new reconciliation controls. Those historical timings are not current guarantees.

## Historical results are not validation of this revision

The original practice reports remain in `reports/practice-2026-11-17.json` and `reports/etf-practice-2026-11-17.json`. Earlier notes reported approximately +2,144.08 for one volatility run and +89.352 CAD for one settled ETF run, both without fines, plus an ETF heat with no trades. Those runs preceded this integration and do not prove its profitability.

There is no genuine historical price dataset in the provided CSV. It is a disconnected decision-support export; no backtest was fabricated from it. Performance qualification requires fresh randomized heats, including thin liquidity, bad forecasts, rate limiting, adverse moves and realistic network failures. Compare median and worst outcomes, fines, unresolved inventory, rejected orders and latency; retain failed runs and evaluate parameter changes on new paths.

## Remaining limits

- Tests demonstrate implemented behavior under their scenarios, not guaranteed fill quality, profit, or completion after an outage.
- Market orders can move beyond a previously read book. A depth reserve cannot guarantee future liquidity.
- Tender timing is estimated from ticket count and recent latency; sudden stalls can invalidate it.
- Basket positions retain convergence and execution risk. Conversion is not automated.
- Passive execution is opt-in and has no live fill demonstration in this revision. Put–call parity is diagnostic-only.
- The launcher rejects unsupported cases/contracts/fees and smaller position limits rather than inventing new strategy parameters.
- Automatic recovery handles only reconciled, known partial fills. Unknown submission outcomes require account reconciliation before restart.
- Local process locking cannot prevent another computer or an unrelated program from trading the same account.
