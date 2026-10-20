# V6 Smart Money Flow — Issue #6, incremental delivery 1

READ-ONLY / PAPER. No account credentials, order client, worker, Telegram bot,
service activation or changes to main. This is not verified V5 integration.

## Dependencies and scope

Base: PR #1 head `8618eab5b836c7d840a3d86eaaef47e424842f23`.
PR #4 public GET code is already reviewed and integrated in that base; do not
merge its older overlapping transport over it. Issue #5 source review/live API/
Telegram gates remain BLOCKED. This PR targets `codex/paper-safety-ci`, not main.
Original nine V5 sources and actual handler signatures remain unavailable.

## Data semantics and universe

The scanner discovers actual `state=live, instType=SPOT` instrument metadata from
TR's public catalogue. It supports USDT and TRY quotes; all other quote currencies
are explicitly excluded with `UNSUPPORTED_QUOTE_FX`, not silently mapped to USDT.
No hardcoded coin listing or fabricated current availability is used. Tick/lot/
min size are retained. TRY conversion requires a discovered, fresh `USDT-TRY`
SPOT ticker with <=10bps spread at the same official TR origin. If absent, stale
or invalid, TRY coverage is unavailable; no external/global FX is substituted.

24h quote turnover uses `volCcy24h` directly. A TRY value is divided by current
USDT/TRY midpoint. This is a *current-rate activity estimate*, not historical FX
accounting, true USD valuation, customer holdings or a record of net deposits.
Aggregating base coins adds distinct pair executions once per venue/pair, and
lists each base coin once. The FX bridge USDT is not a candidate. The denominator
covers available discovered quote-pair turnover (including the FX bridge); if any
supported pair's volume/FX is missing, shares and eligibility are suppressed.

## Executed flow, bars and order book

- Public recent trades limit 500; history limit 100 with numeric trade-ID `after`
  cursor and pagination type 1. At most 10 history pages. The scan must observe
  the beginning of the exact 5-minute `(start,end]` window; otherwise no CVD or
  candidate is emitted. Duplicate trade IDs count once; conflicting payloads fail
  closed. No claim that consecutive integer IDs prove completeness. Coverage
  depends on the exchange's paginated history, not a private independent audit.
- `side` is the documented **taker** side. Positive quote-notional CVD means
  aggressive buy notional exceeds sell notional. Base delta and quote CVD are
  separate. All-buy ratio has an explicit state rather than dividing by zero.
  CVD is **not** actual net capital inflow or a measure of exchange balances.
- 1m, 5m, 15m, 1H public bars: aligned descending timestamps, no gaps, valid
  OHLC/volume/confirmation, latest completed bar required. 5m RVOL is latest
  completed quote turnover divided by the mean of the **20 preceding completed**
  5m bars; neither the target nor current incomplete bar enters the baseline.
- Books request `sz=400`; actual returned count is retained. REST timestamps,
  sequence monotonicity/conflicts, sorting/crossing and ±0.5%/1% native-quote to
  USDT depth/imbalance are checked. Gaps in full REST snapshot sequence numbers
  are allowed; conflicting/regressing snapshots latch RESYNC_REQUIRED. A caller's
  observed reconnect also latches it. Explicit review/new scanner is required;
  no silent reset. This is not a WS stream or a tested checksum/prevSeqId client.
- Visible ask/bid execution is walked for the configured hypothetical paper
  notional; insufficient capacity, spread, depth and slippage block candidates.
  Fee is an explicit paper assumption, not an authenticated fee-tier lookup.
  Metric serialization rounds away from zero at 18 decimals; the existing paper
  ledger and its high precision accounting are untouched.

## Candidate selection and rotation

Defaults are configurable starting filters, not proven alpha: RVOL >=3,
24h quote volume >=10M USDT, 15m completed-bar return 1–5%, nonnegative 1H,
BTC 1H >-2%, spread <=10bps, each side's ±0.5% depth >=2500 USDT, positive
executed CVD and >=55% taker buy share, hypothetical 90 USDT execution with
<=0.2% midpoint slippage. Resting walls alone cannot qualify a candidate.

Score breakdown: RVOL <=30, momentum <=20, taker share <=30, liquidity <=20.
Eligibility gates are mandatory regardless of score. Score is an unvalidated
heuristic, not a win probability, prediction, guaranteed target or sizing signal.
A coin needs complete supported same-base pair coverage in that batch and net
positive combined taker delta; other assets are watch-only/rejected.

Volume-share rotation compares rolling 24h turnover shares against a prior
private snapshot only for an identical universe/hash and volume-key set, with a
positive elapsed interval <=1 hour. First snapshot has `share_change=null`.
Current FX changes and rolling-window expiry affect this metric. It does not
prove money transferred between coins. Incompatible baselines reset explicitly.

