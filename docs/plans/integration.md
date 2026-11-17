# Research integration implementation plan

Goal: integrate the supplied RITCx research into the two existing algorithms, preserve official sources, and provide a single reliable 300-second heat launcher.

Architecture: a standard-library Python package with shared API, execution, profile/lock and timing controls; separate volatility and ETF models/bots; one session launcher and offline log analysis. Keep working credentials and historic logs. Work in the current checkout because the original implementation is uncommitted here. Do not connect to trade during development.

## Deliverables and verification

- [x] Move the six root modules into ritcx/ with common infrastructure extracted; preserve and run all 45 existing behavioral tests using package imports. Expose python3 -m ritcx.
- [x] Add tests for full-notional FX book valuation, tender cash capacity and exit deadlines, all intermediate basket positions, and malformed depth. Implement in ritcx/etf/model.py.
- [x] Add tests for news range syntax, forward variance, exit hysteresis and put-call parity pricing. Implement in ritcx/volatility/model.py. Keep parity diagnostic-only until execution is separately validated.
- [x] Add local fake-server tests for known partial fills, cancellation races, no duplicate uncertain orders, and read-only enforcement. Implement shared execution and optional passive tender slices, always reconcile actual fills before another leg.
- [x] Harden session reset observation, stopped heat handling, profile propagation, and per-mutation snapshot checks. Exercise both complete child processes against local simulators.
- [x] Add offline heat summaries for median P&L, worst loss, fines, submission rejections, unresolved exposure, and latency. Keep historical results separate from evidence for this version.
- [x] Preserve original competition files and user CSV with SHA-256 provenance. Replace obsolete plans and overlapping documentation with the README runbook, algorithm explanation, research mapping and source notes.
- [x] Run the full tests, CLI examples, compile check, source integrity verification and a CPU benchmark. Report validation limits explicitly.

## Operational choices

Each heat is exactly one 300-tick period. STOPPED tick 0 is waiting. Entry cutoffs and proactive liquidation are bot policies, not rule requirements. The runner follows a bounded number of heats and requires a clean account. New entries remain conservative by default; passive tender execution is opt-in; no automatic converters. Python 3.10+ on macOS/Linux is the supported runtime. DMA uses Basic auth; client REST, if used, uses X-API-Key and a distinct profile. No invented organizer credentials or contest ports.

## Research scope

Core: news-driven scenario valuation, delta-band hedging, selective whole-tender evaluation, adaptive direct execution, and executable basket signals. Secondary parity is integrated as a scanner. Manual conversion coordination is outside the autonomous bot: the rules reserve converters for people, and manual inventory changes conflict with automated reconciliation. No claim of an empirically optimal strategy without fresh independent heats.

## Final verification

The implementation is complete for the documented scope. Core research is executable; parity remains a diagnostic scanner and passive tender execution is opt-in. Final evidence and limitations are recorded in docs/validation.md. No competition orders were submitted during development. The original uncommitted source was preserved in /tmp/ritcx-before-integration.tar.gz before restructuring; credentials and historical account logs were not moved or overwritten.
