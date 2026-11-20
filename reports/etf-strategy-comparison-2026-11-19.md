# ETF strategy comparison, November 19, 2026

The production launcher uses the tender-first glide with a 30-tick neutral horizon and NAV sensitivity of 1. Both the launcher and the local ETF simulator call the same decision code. The original immediate-unwind strategy can be reproduced from commit `0ad20f6`. The only alternative exit mode in the runner is the requested expensive-execution contingency, `--hold-to-close`.

## Paired recorded-quote replay

All figures are CAD per heat. Both repository versions ran their actual ETF bot entry points on the same 106 tender-bearing heats, containing 845 offers. The 25 heats without tender records were excluded from every row. The 20-tick candidate used NAV sensitivity 0.5. The previous-bot comparison includes changes in tender selection, basket sizing, FX hedging and settlement. The two glide rows differ only in horizon and NAV sensitivity.

| Configuration | Mean | Median | 10th percentile | Worst | Losing heats | 30-tick glide wins against row |
|---|---:|---:|---:|---:|---:|---:|
| Previous repository bot | 14,112 | 12,690 | −6,730 | −22,115 | 23/106 | 88/106 |
| 20-tick glide | 45,234 | 39,443 | −3,548 | −69,767 | 13/106 | 69/106 |
| **30-tick glide** | **50,355** | **50,226** | **−1,729** | **−58,907** | **12/106** | — |
| Selective hold-to-close | 68,357 | 66,041 | −15,989 | −106,824 | 21/106 | 33/106 |
| 30-tick glide with CAD 2,500 loss trigger | 24,643 | −4,746 | −18,610 | −53,080 | 61/106 | 55/106, 45 ties |

The 30-tick glide improved mean, median, 10th percentile and loss frequency over the 20-tick candidate. Its worst loss was almost three times the prior repository bot’s. Hold-to-close had the highest mean and won 73 of 106 paired heats against the glide. I selected the glide for its fewer losing heats and better lower tail. A head-to-head comparison gives no field ranking without the other competitors’ P&Ls.

The old CAD 2,500 mark-to-market trigger often liquidated a large tender or basket after a small adverse move. It is now opt-in. Position limits, intermediate cash checks, account locking and order reconciliation remain active. The risk of a large loss is material even with these controls. The cost contingency is intended for measured tender market exits above about 0.10 USD/share. In this replay those exits averaged 0.0685 USD/share for the selected glide, weighted by confirmed fill quantity.

## What the replay measures

The attached market supplied recorded quotes and tender terms, 20-level synthetic books, fees, rebates, partial passive fills and decaying temporary impact. The driver reseeds depth for each heat and tick, charges a simulated 40 ms per read or cancellation and 90 ms per submission, and runs the production order, risk and reconciliation logic in-process. It supplies the official case name, CAD ledger and cash limit expected by the repository. It settles at the attachment’s terminal NLV marks and accepts the previous bot’s unpriced fixed-tender POST. All rows completed 106 heats. The final comparison has one paired row per heat. [Per-heat results](etf-replay-2026-11-19.json)

The bundle’s separate stylized policy replay reproduced its 20-tick mean of 55,064 **USD**, with an assumed impact curve. Its reported 61,560 **CAD** is a separate author-run mock result. Recorded practice prices were used to choose the candidate settings. The 106 heats are an in-sample comparison. Real queues, other traders’ responses, endpoint latency and official settlement can produce different results. [Practice log source](https://github.com/oxiwastaken/mscf_comp)

## Reproduction

Use the original supplied bundle containing `mock_rit_server.py` and `replay_heats.json`. The SHA-256 digest of the quote file is `7fbe6e363c5299782f44d1dedd820ee39697382d206829ffdd67f2a8c85c202a`.

```sh
bundle="$HOME/Downloads/ritcx_etf_algo"
base_dir=$(mktemp -d)
git archive 0ad20f6 ritcx | tar -x -C "$base_dir"
python3 tools/replay_etf.py --checkout "$base_dir" --bundle "$bundle" --output /tmp/previous.json
python3 tools/replay_etf.py --checkout . --bundle "$bundle" --output /tmp/glide.json
```

Add `--horizon 20`, `--hold`, or `--loss-limit-cad 2500` to the last command to reproduce the other rows. Horizon overrides are available only in the offline comparison tool.


## Official UofT practice session

The official ETF practice DMA endpoint was `http://flserver.rotman.utoronto.ca:16500/v1`. It reported a 300,000 stock gross limit, a 200,000 net limit and an RITC weight of 0.5. The bot also enforced the published RITC weight of 2. The full read-only observation covered ticks 1–299. It saw 8 tenders, of which 5 passed the gate at least once, and made zero submissions.

The first trading heat exposed delayed publication of tender inventory. The server acknowledged a 75,000-share buy before its first securities snapshot showed the change. The bot stopped. A fresh read confirmed exactly the purchased quantity and zero open orders. A supervised liquidation incurred a loss of about CAD 7,565 and left USD 0.48 in rounding residual. I corrected tender reconciliation to poll the account after an acknowledgment, and corrected FX reconciliation for sub-millionth-unit differences between decimal server balances and binary floating-point arithmetic. Neither recovery resubmitted the tender. After confirming a flat account, the revised bot resumed that heat. Its combined settled P&L was CAD +14,134.75 with zero fines. That interrupted heat is excluded from the three below.

| Full heat after repairs | Mode | Settled P&L (CAD) | Tenders accepted/seen | Priced tender market-exit shares | Fee-inclusive USD/share | Fines (CAD) | HTTP 429s |
|---|---|---:|---:|---:|---:|---:|---:|
| 1 | 30-tick glide | +71,490.84 | 7/8 | 336,392 | 0.0421 | 0.00 | 0 |
| 2 | 30-tick glide | +50,893.96 | 4/8 | 91,146 | 0.0754 | 0.00 | 0 |
| 3 | 30-tick glide | +29,075.37 | 3/8 | 77,225 | 0.0387 | 0.00 | 0 |

The three full heats settled for **CAD +151,460.18 combined**, with a share-weighted market-exit cost of **0.0476 USD/share**. Each final account read confirmed flat equity inventory, USD cash below one unit, and zero open orders. These were separate randomized practice heats. There was no simultaneous live run of the retired bot.

A fourth full ETF practice heat settled at **CAD +9,337.59** with three tenders accepted, zero fines, flat positions and zero open orders. Across all four uninterrupted heats the total was **CAD +160,797.76** and the arithmetic mean was **CAD +40,199.44** using unrounded server values. The read-only observations and the interrupted recovery heat are excluded.
