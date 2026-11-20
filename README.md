# RITCxUofT competition runbook

Friday, November 20, 2026 · 9 AM–1 PM Toronto time. Use Python 3.10 or newer on your Mac. Both cases run with the Python standard library.

## Set up before the first heat

Open Terminal in the competition folder and edit the private contest file prepared on this Mac.

```sh
cd "/Users/samzhong/Documents/UofT/RITCxUofT"
nano .env.contest
```

On this Mac, the private file has your saved trader ID and password. Its starting URL is the **ETF practice server**. At the connectivity test, confirm the organizer-assigned competition DMA URL, including the port and `/v1`, for the case you are about to run. Save edits with **Ctrl-O**, **Enter**, then **Ctrl-X**. If the next case has a different endpoint, update the URL before launching it.

On a fresh checkout, first create the private file with `(umask 077 && cp -n .env.contest.example .env.contest)`. Git ignores this file.

## Run the scheduled case

**Volatility Trading** — trades RTM options on analyst volatility forecasts and hedges with RTM shares.

```sh
python3 -m ritcx volatility --contest --trade
```

**Algorithmic ETF Arbitrage** — evaluates RITC tenders, unwinds accepted inventory over about 30 ticks, and trades small basket discrepancies.

```sh
python3 -m ritcx etf --contest --trade
```

Start the matching command shortly before the heat and keep Terminal open. It reads `.env.contest`, checks the server’s case name, and waits if that case is stopped. When the heat is active, it checks the rules, positions, and open orders before launching. If the case name differs, it exits and prints the server’s case name so you can check the URL.

**Running several heats:** each command above runs one 300-tick heat. For one launch per case, append `--rounds 3 --max-seconds 4500` and replace `3` with the announced heat count for that case. Keep the terminal open between heats. The 75-minute deadline includes waiting time. The organizers list case blocks at **9:45–10:45** and **11:00–noon** Toronto time and do not give a heat count in the public schedule. [Event schedule and scoring](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/ritcxuoft/)

**Watching P&L:** `pnl` is the change in account value since entry into the heat. After each reconciled trading heat, `HEAT_PNL` prints its result and `average_pnl` for that case in this launch. A new launch starts a new average. The official score uses the average of your **ranks** across heats. To stop early, press **Ctrl-C once** and let the bot reconcile. If it reports unresolved positions or orders, check the RIT account before restarting. Session and trade logs are in `logs/`.

## Results and code

- **ETF** earned **CAD +40,199 per heat on average** over four full UofT practice heats, with zero fines. The same strategy averaged CAD +50,355 across 106 recorded-quote replays, with 12 losing heats. Competition P&L will depend on the tenders and fills in your heat. [ETF comparison and practice results](reports/etf-strategy-comparison-2026-11-19.md)
- **Volatility** earned **+$24,088 per heat on average** in two consecutive UofT practice heats, with zero fines. The buffered strategy averaged +$15,933 on the 100-heat main synthetic benchmark. The uploaded straddle earned more on that benchmark, while buffered had smaller losses in the stress test. [Volatility comparison](reports/volatility-buffered-replay/comparison.md)

The trading code lives in [ritcx/volatility](ritcx/volatility) and [ritcx/etf](ritcx/etf). Offline replay scripts live in [tools](tools), with [tests](tests), [official case materials](references/official), [algorithm details](docs/algorithms.md), and [archived research notes](docs/archive) alongside them.
