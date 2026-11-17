# Algorithm design and operating assumptions

The live entry point is `python3 -m ritcx`. Models are pure functions; bots choose actions; shared execution reconciles orders against server inventory. No model calls a converter. The algorithms target mechanisms described by the supplied cases, not price-history indicators.

## Volatility valuation

For each option, `T = max(300 − tick, 0) / 3600` years. This matches the official Python support script: 20 simulated trading days per 300 seconds, 240 trading days per year. Use the interest rate announced by the server, with European non-dividend Black–Scholes pricing and 100 shares per contract.

News is sorted by its tick and ID, and future-dated records are ignored. “Next week” announcements are placed in the next 75-tick interval. A forecast never replaces current-week realized volatility early. Low/high endpoints and their midpoint form three scenarios. Remaining variance is integrated:

```text
sigma_effective² = sum(duration_in_interval × sigma_interval²) / remaining_ticks
```

Unannounced weeks carry forward the latest available range. This is an explicit modeling assumption, not knowledge of later realized volatility. Tick boundaries follow the existing simulator convention 0/75/150/225; the PDF’s prose and sample announcement table are inconsistent, so actual news timestamps remain authoritative.

Long entries use the low-volatility fair value minus the ask. Short entries use the bid minus the high-volatility fair value. Deduct both option commissions, half the quoted option spread as an exit reserve, and 0.04 premium units for hedging/uncertainty. Require another 0.025 premium units of edge. These are conservative heuristics, not fitted optimal parameters. The bot tries the best admissible signal and takes one 10-contract step per decision.

The quote-implied-volatility solver uses bounded bisection. Implied volatility is used for the hedge delta, consistent with the original decision-support workbook’s intent; valuation uses analyst information. If quote IV is outside the numerical pricing interval, the hedge falls back to the current analyst estimate.

```text
portfolio_delta = RTM_shares + 100 × sum(option_contracts × option_delta)
hedge_change = −round(portfolio_delta)
```

A normal hedge requires at least 500 delta units. After a position change, the bot hedges more tightly. Admission checks include unhedged proposed positions, quoted deltas, and several volatility stress values, with a 6,000-unit internal bound against the published ±7,000 limit. Spot jumps can still create exposure between API observations.

Entry and exit rules differ. An existing long is closed once `fair_low − bid ≤ option_fee/100 + 0.01`; an existing short is closed once `ask − fair_high ≤ option_fee/100 + 0.01`. This retains small remaining value without reopening at the same threshold. New entries stop at 270. Close-out begins no later than 285, with an earlier start computed from remaining 10-contract tickets, one stock/reconciliation cycle, a three-second-per-cycle floor, 1.5 times the slowest of the last 20 complete cycles, and five seconds of slack. A full 100-contract book therefore reserves at least 38 seconds. Stop requests, confirmed partial-fill recovery, or the 1,500 loss trigger also initiate closing. After a confirmed option fill, the hedge refreshes the case and inventory and recomputes delta at the current tick; it does not reuse the original entry snapshot.

At active tick 0, usable news may not yet exist. A flat account waits. Missing usable news while positions exist stops the algorithm for reconciliation rather than guessing a hedge model.

### Put–call parity scanner

For each strike K, the diagnostic scanner evaluates executable synthetic packages against discounted strike cash:

```text
rich synthetic: 100 × (call_bid − put_ask − stock_ask + K exp(−rT))
cheap synthetic: 100 × (stock_bid − call_ask + put_bid − K exp(−rT))
```

It subtracts round-trip commissions for both options and 100 RTM shares and requires a further $3 margin. Signals appear in `parity_scan`. They are not executed: this implementation’s close-out policy would otherwise need separate package ownership, financing, and multi-leg exit validation. A positive scan is not a verified executable profit.

## ETF tender model

The model accepts fixed-price RITC offers only. The action is from the trader’s perspective: BUY adds a long ETF position; SELL adds a short position. Tender acceptance itself is modeled without a trading commission; subsequent market liquidation is charged the equity fee.

