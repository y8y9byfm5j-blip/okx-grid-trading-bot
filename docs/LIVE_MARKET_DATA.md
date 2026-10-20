# P0 live read-only market data: implementation and activation gates

Issue #5 / PR #4 integration, 20 October 2026. Real trading remains unavailable.

## What is implemented

- Reviewed SOL PR #4 (`3f9b172`) transport, integrated on `codex/paper-safety-ci`.
  Six explicit GET contracts at `https://tr.okx.com`: SPOT instruments catalogue
  and individual instrument, SPOT tickers, individual ticker, books `sz=400`,
  candles `15m`/`1H` (3 rows), public server time. No arbitrary URLs, credentials,
  cookie jar, inherited proxy, private APIs or trade methods.
- Explicit certificate validation/hostname verification, no redirects including
  the final returned URL, timeout/response size limits, process-shared 4 GET/sec
  pacing and at most 3 retries with 1/2/4s backoff plus <=100ms jitter.
  Retry-After numeric values <=30s are honored; longer cooldowns fail closed.
  HTTP 403, bad certificate, malformed JSON and nonretryable exchange errors
  do not cause retries. Failures expose stable codes, never remote bodies.
  The single V5 owner must share the client/pacer. Cross-process/global IP
  rate coordination is not implemented; don't start a second poller.
- `TRMarketData.analyze`: server/local time skew check; live TR SPOT/USDT
  instrument eligibility including BTC; completed 15m, 1H and BTC 1H candles;
  ticker/book temporal coherence, freshness and previous REST cursor checks.
  A completed capture older than 10s is discarded. This end-of-capture bound is
  not a hard network cancellation deadline: GET timeout/backoff applies per
  request. The existing V5 scheduler must isolate request work with its own
  timeout/cancellation design before production integration.
- Book reports retain actual returned counts (up to 400/side, not a guarantee
  that 400 exist), top 5 levels/notionals, ±0.5% and ±1% notional depth about
  bid/ask midpoint, imbalance `(bid-ask)/(bid+ask)`, band coverage flags and
  observed largest notional levels. All depth is truncated to returned levels;
  walls are cancelable observations, not predictions. REST snapshot gaps are
  not WebSocket continuity claims. Conflicting/regressing cursors fail closed;
  cursor persistence across process restarts and WS resync are not implemented.
- `radar` queries SPOT tickers, then scans only a configured universe of <=10
  symbols; full quality checks fetch each selected symbol's actual book and
  completed candles. Results are preliminary market filters, not an automatic
  plan or whole-market discovery. Missing/stale/low-volume/downtrend/BTC risk-off
  produce explicit rejections. Older captures are discarded after the scan.
- `grid` validates an explicitly supplied reviewed PAPER plan, exchange
  tick/lot/min size, volume/spread/near depth, fresh trends, bot/total portfolio
  risk, available cash, fee and slippage. The command requires the 500 profile.
  Bands, fee tier and slippage are not inferred from a wall or invented if no
  template exists. Result is a proposal: no capital reservation, bot opening
  or trading. Another worker can change portfolio after proposal generation;
  eventual opening must rerun the atomic paper risk checks.
- `handle_market_update` reuses private chat AND user ACL, freshness, update
  fingerprint/cursor/rate controls BEFORE any market GET. The existing consumer
  may call this hook and pass its returned message to its existing sender.
  No new Telegram poller/sender or token handling exists here. Duplicate update
  returns its stored result without network; interrupted analysis is UNKNOWN
  and requires a new update, not blind retry. Stored duplicate reports retain
  original as-of timestamp and are never presented as freshly fetched.
- `/analiz SYMBOL-USDT`, `/radar`, `/grid`, `/durum` have tested reference
  message builders. Successful market reports label
  `CANLI PİYASA VERİSİ — SANAL İŞLEM (PAPER)` and `anlık REST snapshot`.
  Failed capture returns `VERİ ALINAMADI - ANALİZ YOK` without cached quotes.
  `/durum` reports connection check age, paper state and `V5 NOT_VERIFIED`;
  it does not claim systemd/process health. `delivery=NOT_SENT` persists.

## Explicit public smoke, in an authorized network environment

```
python tools/validate.py
python tools/public_smoke.py --allow-public-network --symbol BTC-USDT --symbol SOL-USDT
```

The explicit flag prevents accidental network calls in offline CI/staging.
The smoke prints only public metric/timestamp summaries, no raw API replies,
credentials, account balances or trade history. Nonzero exit means not verified.
Routine CI does not call the live API and its green status is not live evidence.

This environment's actual smoke on 20 October returned exit 2:
`{"ok":false,"live_enabled":false,"error":"PUBLIC_HTTP_UNAVAILABLE"}`.
No real price, depth, volume or trend measurement was obtained. The TR API host
is outside the supplied network allowlist; no fallback to global OKX or a relay
was attempted. Official documentation access is not API transport verification.

## V5 and activation — BLOCKED

`/opt/okx-grid-demo/v5`, SSH configuration/agent and a DigitalOcean remote shell
are unavailable. Original nine source contracts, Telegram command handlers,
single consumer ownership, active services and a safe rollout/rollback path
could not be inspected. This hook is NOT a verified V5 integration.

REAL_API_VERIFIED=false, TELEGRAM_VERIFIED=false, ACTIVATED=false.
No command has been verified in the user's real Telegram bot. No process was
restarted or new Telegram consumer installed. Main and live orders stay closed.

Activation requires successful official TR smoke from an authorized environment,
read-only source review, known V5 interfaces, one consumer, failure/timeout and
rollback tests, real Telegram command evidence, and unchanged trading guard.
The user's read-only activation authorization is recorded; these technical gates
have not passed, so no activation is attempted.

Official contracts checked: https://tr.okx.com/docs-v5/en/ — Market Data ticker,
SPOT quote `volCcy24h` vs base `vol24h`, books depth/seqId, candles confirmation,
public instruments and server time. HTTP cache services can return older
snapshots; this implementation rejects rather than silently adopting regressions.
