# Competition sources and rule audit

Reviewed November 17, 2026 against the linked event page and fresh downloads of both PDFs. The PDFs and both existing DMA support scripts match the downloads byte-for-byte. REST support scripts are also archived. `references/manifest.json` records source URLs and hashes. Original third-party copyright notices remain intact; those files are not relicensed under this repository’s license.

## Event and connections

The [event page](https://www.rotman.utoronto.ca/faculty-and-research/education-labs/bmo-financial-group-finance-research-and-trading-lab/finance-research-and-trading-lab/events/ritcxuoft/) says both cases use randomized paths between iterations and each contributes 50% of the score through heat/case ranks. Only one team member participates in the ETF case. The event recommends client REST for Windows stability and supplies DMA examples for browser/Mac participants.

Website client/browser login ports are not automatically DMA API ports. The existing local mock profile used `flserver.rotman.utoronto.ca:16520/v1`; the archived scripts contain other sample DMA ports. On November 19 the event page’s ETF Mac/browser port also served the authenticated practice DMA API at `http://flserver.rotman.utoronto.ca:16500/v1`. The configured 16520 endpoint then served Volatility. Neither endpoint establishes the formal competition assignment. Use organizer-provided endpoint/authentication information and pass preflight each time the profile changes.

## Volatility case

The [published PDF](https://rotmanfrtl.github.io/RITCx-Volatility%20Trading%20Case.pdf) specifies:

- One 300-second heat represents one 20-day month; a year has 240 days.
- RTM plus calls and puts at strikes 48, 49, 50, 51, 52; European options and 100 shares per contract.
- Weekly volatility changes and advance estimates; the market maker learns with a lag.
- No liquidity constraint; quoted RTM/option spreads of $0.02.
- Published position caps: 50,000 RTM shares, 2,500 gross / 1,000 absolute net option contracts.
- Tickets: 10,000 shares or 100 contracts. Fees: $0.02/share and $2/contract.
- Delta must stay within ±7,000; fine $0.10 per excess delta unit per second.
- Terminal RTM closes at last price and options settle to intrinsic value. Early liquidation is our operating policy.

The PDF overview says “10 different strike prices” but its table lists five strikes and ten contracts. The event page also says five strikes. The sample news table includes times past 300 despite the stated heat length. The implementation uses the actual instrument universe, 300-second maturity and news timestamps, with the 75-tick weekly structure.

## ETF case

The [published PDF](https://rotmanfrtl.github.io/RITCx-Algorithmic%20ETF%20Arb%20Case.pdf) specifies:

- One 300-second heat. Ordinary trades must be submitted algorithmically.
- CAD base currency; BULL and BEAR quote in CAD, RITC in USD; USD quotes CAD per USD.
- Equilibrium: `RITC × USD = BULL + BEAR`.
- Full tender positions require liquidity/impact management.
- Equity tickets at most 10,000; currency tickets at most 2,500,000.
- Market equity fee $0.02/share; stated passive rebate $0.01/share.
- Position weighting: ETF twice, each constituent once. Numerical position caps are queried from the live server; this bot additionally applies lower internal caps.
- Creation/redemption exchanges 10,000 ETF units for 10,000 of each constituent, costing $1,500 USD/use. **Converters are manual-only; automatic API use is excluded.**
- Terminal positions can settle at last/corrected value; statistical-arbitrage positions need not be closed before expiry. Our bot closes proactively.

Earlier practice observations recorded a 0.5 live RITC weight and $0.03 passive rebate. They do not override the PDF. Both live and published exposure weights are checked independently; no passive rebate is assumed in acceptance valuation.

## Workbook and CSV

The original macro workbook is retained as an unchanged reference. The earlier extracted VBA and member list remain under `references/notes`; no macros were executed in this integration. The workbook uses Windows `rit2.rtd`, manually updated volatility, and quote-implied deltas. Its maturity rounds up to whole simulated days, while the official Python example uses continuous `(300−tick)/3600`; this bot follows the Python clock.

The original VBA IV routine has a questionable derivative calculation: its price function does not branch on the documented price/d1 selector, and standard vega uses a normal density rather than a CDF. The production implementation uses an independently tested bounded bisection solver rather than importing those macros.

The user-supplied CSV is an unchanged export of one sheet, with cached errors and disconnected prices. Its explanatory formula strings are retained for reference; exported row numbers are not a reliable runnable spreadsheet model. It supplies no historical price series for backtesting.