1. Require a positive integral size no larger than 100,000 units and at least two ticks before offer expiry; no new tenders at/after tick 250.
2. Project the full accepted quantity and its USD cash payment/receipt. Check cash and equity capacity before acceptance.
3. Sort the unwind side of the book, subtract each order’s filled quantity, remove own orders, and use only 80% of the remaining displayed volume. Require enough depth for the entire tender.
4. Compute full-book net USD profit after the liquidation fee. Require at least `max(500, 0.08 × quantity)` USD. The depth haircut and profit margin are buffers, not a guarantee that quotes persist.
5. Check the projected post-liquidation cash state, and full FX depth for converting estimated positive net USD proceeds into CAD. Cash and ETF value can offset during execution; the actual FX hedge uses their combined exposure.
6. Reserve `ceil(quantity/10000) × (slice_seconds + passive_wait) + 8` seconds before tick 275. `slice_seconds` is at least 1.5 seconds and increases to four times the slowest recent POST latency if that is larger. Passive mode adds 0.75 seconds per slice. This is a conservative scheduling estimate, not a hard upper bound on network time.
7. Re-read the case, books and inventory immediately before acceptance and rerun the test.

After acceptance, verify the position change. Direct liquidation refreshes book depth for every ticket of up to 10,000 shares and reconciles every fill. It stops with explicit unresolved inventory if visible liquidity disappears or a response is uncertain. It does not make an untested assumption that a basket hedge or manual conversion will complete the exit.

Optional passive execution quotes on the inventory-reducing side, waits at most 0.75 seconds, cancels the remainder, and reads the final order state. This handles a fill that arrives during cancellation. An incomplete passive attempt disables further resting attempts for that unwind; a short remaining time budget uses market execution immediately. No passive rebate is needed in the acceptance calculation. There is no post-only promise or full optimal-execution optimizer.

## ETF basket model

The economic relationship is one BULL share and one BEAR share per RITC ETF share:

```text
RITC_USD × CAD_per_USD = BULL_CAD + BEAR_CAD
```

A cheap ETF suggests buy RITC / sell BULL / sell BEAR. A rich ETF suggests the reverse. Pricing walks each equity book for the entire 500-unit proposed quantity. It also walks the appropriate FX book for the ETF’s **full USD notional**, not one dollar at the best quote. Fees are converted to CAD and a round-trip commission allowance is deducted before the 0.15 CAD/unit entry threshold.

Before sending any leg, test all seven nonempty subsets of the basket for intermediate equity and cash capacity. Every subsequent leg is checked again against reconciled inventory. Published weights count RITC twice and each constituent once. Live reported weights are checked separately: taking the larger individual weight alone does not guarantee a stricter *net* exposure when long and short legs offset.

A basket position must satisfy `BULL = BEAR = −RITC`. The maximum open basket size is 5,000 units. The exit trigger uses the midpoint CAD basis approaching within 0.03 of convergence, so actual exit spread/impact can reduce the captured profit. Hard close-out begins at 275 regardless of convergence. Without manual conversion, these are relative-value positions with basis risk.

FX exposure is:

```text
USD_cash + RITC_quantity × RITC_USD_midpoint
```

The normal hedge trigger is $1,000 of USD exposure; close-out hedges to less than one USD unit of rounding residual. Currency orders are capped at 2,500,000 units and projected cash is checked. A fully filled basket does not necessarily need an FX trade: the RITC cash payment and marked ETF value may already offset.

## Execution and lifecycle

- Default transport is DMA Basic authentication; explicit client REST profiles use `X-API-Key`.
- Requests are paced; read-only HTTP 429 responses follow the supplied retry wait within the operating budget. Mutations are not automatically retried, including a POST rejected with 429.
- Every mutation checks the bound active case, period and monotonic tick. New order/tender submissions also enforce a decision age of at most two ticks. Cancelling a known order can proceed on older price data, but still requires the same active heat.
- A receipt is not a fill ledger. Known partial orders are cancelled by ID, reread to include cancellation races, compared with actual positions, and checked for residual open orders before a replacement.
- An ETF basket partial fill aborts later legs and closes known inventory. Volatility partial recovery hedges actual known inventory and enters close-out mode. Uncertain responses stop execution; the system cannot safely invent a fill amount.
- Preflight requires a flat start, validated securities/fees and sufficient live limits. Contracts or smaller limits outside the implemented assumptions cause a stop.
- A local account lock spans launcher and child. Child errors stop the session. Ctrl-C requests close-out and final reconciliation.

All strategy parameters in this document are initial operating choices. They require evaluation across independent randomized heats before increasing size or describing them as optimal.
