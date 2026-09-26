"""Signal-level parity check: strategies/gb_tokyo_drift.py vs. gbTokyoDrift.cs.

Ground truth is the .cs's own DiagLog output (a temporary diagnostic build,
2026-09-25 — see gbTokyoDrift's Obsidian testing log), captured on MGC 5m,
2026-06-14..2026-09-21, AutoTrade off (signal-only). This repo's MGC cache
only reaches 2026-08-07, so the checkable window is 2026-06-14..2026-08-07 —
20 of the .cs's 25 fires. The gate is signal-level agreement (entry bar +
direction + level name), NOT P&L — this port resolves fills on real ticks,
the .cs's Strategy Analyzer run resolves them on bar-close approximations, so
a P&L difference is expected and not a bug.

Usage (repo root): python3 -m tools.compare_tokyodrift
"""
from datetime import datetime

from backtester import Backtest
from strategies.gb_tokyo_drift import GbTokyoDrift

# Pasted verbatim from tokyodrift-fvg-diag.log's FIRE lines (see gbTokyoDrift's
# Obsidian testing log, 2026-09-25 entry) that fall inside the cached window.
REFERENCE_FIRES = [
    ("2026-06-15 21:45", 1, "6-8pm High"),
    ("2026-06-16 02:05", -1, "6-8pm High"),
    ("2026-06-16 20:55", 1, "6-8pm Low"),
    ("2026-06-16 22:05", 1, "6-8pm High"),
    ("2026-06-17 01:10", -1, "NY VAL"),
    ("2026-06-24 21:25", 1, "NY POC"),
    ("2026-06-24 22:30", -1, "6-8pm Low"),
    ("2026-06-25 00:30", 1, "6-8pm Low"),
    ("2026-06-25 02:10", 1, "NY POC"),
    ("2026-06-25 20:50", 1, "6-8pm Low"),
    ("2026-06-25 23:30", 1, "NY VAL"),
    ("2026-06-25 23:55", -1, "NY POC"),
    ("2026-06-26 01:35", 1, "6-8pm Low"),
    ("2026-06-29 02:35", 1, "6-8pm Low"),
    ("2026-07-09 21:10", 1, "NY POC"),
    ("2026-07-13 22:35", 1, "6-8pm High"),
    ("2026-07-14 03:15", 1, "NY POC"),
    ("2026-07-20 19:25", 1, "NY VAL"),
    ("2026-07-23 20:30", -1, "6-8pm Low"),
    ("2026-07-26 21:05", -1, "6-8pm High"),
]

# gapAtr from the same DiagLog FIRE lines, same order as REFERENCE_FIRES (for
# a quick "did we detect the same geometric gap" sanity check on mismatches).
REF_GAP_ATR = [0.51, 0.27, 0.37, 0.79, 0.38, 0.25, 0.15, 0.69, 0.19, 0.50,
               0.10, 0.49, 0.22, 0.12, 0.68, 0.17, 0.97, 0.37, 0.12, 0.57]

# .cs level names -> this port's names (cosmetic only, RangeName default changed
# during the gbFitMom -> gbTokyoDrift Eastern-time conversion; "6-8pm" here maps
# to this port's generic "Range High"/"Range Low")
_NAME_MAP = {
    "6-8pm High": "Range High",
    "6-8pm Low": "Range Low",
    "NY POC": "NY POC",
    "NY VAH": "NY VAH",
    "NY VAL": "NY VAL",
}


def main():
    strat = GbTokyoDrift()
    strat.take_longs = strat.take_shorts = False  # signal-only, matches the .cs diag run's intent

    bt = Backtest(strat, symbol="MGC", period="5m",
                  start="2026-06-14", end="2026-08-07", prop=None)
    print(f"Running gb_tokyo_drift over {bt.symbol} {bt.barspec.key}, "
          "2026-06-14..2026-08-07 ...")
    bt.run()

    # gbTokyoDrift.cs's DiagLog timestamps with Time[0] -- the FORMING bar's
    # close, which for a time-bar chart NT8 knows in advance and which equals
    # (confirming bar's close) + one bar length. Our fires list timestamps
    # with the confirming bar's own bar.ts, so add one bar length here purely
    # for this comparison -- the strategy's own order submission already fires
    # at the correct real moment (this is a logging-convention difference,
    # not a signal-timing difference; see this tool's own module docstring).
    period_ns = strat._period_s * 1_000_000_000
    from zoneinfo import ZoneInfo
    ours = []
    for f in strat.fires:
        et = datetime.fromtimestamp((f["entry_time"] + period_ns) / 1e9, ZoneInfo("America/New_York"))
        gap_atr = (f["gtop"] - f["gbot"]) / f["atr"] if f["atr"] else float("nan")
        ours.append((et.strftime("%Y-%m-%d %H:%M"), f["dir"], f["name"], gap_atr))

    print(f"\nReference (.cs) fires in window: {len(REFERENCE_FIRES)}")
    print(f"Port fires in window:            {len(ours)}\n")

    ref_norm = [(t, d, _NAME_MAP.get(n, n)) for t, d, n in REFERENCE_FIRES]
    ref_gap = dict(zip([(t, d) for t, d, _ in REFERENCE_FIRES], REF_GAP_ATR))
    matched = 0
    print(f"{'ref time':<17} {'ref dir':>7} {'ref name':<10} {'ref gapAtr':>10} | {'match?':<8} {'port time':<17} {'port dir':>8} {'port name':<10} {'port gapAtr':>10}")
    used = [False] * len(ours)
    for rt, rd, rn in ref_norm:
        hit = None
        for i, (ot, od, on, og) in enumerate(ours):
            if used[i]:
                continue
            if ot == rt and od == rd:
                hit = i
                break
        rg = ref_gap.get((rt, rd), float("nan"))
        if hit is not None:
            used[hit] = True
            matched += 1
            ot, od, on, og = ours[hit]
            ok = "OK" if on == rn else "NAME?"
            print(f"{rt:<17} {rd:>7} {rn:<10} {rg:>10.2f} | {ok:<8} {ot:<17} {od:>8} {on:<10} {og:>10.2f}")
        else:
            print(f"{rt:<17} {rd:>7} {rn:<10} {rg:>10.2f} | {'MISSING':<8}")

    extra = [o for i, o in enumerate(ours) if not used[i]]
    for ot, od, on, og in extra:
        print(f"{'':<17} {'':>7} {'':<10} {'':>10} | {'EXTRA':<8} {ot:<17} {od:>8} {on:<10} {og:>10.2f}")

    print(f"\n{matched}/{len(ref_norm)} reference fires matched "
          f"(entry bar + direction). {len(extra)} extra fires in the port not in the reference.")


if __name__ == "__main__":
    main()
