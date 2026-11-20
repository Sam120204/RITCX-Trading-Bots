# Exact uploaded straddle versus the current buffered trader

Both strategies traded the same underlying path within each pair. Each account started flat and P&L includes commissions, delta fines and terminal settlement. The deployed policy was preserved throughout the evaluation.

| Market | Paired heats | Uploaded straddle mean | Buffered mean | Straddle wins | Losing heats, straddle / buffered | Worst straddle heat | Worst buffered heat |
|---|---:|---:|---:|---:|---:|---:|---:|
| Supplied simulator, seven full-fee scenarios | 2,100 | $111,956.46 | $20,765.92 | 2,054 | 37 / 66 | −$29,104.03 | −$18,636.54 |
| Repository main case | 100 | $74,412.80 | $15,933.44 | 98 | 2 / 1 | −$18,091.01 | −$194.84 |
| Repository fast maker | 100 | $76,612.41 | $18,164.94 | 94 | 6 / 1 | −$18,784.42 | −$1,601.68 |
| Repository higher request latency | 100 | $72,842.47 | $16,326.58 | 97 | 3 / 2 | −$16,387.37 | −$3,953.80 |
| Repository standard sensitivity | 100 | $645.51 | −$2,800.96 | 56 | 44 / 72 | −$68,282.74 | −$14,013.70 |
| Repository stress sensitivity | 100 | −$59,298.45 | −$4,584.28 | 7 | 94 / 85 | −$187,342.92 | −$16,662.98 |

On the main repository benchmark the uploaded straddle's median net P&L was **$70,898.30**, versus **$13,523.52** for buffered. The uploaded straddle reached $60,000 in 58 of these 100 heats. On the supplied full-fee markets, **1,566/2,100** straddle heats reached $60,000, versus **52/2,100** buffered heats.

## How the comparison was verified

- **Supplied simulator.** The seven markets share seeds 1–300 across scenarios. We matched each buffered outcome to the same scenario and seed in the downloaded trader's original reproduction. The script and market hashes match the supplied files, and 24 spot reruns across all eight scenarios reproduced the saved P&L to full precision. This simulator calls each strategy once per tick and executes orders within the tick with zero simulated latency. The half-fee scenario is excluded from the primary comparison because the current bot refuses that schedule. All 300 half-fee outcomes are available in the underlying reports. [Original straddle reproduction](../downloaded-volatility-reproduction-2026-11-19.json) · [Buffered replay](results.json)
- **Repository simulator.** The original `vol_algo.py` ran its own `run_live` loop with its default configuration through an in-process REST adapter. The current strategy ran through the repository's buffered heat runner. Each strategy makes its own reads, orders and polling decisions. Both see the same price and news paths, fee schedule, limits, per-request timing distribution and settlement rules. Each of five scenarios used seeds 101000–101049 and 111000–111049. Seeds are reused across scenarios, and results are reported separately. The main case charges 40–80 ms per request, the higher-latency case charges 100–180 ms, and the sensitivity scenarios also delay order arrival. [Paired outcomes and source hashes](exact-comparison.json) · [Replay code](../../tools/compare_volatility.py)
- **Why the previous 74/100 result differs.** The archived benchmark used the repository's `ritcx/volatility/straddle.py`, with a $6,000 loss trigger, the repository's order workflow and pre-expiry liquidation. The submitted `vol_algo.py` uses its own order loop and can carry positions into settlement. The current comparison runs both strategies with their own exposure and exit policies. The old result describes a different implementation. [Historical benchmark](../volatility-judge-rerun-2026-11-19.md)

The repository runs retained 3 rejected straddle order requests and 0 buffered runs with errors in the terminal P&L totals. All 500 straddle runs parsed the seven expected news items. Both simulators assume sufficient liquidity to fill accepted market orders. Official head-to-head performance remains unmeasured.

To reproduce the repository comparison from this checkout:

```sh
python3 tools/compare_volatility.py --bundle ~/Downloads/ritcx-vol-algo --workers 8
```

## Buffered strategy on the UofT practice server

On November 19, 2026 (Toronto time), one launcher followed two consecutive heats on the official Volatility practice DMA endpoint, port 14960. Both accounts started flat and the strategy finished trading at tick 285. Final reconciled P&L was **+$21,169.77** and **+$27,006.72**, averaging **+$24,088.25 per heat**. Both final reads showed zero fines, zero positions, and zero open orders. Only the buffered strategy traded these two practice heats.
