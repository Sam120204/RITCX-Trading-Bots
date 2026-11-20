# Volatility-only rerun under the judge ranking rule

> Historical paired evaluation. Buffered has since been retained as the only runnable volatility strategy. The discarded strategy’s results remain here as evidence for the selection.

Run date: November 19, 2026. Main synthetic `case` scenario only. Both production policies were fixed throughout the rerun. 100 fresh matched market paths per method, 200 strategy runs total. Seeds 101000–101049 and 111000–111049 do not overlap the earlier saved evaluation ranges. No strategy code or default setting was changed. No remote trading was performed.

## All 100 fresh matched heats

| Metric | Buffered | Straddle |
|---|---:|---:|
| Mean net P&L per heat | $15,933.44 | $6,183.30 |
| Median net P&L per heat | $13,523.52 | $-5,479.90 |
| Total net P&L | $1,593,344.47 | $618,329.97 |
| Worst heat | $-194.84 | $-12,398.68 |
| Best heat | $47,013.14 | $99,884.77 |
| Losing heats | 1 | 64 |
| Head-to-head wins | 74 | 26 |
| Average heat rank | 1.260 | 1.740 |
| Final two-method case rank | 1 | 2 |
| Two-method case points | 2 | 1 |
| Execution errors | 0 | 0 |
| Confirmed flat before expiry | 100 | 100 |

P&L includes fees and fines. Ordinal scoring does not create a separate dollar P&L: it maps each heat’s net P&L to a rank, then averages heat ranks. With only two methods, first/second get 2/1 case points. Those points are illustrative, not predicted points against the actual competition field. No ETF contribution or 50/50 weighting is included.

## Split disclosure

| Split (50 heats per method) | Buffered mean P&L | Straddle mean P&L | Buffered / straddle heat wins |
|---|---:|---:|---:|
| exploratory | $16,403.29 | $12,112.68 | 33 / 17 |
| held_out | $15,463.60 | $253.92 | 41 / 9 |

The harness labels the first block exploratory and the second held-out; both were freshly generated and neither was used to tune parameters in this rerun. The combined sample and both blocks are disclosed to avoid selecting the favorable block. Results depend on synthetic assumptions and do not guarantee live P&L or an actual contest rank.

## Reproduce

```sh
python3 -m ritcx.mock_evaluate --workers 4 --seeds 50 --scenarios case \
  --policies buffered straddle --exploratory-start 101000 --held-out-start 111000 \
  --output reports/volatility-judge-rerun-2026-11-19.json
```

Ranks use strict comparisons, so tied P&Ls receive the same rank. Case points use N − rank + 1 with N = 2. Source hashes were checked against the final code after the run.

- [All raw runs](volatility-judge-rerun-2026-11-19.json)
- [Per-heat rankings and summaries](volatility-judge-scoring-2026-11-19.json)