## Single owner command hook

The existing owner may invoke `handle_flow_update`; no poller or sender starts.
It reuses the tested user+private-chat ACL, timestamp/rate limit, durable update
fingerprint and cursor. The 500 profile is mandatory. Commands:

- `/paraakisi`: up to 5 eligible *current batch* base coins; timestamps, source,
  score breakdown, volume, taker delta, liquidity, risks and next offset.
- `/radar`: the same early-momentum view with durable per-coin 15-minute cooldown.
  Duplicate update returns its receipt without GET or another outbox entry.
  Cooldown records scheduling, not successful delivery. `delivery=NOT_SENT` and
  the existing private notice outbox require V5 ACK/UNKNOWN reconciliation before
  actual send. Repeated alerts are removed from the rendered candidate list.
- `/analiz COIN`, `/analiz COIN-USDT`, `/analiz COIN-TRY`: discovery-targeted
  same-base pair analysis with entry conditions/invalidation. No configured
  directional plan exists here, so stop/target prices and risk/reward are
  explicitly **not computed** rather than guessed.
- `/grid`, `/durum`, `/paper` are delegated to the existing read-only market/
  paper command boundary. A real fixture-backed grid route regression verifies
  the 500 profile, proposal-only semantics and zero capital reservation.

Snapshot data, accepted-update receipts, results, baseline, cooldown and notices
stay in the private paper SQLite state. A crash between auth acceptance and
analysis receipt yields UNKNOWN and requires a new update; never blind resend.
No database, real response, trade log, account balance or token is committed.

## Batching, timeouts and replay limits

`scan_batch(offset, limit=3, expected_universe=...)` provides reproducible cursor
progress across the discovered universe without dropping shifted pairs. Any
universe change requires restart. Each fresh batch reports its actual scope;
results from earlier batches are not combined into a fake simultaneous ranking.
Coin analysis fetches all currently supported same-base pairs, not the next
unrelated batch. All-market fresh top5, continuous WS collection, provider gap
repair and an independent streaming scheduler are not delivered in increment 1.

The shared HTTP client retains TLS/redirect/pacing/retry bounds. Captures and
batches >30s are discarded. This is an end-of-work freshness veto, **not hard
cancellation** of a slow request. Before connecting V5, its existing owner must
isolate this work with a verified timeout/cancellation/rollback design so polling
and the active Paper Trader cannot be blocked. No process/systemd was changed.

`flow_replay.replay_roundtrip` evaluates supplied USDT entry and later exit books
net of explicit fees and visible spread/depth. Tests include flat-price loss,
favorable exit, gap beyond 5 USDT and missing exit liquidity. This is deterministic
execution replay, not a historical/forward-tested profitable strategy or a new
wallet. Existing paper risk/accounting tests continue to pass unchanged.

## Validation and access evidence

Run `python tools/validate.py`. Offline fixtures include full GET transport to
flow command, exact CVD direction/duplicate conflict/window coverage, TRY FX,
RVOL baseline/confirmation/gaps, universe/rotation, depth/spread/slippage, BTC
shock/chase, cursor/reconnect, cooldown/restart/UNKNOWN, outbox rollback, legacy
/grid regression and fee-aware execution replay. CI remains Python 3.11/12/13;
Python 3.12 builds/stages the exact commit without activation.

Explicit network smoke in an authorized environment:

```
python tools/flow_smoke.py --allow-public-network --target BTC --limit 1
```

This environment still has no V5 directory/SSH config/agent/remote shell. Previous
DigitalOcean browser navigation returned `Site Unavailable`; no new credentials
were requested or sent. Official TR API domain is outside the environment's
network allowlist. The actual V6 smoke result and exact CI/SHA are reported in
the PR. No real API data, real Telegram command or production V5 integration is
claimed from synthetic tests. No activation, restart, main merge or real order.

## Remaining acceptance gates

1. Issue #5: authorized TR public smoke and original V5 source/single-owner audit.
2. Real supported pair/quote availability; real TRY FX and all trade/bar endpoints.
3. Existing V5 timeout/rollback and send ACK/UNKNOWN integration; actual Telegram
   commands and 500 USDT paper shadow, without interrupting the active service.
4. Verified TR public WS availability, reconnect/resync/checksum contracts and
   streaming collection, full-universe freshness/ranking and historical replay.
5. Reviewed directional plan for risk/reward; no invented entry/target levels.

REAL_API_VERIFIED=false, TELEGRAM_VERIFIED=false, ACTIVATED=false.
Real trading remains unavailable; this document grants no live-order authority.
