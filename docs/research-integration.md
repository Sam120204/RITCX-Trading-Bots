# Supplied research: implementation map

Source: the strategy notes supplied in this chat on November 17, 2026. They prioritize case-specific mechanisms and explicitly do not claim a verified ranking of winning bots. The official rules remain the authority for allowed actions.

| Research recommendation | Implementation / status |
|---|---|
| Shared risk and execution first | `ritcx/api.py`, `execution.py`, `runtime.py`: paced transport, read-only enforcement, account lock, stale decisions, known-fill reconciliation, bounded cancellation, and no blind POST retry. |
| News-driven options valuation | `volatility/model.py`: rate/news parsing, current/future separation, remaining-variance scenarios, bid/ask edges, and standard contract multipliers. |
| Select strongest robust edge | Sort net opportunities; buy against low fair value and sell against high fair value; admit one small eligible position change per cycle. |
| Delta-triggered no-trade band | `volatility/bot.py`: 500-unit trigger between trades, tight hedge after fills, 6,000-unit stress admission bound. Separate entry and exit thresholds reduce churn. |
| Evaluate full tender and inventory | `etf/model.py`: whole quantity, remaining displayed depth, 20% depth reserve, cash and equity projections, offer expiry and unwind time estimate. |
| Adaptive inventory-reducing liquidation | `etf/execution.py`: fresh depth per slice, known-fill updates and FX hedging. Optional `--passive-tenders` rests briefly, cancels/reconciles, then crosses if necessary. |
| Executable ETF/basket discrepancies | `etf/model.py`: quantity-dependent equity prices and full-notional FX book valuation, commission reserve, and separate live/published risk weights. |
| Hedge actual fills; avoid unresolved legs | Known partial basket entries abort subsequent legs and unwind actual positions. Unknown outcomes stop. Every replacement requires reconciliation first. |
| Put–call parity | Integrated as a fee-adjusted scanner in volatility snapshots; execution intentionally remains disabled pending package-specific testing. |
| Manual converters as an alternative | Official files retained and rule documented. No converter API/UI automation; no hybrid manual-conversion workflow while the bot owns the account. |
| Measure consistency and adverse outcomes | `analytics.py`: completed-heat median, worst completed and observed P&L, fines, rejected requests, unresolved runs, and order latency. |
| Avoid overfitting to successful examples | Historical reports retained separately; no parameter optimization or profitability claim made from those few old heats. |
| Starter-code traps | No unconditional tender acceptance, no fixed 25% volatility, no last-price entry signal, no unweighted ETF net-risk shortcut, no uncertain-order retry loop. |

The research’s academic references motivate band hedging, inventory-aware execution, and independent validation. The implementation does not claim to reproduce an Avellaneda–Stoikov market maker or Almgren–Chriss optimizer. It uses a bounded inventory-reducing passive attempt suited to this short heat. General passive market making, automatic parity package execution, and human-converter coordination need their own strategy ownership, failure handling and fresh-heat evidence before activation.

The supplied CSV is incorporated unchanged under `references/volatility-decision-support.csv`. It is a static export of a decision-support sheet, not training data. Its 300-second timer, 20-day month, 240-day year, ten options, manual volatility input and ±7,000 delta limit informed the rule audit. Cached `#VALUE!` / `#NUM!` cells and disconnected RTD values are never used as market observations.
