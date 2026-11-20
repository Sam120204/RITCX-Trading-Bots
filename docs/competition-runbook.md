# Competition runbook

The November 20, 2026 event ran in Toronto time. The Python launcher uses the standard library and requires Python 3.10 or newer.

## Set up before the first heat

Run commands from the repository root. On a fresh checkout, create private profiles from [the contest template](../.env.contest.example):

```sh
(umask 077 && cp -n .env.contest.example .env.contest && cp -n .env.contest.example .env.contest.etf)
```

Use `.env.contest` for your individual Volatility login. Use `.env.contest.etf` for the shared ETF login, with one teammate running the ETF bot. Set the credentials and the assigned `RIT_API_URL` in each profile. The URL includes the host, port, and `/v1` suffix. Both private profiles are ignored by Git.

During the connectivity test, confirm the relevant endpoint with the organizers. For the browser or Mac app, use the browser/DMA port. The November 20 setup used `flserver.rotman.utoronto.ca` with DMA port `16520` for Volatility and `16500` for ETF.

## Check the assigned case

Run the matching read-only preflight:

```sh
python3 -m ritcx volatility --contest --preflight
python3 -m ritcx etf --contest --config .env.contest.etf --preflight
```

Check for `PREFLIGHT PASSED (read-only)`, the expected case and account, and a flat account with no open orders. `STOPPED` at tick 0 is normal before a heat.

## Run the heats

Once the organizers give the go-ahead, run the matching command. Each case had eight 300-tick heats in the 2026 competition.

```sh
python3 -m ritcx volatility --contest --trade --rounds 8 --max-seconds 4500
python3 -m ritcx etf --contest --config .env.contest.etf --trade --rounds 8 --max-seconds 4500
```

Keep Terminal open between heats. The launcher starts trading as soon as it sees an eligible `ACTIVE` heat and validates the account. The 75-minute session budget includes time spent waiting.

## Watch P&L and recover from an interruption

`HEAT_PNL` reports the change in account value since entry for each reconciled heat. `average_pnl` covers the results from the current launch. Session and trade logs are saved under `logs/`.

To stop early, press **Ctrl-C** once and let the bot reconcile. If the launcher exits unexpectedly or reports unresolved inventory or orders, check the RIT account and follow the organizers’ instructions for reconciling any open positions. Run the matching preflight again before restarting, and set `--rounds` to the number of heats still to run.
