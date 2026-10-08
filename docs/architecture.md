# Architecture

How a backtest flows through the code, and the rules that keep the fills
honest. Read this before changing the engine or broker.

```
 Parquet day files          data.py                 engine.py
 (raw L1 events)  ──►  reduced cache + bar cache ──► day loop ──► per bar:
                                                          1. broker.resolve_span(i0, i1)
                                                          2. strategy.on_bar(bar, bars)
                                                                 │
        orders ◄────────────────────── broker.py ◄───────────────┘
          │            (fills on real ticks)
          ▼
   account.py ──► TradeRecorder ──► metrics.py ──► report.py (HTML + CSV)
   PropFirmTracker                       └──► montecarlo.py
```

## The rule that matters most: no look-ahead

For every bar the engine calls `broker.resolve_span(i0, i1)` **first**, which
fills any working orders against the ticks inside the bar, and only then calls
`strategy.on_bar`. A strategy therefore never sees a bar whose fills it could
have influenced, and never trades on information from inside the bar it is
deciding on. Orders submitted in `on_bar` are first evaluated on the next
bar's ticks.

Fills always resolve on real ticks, whatever the bar type. Renko, TBars and
tick bars only change *when the strategy is asked to decide*.

## Modules

| Module | Job |
|---|---|
| `data.py` | `Catalog` finds day files. The first touch of a day reduces ~24M raw L1 events to trade events plus the prevailing bid/ask (the **reduced cache**), then builds per-period **bar caches**. Each bar stores its index span `[i0, i1)` into the reduced arrays. Cached runs cost ~0.05 s/day. Caches live in `.cache/` and are rebuilt when `CACHE_VERSION` / `BARS_VERSION` change. |
| `engine.py` | Day loop. Sessions are `"HH:MM"` **US/Eastern**. Bars outside the session are skipped, orders are cancelled and positions flattened at session end. Overnight sessions (start > end, e.g. `("18:00", "16:55")`) are split into segments so positions and orders carry across the file boundary. Secondary timeframes (`secondary_periods`) are appended the instant they close. |
| `broker.py` | Span resolution. Repeatedly finds the earliest-triggering working order in the remaining span (a vectorised search on the trade-price slice), marks equity up to the fill, applies it, and continues from that event. |
| `orders.py`, `atm.py` | Order types, brackets, OCO legs, and ATM-style multi-bracket exits (scale-out, breakeven, trailing). |
| `account.py` | `Account` (signed position, average price, realised P&L net of commission), `TradeRecorder` (round trips with MAE/MFE in dollars), `PropFirmTracker` (trailing threshold). |
| `strategy.py` | The `Strategy` base class: `on_start`, `on_bar`, `on_fill`, `on_session_end`, `on_finish`, optional `on_secondary_bar` and `on_tick`, plus order helpers (`buy_bracket`, `move_stop`, ...). |
| `indicators.py` | Incremental (bar-by-bar) EMA, SMA, ATR, RSI, Highest, Lowest, KAMA and friends, following NinjaTrader's warm-up conventions. |
| `metrics.py`, `report.py` | Statistics dict, console summary, self-contained HTML tearsheet (Plotly from a CDN) and trades CSV. |
| `montecarlo.py` | Trade-P&L resampling, i.i.d. or circular block bootstrap (chosen automatically when trade autocorrelation is high), prop-firm breach probability and eval-pass probability. |
| `sweep.py`, `walkforward.py` | Parameter grids over a process pool, a sensitivity report that flags fragile parameters, and rolling in/out-of-sample walk-forward. |
| `risk.py` | Prop-firm account presets, a headroom-based risk budget, profit banking and an eval simulator, exposed to strategies through `Strategy.size_within_budget` and `Strategy.bank_profit`. |
| `sizing.py`, `news.py` | Volatility-targeted position sizing and the high-impact news filter. |
| `nt8config.py` | Reads saved NinjaTrader ATM and strategy templates so a live configuration can drive a backtest. |

## Fill semantics

- **Market:** fills at the opposite quote, plus slippage ticks.
- **Limit:** needs the trade price to go *through* the limit. Touching it does
  not fill. A limit that is marketable when first evaluated fills at the quote.
- **Stop:** triggers on the last trade price, fills at the quote, and is never
  filled better than the stop price.
- **Reversals** are split at flat so the trade recorder sees clean round trips.

## Data hygiene built into the reducer

- Raw timestamps from the NinjaTrader recorder are the recording machine's
  US/Eastern wall clock. The reducer converts them to UTC per day. Every
  timestamp after reduction is int64 nanoseconds UTC.
- A **crossed quote** (bid > ask) means one side of the book is stale. The
  reducer blanks both sides for that event, keeps the trade price, and the
  aggressor classifier treats it as unknown.
- Renko-style bar state is carried across day-file boundaries and reset only
  on a real trading gap, because an overnight session trades straight through
  midnight ET.

## Prop-firm model

`PropFirmTracker` trails the **intratrade** equity peak (unrealised P&L
included) by `threshold`, and locks at start balance + `lock_buffer`. A breach
is equity touching the floor. The Monte Carlo breach model tests each trade's
MAE against the floor from the *prior* trades and tests the close against the
floor including the trade's own MFE. Applying a trade's MFE to its own trough
would fabricate breaches.

## Tests

Tests build synthetic tick streams with `tests/conftest.py::make_day` and
assert hand-computed fills. Keep that style: it catches real bugs, and a
fill-model change should come with a test that fails without it.
